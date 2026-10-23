"""
Watches the Elite Dangerous journal and keeps elite-dangerous.db's
live-state tables current: materials_inventory, commander_status,
engineer_progress, current_loadout (+ its modules), and ship_state (the
fuel on board and cargo in the hold, tracked from events).

WHY REPLAY THEN TAIL (not just tail from the end):
Materials/Rank/LoadGame/SquadronStartup are only written ONCE per session,
at the top of the journal file. Tailing from the end alone would leave us
with zero materials/rank/ship info until the player's next pickup or
jump. Reading the whole current file top-to-bottom first reconstructs
correct starting state; tailing from there keeps it current.

WHY EngineerCraft/EngineerContribution/Synthesis MATTER as much as
MaterialCollected/Discarded/Trade:
These are what actually CONSUME materials -- applying a blueprint,
contributing to unlock an engineer, or synthesizing a consumable. Missing
these would mean the tracked inventory only ever grows, and Ellie would
confidently say you have materials you used up hours ago.

CONFIDENCE NOTE: every event handled here is verified against the
documented journal manual, and EngineerCraft / EngineerProgress /
Location / LoadGame / ShipyardSwap have also been checked against real
journal lines. Two things those real lines showed that the manual alone
didn't:
  - EngineerCraft's Ingredients are {Name, Count} with NO Category, so
    the category on record in materials_inventory is kept as-is.
  - EngineerProgress reports the unlock state as "Progress" ("Known" /
    "Invited" / "Unlocked"), and "Rank" only once unlocked.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_DIR = Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"
DB_PATH = Path(__file__).resolve().parent / "elite-dangerous.db"
POLL_INTERVAL_SECONDS = 2


def find_latest_journal(journal_dir=JOURNAL_DIR):
    files = list(journal_dir.glob("Journal.*.log"))
    if not files:
        raise FileNotFoundError(f"No journal files found in {journal_dir}")
    # Modification time, not filename sort -- a filename-based sort assumes
    # every journal file's timestamp is in the same, correctly-sortable
    # format, which breaks on old pre-2018 journals (different date
    # format), stray backups, or sync-tool-renamed copies. mtime is a
    # direct, unambiguous signal for "written to most recently" regardless
    # of what the file happens to be named.
    return max(files, key=lambda f: f.stat().st_mtime)


def build_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS commander_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            commander_name TEXT, current_ship TEXT, current_ship_id INTEGER,
            current_system TEXT, current_station TEXT, docked INTEGER,
            combat_rank INTEGER, trade_rank INTEGER, explore_rank INTEGER,
            cqc_rank INTEGER, federation_rank INTEGER, empire_rank INTEGER,
            credits INTEGER, squadron_name TEXT, squadron_rank INTEGER,
            last_updated TEXT,
            current_x REAL, current_y REAL, current_z REAL
        );
        INSERT OR IGNORE INTO commander_status (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS materials_inventory (
            material_name TEXT PRIMARY KEY, category TEXT, display_name TEXT,
            count INTEGER, last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS engineer_progress (
            engineer_name TEXT PRIMARY KEY, rank_stage TEXT, rank_value INTEGER,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS current_loadout (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ship_key TEXT, ship_id INTEGER, ship_name TEXT,
            max_jump_range REAL, unladen_mass REAL, cargo_capacity REAL,
            fuel_main REAL, fuel_reserve REAL,
            hull_value INTEGER, modules_value INTEGER, rebuy INTEGER,
            last_updated TEXT
        );
        INSERT OR IGNORE INTO current_loadout (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS current_loadout_modules (
            slot TEXT PRIMARY KEY,
            item TEXT, is_on INTEGER, priority INTEGER, health REAL, value INTEGER,
            engineering_blueprint TEXT, engineering_level INTEGER, engineering_quality REAL,
            engineering_experimental TEXT, engineering_modifiers_json TEXT,
            last_updated TEXT
        );

        -- Fuel and cargo live in their own table, NOT in current_loadout:
        -- a Loadout event is a full snapshot that fires on any module
        -- change, and replacing current_loadout wholesale must never wipe
        -- the fuel on board or the cargo in the hold. NULL means unknown.
        CREATE TABLE IF NOT EXISTS ship_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            fuel_main REAL, cargo_count INTEGER, fuel_updated TEXT, cargo_updated TEXT,
            last_jump_dist REAL, last_jump_fuel_used REAL, last_jump_fuel_before REAL,
            last_jump_cargo INTEGER, last_jump_unladen REAL, last_jump_fsd_item TEXT,
            last_jump_boosted INTEGER, last_jump_time TEXT
        );
        INSERT OR IGNORE INTO ship_state (id) VALUES (1);
        """
    )
    # CREATE TABLE IF NOT EXISTS won't add columns to a table an earlier
    # version already created, so add any that are missing.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(commander_status)")}
    for col in ("current_x", "current_y", "current_z"):
        if col not in existing:
            conn.execute(f"ALTER TABLE commander_status ADD COLUMN {col} REAL")
    conn.commit()


def _ship_display_name(internal, localised):
    """The journal reports ships by internal name ('Explorer_NX') and adds
    Ship_Localised ('Caspian Explorer') when the in-game name differs.
    Use the game's own name rather than a table of ours; when there's no
    localised name the internal name already is the display name."""
    if localised:
        return localised
    return internal[:1].upper() + internal[1:] if internal else internal


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_status(conn, **fields):
    fields["last_updated"] = _now_iso()
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE commander_status SET {cols} WHERE id = 1", fields)
    conn.commit()


def set_ship_state(conn, **fields):
    """Write the named ship_state columns. A field left out stays as it was;
    a field passed as None is written as NULL, i.e. "unknown". The *_updated
    stamps follow the value they describe."""
    now = _now_iso()
    if "fuel_main" in fields:
        fields["fuel_updated"] = now
    if "cargo_count" in fields:
        fields["cargo_updated"] = now
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE ship_state SET {cols} WHERE id = 1", fields)
    conn.commit()


def forget_ship_fuel_and_cargo(conn):
    """A different ship (or a respawn) means the tracked fuel and cargo no
    longer describe the ship we're in. Unknown beats a stale guess: nothing
    downstream computes a current-load jump range from a NULL."""
    set_ship_state(conn, fuel_main=None, cargo_count=None)


def add_refuelled_fuel(conn, amount):
    """RefuelAll/RefuelPartial report only the tonnes ADDED, so they can only
    extend a level we already know -- with no known level there is nothing to
    add to and it stays unknown. Capped at the tank capacity from the
    Loadout when that is known."""
    if amount is None:
        return
    fuel = conn.execute("SELECT fuel_main FROM ship_state WHERE id = 1").fetchone()[0]
    if fuel is None:
        return
    fuel += amount
    capacity = conn.execute("SELECT fuel_main FROM current_loadout WHERE id = 1").fetchone()[0]
    if capacity is not None:
        fuel = min(fuel, capacity)
    set_ship_state(conn, fuel_main=fuel)


def record_fsd_jump(conn, event):
    """Snapshot what the game says this jump cost, alongside the ship as it
    was BEFORE the jump (cargo, unladen mass, fitted FSD, fuel), so
    query_functions can check its mass model against the game's own fuel
    burn; then take FuelLevel as the new tank level.

    The snapshot is all-or-nothing: an event missing JumpDist, FuelUsed or
    FuelLevel leaves the previous snapshot whole rather than mixing two
    jumps' numbers. The new tank level is still taken whenever FuelLevel is
    there."""
    dist, used, level = event.get("JumpDist"), event.get("FuelUsed"), event.get("FuelLevel")
    fields = {}
    if dist is not None and used is not None and level is not None:
        cargo = conn.execute("SELECT cargo_count FROM ship_state WHERE id = 1").fetchone()[0]
        unladen = conn.execute("SELECT unladen_mass FROM current_loadout WHERE id = 1").fetchone()[0]
        fsd = conn.execute(
            "SELECT item FROM current_loadout_modules WHERE slot = 'FrameShiftDrive'"
        ).fetchone()
        fields.update(
            last_jump_dist=dist, last_jump_fuel_used=used, last_jump_fuel_before=level + used,
            last_jump_cargo=cargo, last_jump_unladen=unladen,
            last_jump_fsd_item=fsd[0] if fsd else None,
            last_jump_boosted=1 if event.get("BoostUsed") else 0,
            last_jump_time=event.get("timestamp"),
        )
    if level is not None:
        fields["fuel_main"] = level
    if fields:
        set_ship_state(conn, **fields)


def reset_materials(conn, materials_event):
    """Materials event is a full snapshot -- replace the table, don't merge."""
    conn.execute("DELETE FROM materials_inventory")
    now = _now_iso()
    for category in ("Raw", "Manufactured", "Encoded"):
        for item in materials_event.get(category, []):
            conn.execute(
                "INSERT INTO materials_inventory VALUES (?,?,?,?,?)",
                (
                    item["Name"].lower(),
                    category,
                    item.get("Name_Localised", item["Name"]),
                    item["Count"],
                    now,
                ),
            )
    conn.commit()


def set_loadout(conn, event):
    """Loadout is a full snapshot of the current ship, fired on launch, on
    any module change, and on request -- replace both tables wholesale,
    the same convention reset_materials() already uses for Materials."""
    now = _now_iso()
    fuel = event.get("FuelCapacity") or {}
    conn.execute(
        "UPDATE current_loadout SET ship_key=?, ship_id=?, ship_name=?, max_jump_range=?, "
        "unladen_mass=?, cargo_capacity=?, fuel_main=?, fuel_reserve=?, hull_value=?, "
        "modules_value=?, rebuy=?, last_updated=? WHERE id = 1",
        (
            event.get("Ship"), event.get("ShipID"), event.get("ShipName"),
            event.get("MaxJumpRange"), event.get("UnladenMass"), event.get("CargoCapacity"),
            fuel.get("Main"), fuel.get("Reserve"),
            event.get("HullValue"), event.get("ModulesValue"), event.get("Rebuy"),
            now,
        ),
    )

    conn.execute("DELETE FROM current_loadout_modules")
    for m in event.get("Modules", []):
        eng = m.get("Engineering") or {}
        conn.execute(
            "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.get("Slot"), m.get("Item"), int(bool(m.get("On"))), m.get("Priority"),
                m.get("Health"), m.get("Value"),
                eng.get("BlueprintName"), eng.get("Level"), eng.get("Quality"),
                eng.get("ExperimentalEffect"),
                json.dumps(eng["Modifiers"]) if "Modifiers" in eng else None,
                now,
            ),
        )
    conn.commit()


def adjust_material(conn, name, category, display_name, delta):
    """delta > 0 for gained, < 0 for consumed. Never lets count go below 0
    (a delta arriving before the matching Materials snapshot replay, or a
    rounding/ordering quirk, shouldn't produce a negative on-hand count)."""
    if not name:
        return
    key = name.lower()
    now = _now_iso()
    row = conn.execute(
        "SELECT count, category FROM materials_inventory WHERE material_name = ?", (key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO materials_inventory VALUES (?,?,?,?,?)",
            (key, category, display_name or name, max(0, delta), now),
        )
    else:
        existing_count, existing_category = row
        conn.execute(
            "UPDATE materials_inventory SET count = ?, category = ?, last_updated = ? WHERE material_name = ?",
            (max(0, existing_count + delta), category or existing_category, now, key),
        )
    conn.commit()


def process_event(conn, event):
    etype = event.get("event")

    if etype == "LoadGame":
        set_status(
            conn,
            commander_name=event.get("Commander"),
            current_ship=_ship_display_name(event.get("Ship"), event.get("Ship_Localised")),
            current_ship_id=event.get("ShipID"),
            credits=event.get("Credits"),
        )
        # The Cargo event follows at launch, so the hold is unknown until then.
        fields = {"cargo_count": None}
        if event.get("FuelLevel") is not None:
            fields["fuel_main"] = event["FuelLevel"]
        set_ship_state(conn, **fields)
    elif etype == "ShipyardSwap":
        # Swapping ships doesn't emit a new LoadGame, so without this
        # current_ship would stay on the old ship until the next launch.
        set_status(
            conn,
            current_ship=_ship_display_name(event.get("ShipType"), event.get("ShipType_Localised")),
            current_ship_id=event.get("ShipID"),
        )
        forget_ship_fuel_and_cargo(conn)
    elif etype in ("Died", "Resurrect"):
        forget_ship_fuel_and_cargo(conn)
    elif etype == "Cargo":
        # The game writes a fresh Cargo event after every change to the hold,
        # so the last one is enough -- no trade/mining delta events needed.
        # Vessel can also be "SRV", which is a different hold.
        if event.get("Vessel") == "Ship" and event.get("Count") is not None:
            set_ship_state(conn, cargo_count=event["Count"])
    elif etype == "ReservoirReplenished":
        if event.get("FuelMain") is not None:
            set_ship_state(conn, fuel_main=event["FuelMain"])
    elif etype == "FuelScoop":
        if event.get("Total") is not None:
            set_ship_state(conn, fuel_main=event["Total"])
    elif etype in ("RefuelAll", "RefuelPartial"):
        add_refuelled_fuel(conn, event.get("Amount"))
    elif etype == "Rank":
        set_status(
            conn,
            combat_rank=event.get("Combat"),
            trade_rank=event.get("Trade"),
            explore_rank=event.get("Explore"),
            cqc_rank=event.get("CQC"),
            federation_rank=event.get("Federation"),
            empire_rank=event.get("Empire"),
        )
    elif etype == "SquadronStartup":
        set_status(conn, squadron_name=event.get("SquadronName"), squadron_rank=event.get("CurrentRank"))
    elif etype == "Materials":
        reset_materials(conn, event)
    elif etype == "MaterialCollected":
        adjust_material(
            conn, event.get("Name"), event.get("Category"),
            event.get("Name_Localised"), event.get("Count", 0),
        )
    elif etype == "MaterialDiscarded":
        adjust_material(
            conn, event.get("Name"), event.get("Category"),
            event.get("Name_Localised"), -event.get("Count", 0),
        )
    elif etype == "MaterialTrade":
        paid, received = event.get("Paid") or {}, event.get("Received") or {}
        if paid:
            adjust_material(conn, paid.get("Material"), paid.get("Category"), paid.get("Material"), -paid.get("Quantity", 0))
        if received:
            adjust_material(conn, received.get("Material"), received.get("Category"), received.get("Material"), received.get("Quantity", 0))
    elif etype == "EngineerCraft":
        # NOT VERIFIED against a real file -- Ingredients items may or may not
        # carry a Category; passing None falls back to whatever category we
        # already have on record for that material (see adjust_material).
        for ing in event.get("Ingredients", []):
            adjust_material(conn, ing.get("Name"), ing.get("Category"), ing.get("Name_Localised"), -ing.get("Count", 0))
    elif etype == "EngineerContribution":
        if event.get("Type") == "Materials":
            adjust_material(conn, event.get("Material"), event.get("Category"), event.get("Material"), -event.get("Quantity", 0))
    elif etype == "Synthesis":
        for mat in event.get("Materials", []):
            adjust_material(conn, mat.get("Name"), mat.get("Category"), mat.get("Name_Localised"), -mat.get("Count", 0))
    elif etype in ("FSDJump", "Location"):
        # StarPos is the system's [x, y, z] in light years -- lets distance
        # lookups start from the journal instead of needing the current
        # system to also be in the EDSM populated-systems dump.
        pos = event.get("StarPos") or [None, None, None]
        set_status(
            conn,
            current_system=event.get("StarSystem"), current_station=None, docked=0,
            current_x=pos[0], current_y=pos[1], current_z=pos[2],
        )
        if etype == "FSDJump":
            record_fsd_jump(conn, event)
    elif etype == "Docked":
        set_status(conn, current_station=event.get("StationName"), docked=1)
    elif etype == "Undocked":
        set_status(conn, current_station=None, docked=0)
    elif etype == "EngineerProgress":
        # Startup snapshot has an "Engineers" list; later updates are a single
        # engineer's fields at the top level. "Progress" is Known/Invited/
        # Unlocked; "Rank" only exists once unlocked. An update that omits a
        # field must not blank what's already stored, hence the COALESCE.
        now = _now_iso()
        entries = event.get("Engineers") if "Engineers" in event else [event]
        for eng in entries:
            name = eng.get("Engineer")
            if not name:
                continue
            conn.execute(
                "INSERT INTO engineer_progress VALUES (?,?,?,?) "
                "ON CONFLICT(engineer_name) DO UPDATE SET "
                "rank_stage = COALESCE(excluded.rank_stage, rank_stage), "
                "rank_value = COALESCE(excluded.rank_value, rank_value), "
                "last_updated = excluded.last_updated",
                (name, eng.get("Progress"), eng.get("Rank"), now),
            )
        conn.commit()
    elif etype == "Loadout":
        set_loadout(conn, event)


def replay_journal(conn, path):
    print(f"Replaying {path.name} from the start...")
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partially-written last line while the game is live -- skip, tailing will pick it up whole later
            process_event(conn, event)
            count += 1
    print(f"Replay complete: {count} events processed.")


def follow_journal(conn, path):
    print(f"Tailing {path.name} for new events (checking every {POLL_INTERVAL_SECONDS}s)...")
    with open(path, "r", encoding="utf-8") as f:
        f.seek(0, 2)  # start at current end -- replay already covered everything before this
        while True:
            # a new session (game restart/crash) writes a NEW journal file rather
            # than appending to this one -- check for that each poll and switch
            latest = find_latest_journal(path.parent)
            if latest != path:
                print(f"New session detected: switching to {latest.name}")
                return latest

            line = f.readline()
            if not line:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            process_event(conn, event)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    build_schema(conn)

    path = find_latest_journal()
    replay_journal(conn, path)
    while True:
        path = follow_journal(conn, path)
        replay_journal(conn, path)  # new session -- replay it from scratch too


if __name__ == "__main__":
    main()
