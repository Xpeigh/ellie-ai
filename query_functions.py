"""
The query layer. Every function here returns data straight from SQLite --
the chat model's job is to pick the right function and phrase the
result, never to generate a material count or distance from its own
"knowledge." This file is the entire trust boundary for the project.

Precedence rule for blueprint lookups: your Excel sheet (graded_blueprints)
is checked first since it's the most specific and the one you maintain by
hand; Coriolis (coriolis_blueprints) is the fallback for anything the
sheet doesn't have filled in yet. See blueprint_mapping.py for why these
two don't share a join key directly.
"""
import functools
import json
import math
import re
import sqlite3
from pathlib import Path

from blueprint_mapping import MODULE_TO_CORIOLIS_KEYWORDS
from loadout_calc import compute_jump_range, compute_current_jump_range, apply_blueprint_delta

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")

# Friendly name -> Coriolis's own grp code (verified against every real
# modules/{standard,internal,hardpoints}/*.json file's grp values).
# A group not listed here can still be addressed by its raw grp code.
MODULE_GROUP_TO_GRP = {
    "frame shift drive": "fsd", "fsd": "fsd",
    "power plant": "pp", "power distributor": "pd",
    "thrusters": "t", "life support": "ls", "sensors": "s",
    "fuel tank": "ft",
    "shield generator": "sg", "bi-weave shield generator": "bsg",
    "prismatic shield generator": "psg", "shield cell bank": "scb",
    "shield booster": "sb",
    "hull reinforcement": "hr", "module reinforcement": "mrp",
    "cargo rack": "cr", "large cargo rack": "crl",
    "fuel scoop": "fs", "docking computer": "dc",
    "guardian fsd booster": "gfsb",
    "auto field-maintenance unit": "am", "afmu": "am",
    "refinery": "rf", "fighter hangar": "fh",
    "beam laser": "bl", "burst laser": "ul", "pulse laser": "pl",
    "cannon": "c", "multi-cannon": "mc", "fragment cannon": "fc",
    "rail gun": "rg", "plasma accelerator": "pa", "mine launcher": "nl",
    "missile rack": "mr", "torpedo pylon": "tp", "chaff launcher": "ch",
    "heat sink launcher": "hs", "point defence": "po",
}

# The 7 standard modules are always exactly one per ship, with a FIXED
# journal slot name regardless of ship type -- unlike optional-internal/
# hardpoint slots, there's no size-to-name ambiguity for these. Used only
# as a fallback when nothing of that group is currently fitted (R1) --
# the fitted-module lookup always runs first, so a ship with an internal
# fuel tank alongside its standard tank still gets a real ambiguity error
# instead of always landing on the standard slot. Names verified against
# the journal manual (elite-journal.readthedocs.io) and real Loadout events.
STANDARD_SLOT_NAMES = {
    "pp": "PowerPlant", "t": "MainEngines", "fsd": "FrameShiftDrive",
    "ls": "LifeSupport", "pd": "PowerDistributor", "s": "Radar", "ft": "FuelTank",
}

# Friendly display text for a standard grp, used only in "this slot only
# takes a <X>" messages (R5). Same 7 groups as STANDARD_SLOT_NAMES.
STANDARD_GRP_DISPLAY = {
    "pp": "power plant", "t": "thrusters", "fsd": "frame shift drive",
    "ls": "life support", "pd": "power distributor", "s": "sensors", "ft": "fuel tank",
}

# Order of ship_stats.slots_json["standard"], verified against every real
# ship file in coriolis-data/ships/*.json.
STANDARD_SLOTS_ORDER = ["pp", "t", "fsd", "ls", "pd", "s", "ft"]

# The 6 standard grps that are "core" modules for slot-kind purposes (S1)
# -- fuel tank (ft) is deliberately excluded: it's the one standard group
# that can ALSO be fitted in an ordinary internal slot, so it gets its own
# "fuel tank" kind rather than "core".
_CORE_GRPS = {"pp", "t", "fsd", "ls", "pd", "s"}

# journal current_loadout.ship_key -> ship_stats.ship_key, for the ships
# where the journal's internal name doesn't match Coriolis's directly.
# Each entry verified by reconstructing that ship's real UnladenMass
# exactly from Coriolis data -- except federation_corvette, verified by
# identity only (it's the only Corvette); Coriolis's mass for it is off
# by 12 t, which this tool doesn't depend on.
JOURNAL_SHIP_TO_CORIOLIS = {
    "belugaliner": "beluga", "cobramkiii": "cobra_mk_iii", "cutter": "imperial_cutter",
    "diamondbackxl": "diamondback_explorer", "empire_eagle": "imperial_eagle",
    "federation_corvette": "federal_corvette", "ferdelance": "fer_de_lance",
    "independant_trader": "keelback", "krait_light": "krait_phantom",
    "lakonminer": "type_11_prospector", "smallcombat01_nx": "kestrel",
    "type6": "type_6_transporter", "type7": "type_7_transport", "type8": "type_8_transport",
    "type9": "type_9_heavy",
}

# NN and M both captured -- S3's internal-slot existence checks need the
# slot NUMBER (NN), not just its advertised size (M).
_OPTIONAL_INTERNAL_SLOT_RE = re.compile(r"^Slot(\d+)_Size(\d+)$", re.IGNORECASE)
# Size word and index N both captured -- S1 splits Tiny (utility kind) from
# Small/Medium/Large/Huge (hardpoint kind); S3's existence check needs N.
_HARDPOINT_SLOT_RE = re.compile(r"^(Tiny|Small|Medium|Large|Huge)(?:Mining)?Hardpoint(\d+)$", re.IGNORECASE)
_HARDPOINT_MAX_CLASS = {"tiny": 0, "small": 1, "medium": 2, "large": 3, "huge": 4}

# On-item name patterns that draw no power at all and are never in
# module_stats (cosmetics/cockpits/armour are ship-cosmetic or
# bulkhead-only, not real modules) -- checked before a module_stats
# lookup so they're never mistaken for an unknown/unmodeled module.
_ZERO_DRAW_SUFFIXES = ("_cockpit",)
_ZERO_DRAW_PREFIXES = ("voicepack_", "paintjob_", "decal_", "nameplate_", "bobble_", "string_lights_")
_ZERO_DRAW_SUBSTRINGS = ("_armour_", "_shipkit", "customisation_")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _needs_setup(func):
    """If the table(s) a function needs don't exist yet -- setup.py hasn't
    been run, or only some of its steps have -- SQLite raises
    OperationalError. Without this, that would crash ellie_chat's tool-call
    loop instead of letting the model tell the commander plainly that the
    data isn't there yet. Any OTHER OperationalError (a real bug) still
    raises rather than being swallowed here."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            return {"found": False, "reason": f"Ellie's database isn't fully set up yet ({e}). Run `python setup.py`."}
    return wrapper


def _materials_to_dict(materials_json):
    """Normalizes both storage shapes (Excel's list-of-{qty,item} and
    Coriolis's dict-of-{name:qty}) into one {name: qty} dict."""
    materials = json.loads(materials_json)
    if isinstance(materials, dict):
        return materials
    return {m["item"]: m["qty"] for m in materials if m.get("item")}


@_needs_setup
def get_blueprint(module_name: str, upgrade_name: str, grade: int) -> dict:
    """Materials required for a specific module/upgrade/grade blueprint.
    Checks the Excel sheet first (most specific), falls back to Coriolis's
    broader category data if the sheet doesn't have this one filled in.
    Returns {"found": False} if neither source has it."""
    grade = int(grade)  # models don't always return args matching the declared type (e.g. "3" not 3)
    conn = _connect()

    row = conn.execute(
        "SELECT materials_json, modifiers_text FROM graded_blueprints "
        "WHERE lower(module_name) = lower(?) AND lower(upgrade) = lower(?) AND grade = ?",
        (module_name, upgrade_name, grade),
    ).fetchone()
    if row:
        conn.close()
        return {
            "found": True,
            "source": "excel",
            "module": module_name,
            "upgrade": upgrade_name,
            "grade": grade,
            "materials": _materials_to_dict(row["materials_json"]),
            "effect": row["modifiers_text"],
        }

    keywords = MODULE_TO_CORIOLIS_KEYWORDS.get(module_name)
    if keywords:
        candidates = conn.execute(
            "SELECT applies_to, materials_json, feature_ranges_json FROM coriolis_blueprints "
            "WHERE grade = ? AND lower(upgrade_name) = lower(?)",
            (grade, upgrade_name),
        ).fetchall()
        for c in candidates:
            if any(kw in c["applies_to"].lower() for kw in keywords):
                conn.close()
                return {
                    "found": True,
                    "source": "coriolis",
                    "module": module_name,
                    "upgrade": upgrade_name,
                    "grade": grade,
                    "materials": _materials_to_dict(c["materials_json"]),
                    "effect": None,  # Coriolis gives numeric feature ranges, not descriptive text
                }

    conn.close()
    return {"found": False, "module": module_name, "upgrade": upgrade_name, "grade": grade}


@_needs_setup
def get_experimental_effect(module_name: str, effect_name: str) -> dict:
    """Materials required for an experimental effect (ungraded -- single
    cost, no grade 1-5). Checks Excel's ungraded_costs first, falls back
    to Coriolis's coriolis_experimental_effects."""
    conn = _connect()

    row = conn.execute(
        "SELECT materials_json, modifiers_text FROM ungraded_costs "
        "WHERE lower(module_name) = lower(?) AND lower(effect) = lower(?)",
        (module_name, effect_name),
    ).fetchone()
    if row:
        conn.close()
        return {
            "found": True,
            "source": "excel",
            "module": module_name,
            "effect_name": effect_name,
            "materials": _materials_to_dict(row["materials_json"]),
            "description": row["modifiers_text"],
        }

    row = conn.execute(
        "SELECT name, description, materials_json FROM coriolis_experimental_effects "
        "WHERE lower(name) = lower(?)",
        (effect_name,),
    ).fetchone()
    conn.close()
    if row:
        return {
            "found": True,
            "source": "coriolis",
            "module": module_name,
            "effect_name": row["name"],
            "materials": _materials_to_dict(row["materials_json"]),
            "description": row["description"],
        }

    return {"found": False, "module": module_name, "effect_name": effect_name}


def _material_key(name):
    """Elite's journal stores materials as compact, space-free internal
    names ('exceptionalscrambledemissiondata'), while the Excel sheet and
    Coriolis use the human-readable display name with spaces
    ('Exceptional Scrambled Emission Data'). Lowercasing alone isn't
    enough -- strip spaces/hyphens too, to reach the same canonical key
    the journal watcher already uses when it writes materials_inventory."""
    return name.lower().replace(" ", "").replace("-", "")


@_needs_setup
def missing_materials(module_name: str, upgrade_name: str, grade: int) -> dict:
    """Diffs a blueprint's requirement against what's actually on hand
    right now (materials_inventory, kept current by journal_watcher.py)."""
    grade = int(grade)
    blueprint = get_blueprint(module_name, upgrade_name, grade)
    if not blueprint["found"]:
        result = {"found": False, "module": module_name, "upgrade": upgrade_name, "grade": grade}
        if "reason" in blueprint:  # e.g. the database isn't set up -- don't let that read as "no such blueprint"
            result["reason"] = blueprint["reason"]
        return result

    conn = _connect()
    missing, have_enough = {}, True
    for material, needed_qty in blueprint["materials"].items():
        row = conn.execute(
            "SELECT count FROM materials_inventory WHERE material_name = ?",
            (_material_key(material),),
        ).fetchone()
        on_hand = row["count"] if row else 0
        if on_hand < needed_qty:
            missing[material] = {"need": needed_qty, "have": on_hand, "short_by": needed_qty - on_hand}
            have_enough = False
    conn.close()

    return {
        "found": True,
        "module": module_name,
        "upgrade": upgrade_name,
        "grade": grade,
        "have_everything": have_enough,
        "missing": missing,
        "required": blueprint["materials"],
        "source": blueprint["source"],
    }


@_needs_setup
def get_engineer(name: str) -> dict:
    """An engineer's location, prerequisites, and what they unlock."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM engineers WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if not row:
        conn.close()
        return {"found": False, "name": name}

    result = dict(row)
    if row["engineer_type"] == "ship_module":
        unlocks = conn.execute(
            "SELECT grade, item FROM engineer_unlocks WHERE engineer_name = ? ORDER BY grade",
            (row["name"],),
        ).fetchall()
        result["unlocks_by_grade"] = {u["grade"]: u["item"] for u in unlocks}
    else:
        upgrades = conn.execute(
            "SELECT upgrade FROM personal_upgrades WHERE engineer_name = ?", (row["name"],)
        ).fetchall()
        result["upgrades"] = [u["upgrade"] for u in upgrades]

    conn.close()
    result["found"] = True
    return result


# Rank name tables, sourced from the documented journal manual (elite-journal.readthedocs.io/en/latest/Appendix)
# -- translated here, in code, rather than left for the model to recall from
# its own training data. That's the same "don't trust the model's memory"
# principle the whole project is built on, just applied to rank names too.
COMBAT_RANKS = ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert", "Master", "Dangerous", "Deadly", "Elite"]
TRADE_RANKS = ["Penniless", "Mostly Penniless", "Peddler", "Dealer", "Merchant", "Broker", "Entrepreneur", "Tycoon", "Elite"]
EXPLORE_RANKS = ["Aimless", "Mostly Aimless", "Scout", "Surveyor", "Explorer", "Pathfinder", "Ranger", "Pioneer", "Elite"]
CQC_RANKS = ["Helpless", "Mostly Helpless", "Amateur", "Semi Professional", "Professional", "Champion", "Hero", "Legend", "Elite"]
FEDERATION_RANKS = ["None", "Recruit", "Cadet", "Midshipman", "Petty Officer", "Chief Petty Officer", "Warrant Officer",
                     "Ensign", "Lieutenant", "Lt. Commander", "Post Commander", "Post Captain", "Rear Admiral",
                     "Vice Admiral", "Admiral"]
EMPIRE_RANKS = ["None", "Outsider", "Serf", "Master", "Squire", "Knight", "Lord", "Baron", "Viscount", "Count",
                "Earl", "Marquis", "Duke", "Prince", "King"]

# Frontier added "prestige" tiers beyond the original Elite cap (Odyssey
# era) for the four ladders that top out at "Elite" -- Combat, Trade,
# Exploration, CQC. The documented journal manual above doesn't cover
# this (it was written before/without that update), so this table isn't
# sourced the same verified way as the rest -- found via Ben's own real
# commander data (trade_rank: 9, which the base 9-entry table above has
# no slot for) plus Frontier's own forums confirming "Elite I" through
# "Elite V" exist as named prestige tiers. The exact journal INTEGER for
# each tier is inferred (the obvious sequential extension: 8=Elite,
# 9=Elite I, ..., 13=Elite V), not confirmed against an official source --
# worth re-checking if a commander at Elite II+ ever reports a name that
# looks off.
_ELITE_PLUS = {9: "Elite I", 10: "Elite II", 11: "Elite III", 12: "Elite IV", 13: "Elite V"}


def _rank_name(table, value):
    """table[value], with a fallback for the Elite-plus prestige tiers
    (see _ELITE_PLUS above) on the four ladders that actually end in
    "Elite" -- Federation/Empire end in "Admiral"/"King" and never hit
    this path. A value this function doesn't recognize is labeled
    honestly rather than guessed at or silently dropped."""
    if value is None:
        return None
    try:
        value = int(value)
    except (ValueError, TypeError):
        return None
    if 0 <= value < len(table):
        return table[value]
    if table[-1] == "Elite":
        if value in _ELITE_PLUS:
            return _ELITE_PLUS[value]
        if value > max(_ELITE_PLUS):
            return f"Elite (prestige rank {value}, beyond the confirmed Elite I-V range)"
    return None  # genuinely out of range for a ladder that doesn't extend (Federation/Empire)


def player_state() -> dict:
    """Current commander status plus a materials summary, as tracked live
    by journal_watcher.py. Ranks are translated to their real in-game
    names here (not left for the model to recall). Materials are returned
    as per-category COUNTS, not a full itemized list -- Ben's own testing
    showed a model paraphrasing a ~150-item list into "selected examples"
    and quietly getting several exact numbers wrong in the process. Use
    get_material_count() for a specific material's exact figure instead.

    Unlike the other functions here this has no "found" key on success, so
    it can't share the @_needs_setup decorator -- a missing table gets its
    own try/except below, returning the same {status, materials_summary}
    shape (both empty) plus an "error" key, rather than a shape the caller
    isn't expecting."""
    try:
        return _player_state()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return {"status": {}, "materials_summary": {}, "error": f"Ellie's database isn't fully set up yet ({e}). Run `python setup.py`."}


def _player_state() -> dict:
    conn = _connect()
    status_row = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
    status = dict(status_row) if status_row else {}
    status.pop("id", None)

    status["combat_rank_name"] = _rank_name(COMBAT_RANKS, status.get("combat_rank"))
    status["trade_rank_name"] = _rank_name(TRADE_RANKS, status.get("trade_rank"))
    status["explore_rank_name"] = _rank_name(EXPLORE_RANKS, status.get("explore_rank"))
    status["cqc_rank_name"] = _rank_name(CQC_RANKS, status.get("cqc_rank"))
    status["federation_rank_name"] = _rank_name(FEDERATION_RANKS, status.get("federation_rank"))
    status["empire_rank_name"] = _rank_name(EMPIRE_RANKS, status.get("empire_rank"))

    materials = conn.execute(
        "SELECT category, COUNT(*) as distinct_materials, SUM(count) as total_units "
        "FROM materials_inventory WHERE count > 0 GROUP BY category"
    ).fetchall()
    conn.close()

    materials_summary = {
        m["category"] or "Unknown": {"distinct_materials": m["distinct_materials"], "total_units": m["total_units"]}
        for m in materials
    }

    return {"status": status, "materials_summary": materials_summary}


@_needs_setup
def get_material_count(material_name: str) -> dict:
    """Exact on-hand count for ONE specific material, by exact name. Use
    this for "how much X do I have" instead of trying to read it off
    player_state()'s summary."""
    conn = _connect()
    row = conn.execute(
        "SELECT display_name, category, count FROM materials_inventory WHERE material_name = ?",
        (_material_key(material_name),),
    ).fetchone()
    conn.close()
    if not row:
        return {"found": False, "material_name": material_name}
    return {"found": True, "material_name": row["display_name"], "category": row["category"], "count": row["count"]}


@_needs_setup
def distance_to_system(system_name: str) -> dict:
    """Straight-line distance (light years) from the commander's CURRENT
    system to a named system. The origin's coordinates come from the
    journal (StarPos, stored by journal_watcher.py), so it works from any
    system, including unpopulated ones. The destination comes from EDSM,
    and edsm_systems only holds POPULATED systems -- an unpopulated
    destination is reported as not found, with that reason spelled out so
    it isn't mistaken for "doesn't exist"."""
    conn = _connect()

    current = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
    if not current or not current["current_system"]:
        conn.close()
        return {"found": False, "reason": "current system unknown -- journal_watcher may not be running"}

    # journal coordinates first; the EDSM lookup covers a database the
    # watcher hasn't migrated/updated yet (no coordinate columns, or NULL)
    if "current_x" in current.keys() and current["current_x"] is not None:
        origin = {"x": current["current_x"], "y": current["current_y"], "z": current["current_z"]}
    else:
        origin = conn.execute(
            "SELECT x, y, z FROM edsm_systems WHERE lower(name) = lower(?)", (current["current_system"],)
        ).fetchone()
    destination = conn.execute(
        "SELECT name, x, y, z FROM edsm_systems WHERE lower(name) = lower(?)", (system_name,)
    ).fetchone()
    conn.close()

    if not origin:
        return {"found": False, "reason": f"coordinates for current system '{current['current_system']}' unknown"}
    if not destination:
        return {
            "found": False,
            "reason": f"'{system_name}' not found -- the system data only covers populated systems, so it may be unpopulated",
        }

    distance = math.sqrt(
        (origin["x"] - destination["x"]) ** 2
        + (origin["y"] - destination["y"]) ** 2
        + (origin["z"] - destination["z"]) ** 2
    )
    return {
        "found": True,
        "from": current["current_system"],
        "to": destination["name"],
        "distance_ly": round(distance, 2),
    }


def _round2(value):
    return None if value is None else round(value, 2)


def _fitted_fsd_params(conn):
    """The fitted frame shift drive's jump-range inputs -- {"item", "optmass",
    "maxfuel", "fuelmul", "fuelpower", "booster_bonus"} -- or None when the
    FSD isn't in module_stats at all. Prefers the journal's own engineered
    FSDOptimalMass over the base value (ground truth for a module the
    commander really has fitted). booster_bonus is the flat ly bonus of a
    fitted Guardian FSD Booster, 0.0 when there is none.

    Any individual stat can still be None (a module_stats row missing a
    field): callers must treat that as "unknown jump range", never as 0."""
    fsd_row = conn.execute(
        "SELECT clm.item, clm.engineering_modifiers_json, ms.* FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE clm.slot = ?",
        (STANDARD_SLOT_NAMES["fsd"],),
    ).fetchone()
    if fsd_row is None:
        return None
    # N6: a missing base optmass must stay None (-> an honest "unknown jump
    # range"), never get routed through the NULL->0.0 rule that's correct for
    # mass/power but wrong here -- a ~0 optmass would give a jump range of
    # ~0, not "unknown".
    engineered_optmass = _engineered_modifier(fsd_row["engineering_modifiers_json"], "FSDOptimalMass")
    optmass = engineered_optmass if engineered_optmass is not None else new_module_extra_get(fsd_row, "optmass")
    booster_row = conn.execute(
        "SELECT ms.extra_json FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE ms.grp = 'gfsb'"
    ).fetchone()
    return {
        "item": fsd_row["item"],
        "optmass": optmass,
        "maxfuel": new_module_extra_get(fsd_row, "maxfuel"),
        "fuelmul": new_module_extra_get(fsd_row, "fuelmul"),
        "fuelpower": new_module_extra_get(fsd_row, "fuelpower"),
        "booster_bonus": (new_module_extra_get(booster_row, "jumpboost") if booster_row else None) or 0.0,
    }


def _fsd_params_complete(params):
    return params is not None and None not in (
        params["optmass"], params["maxfuel"], params["fuelmul"], params["fuelpower"],
    )


def _read_ship_state(conn):
    """The ship_state row (fuel/cargo/last-jump snapshot), or None if the
    table doesn't exist yet -- a database from before fuel tracking, where
    journal_watcher hasn't been restarted since the upgrade. That reads as
    "nothing tracked yet", not as the whole database being unset-up."""
    try:
        return conn.execute("SELECT * FROM ship_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return None


def _current_ship_state(conn, params):
    """(fuel_now, cargo_now, usable, state_note) for a current-load jump range.
    When the state is not usable, fuel_now and cargo_now are None too, so
    nothing downstream can relay a number we don't trust (e.g. a stale fuel
    level bigger than the tank), and state_note says why in words the
    commander can be told."""
    state = _read_ship_state(conn)
    fuel = state["fuel_main"] if state else None
    cargo = state["cargo_count"] if state else None
    capacity_row = conn.execute("SELECT fuel_main FROM current_loadout WHERE id = 1").fetchone()
    capacity = capacity_row["fuel_main"] if capacity_row else None

    if fuel is None:
        note = "current fuel isn't known yet (no refuel, jump or load event seen since the ship changed)"
    elif cargo is None:
        note = "current cargo isn't known yet (the game hasn't reported the cargo hold since the ship changed)"
    elif fuel < 0 or (capacity is not None and fuel > capacity + 1e-6):
        # Fuel outside [0, tank capacity] means the tracked level came from a
        # ship we've since left without seeing the change.
        note = "the tracked fuel level doesn't fit this ship's tank, so it's probably stale"
    elif not _fsd_params_complete(params):
        note = "the fitted frame shift drive isn't in Ellie's module data"
    else:
        return fuel, cargo, True, None
    return None, None, False, note


def _jump_check_warning(conn, params):
    """Honesty guard for the mass model behind the current-load range. The
    game burns FuelUsed = fuelmul * (JumpDist * M / optmass) ** fuelpower, so
    the last real jump tells us the ship's total mass M; if that disagrees
    with unladen + fuel before the jump + cargo by more than 1 t, Ellie's
    picture of the ship was wrong for that jump (normal jumps agree to about
    0.01 t, 97% within 0.5 t) and the commander is told so. Returns the
    warning sentence, or None -- also None, silently, when the last jump is
    missing a number the check needs, was boosted (injection changes the
    jump), or was made with a different drive than the one fitted now."""
    state = _read_ship_state(conn)
    if state is None or not _fsd_params_complete(params):
        return None
    dist, used = state["last_jump_dist"], state["last_jump_fuel_used"]
    if dist is None or used is None or dist <= 0 or used <= 0:
        return None
    if state["last_jump_boosted"] != 0:
        return None
    if None in (state["last_jump_cargo"], state["last_jump_unladen"], state["last_jump_fuel_before"]):
        return None
    if state["last_jump_fsd_item"] != params["item"]:
        return None
    implied = params["optmass"] * (used / params["fuelmul"]) ** (1 / params["fuelpower"]) / dist
    modeled = state["last_jump_unladen"] + state["last_jump_fuel_before"] + state["last_jump_cargo"]
    if abs(implied - modeled) <= 1.0:
        return None
    return f"Ellie's mass estimate for your last jump was off by {abs(implied - modeled):.1f} t, so these jump ranges may be inaccurate."


@_needs_setup
def get_current_loadout() -> dict:
    """The commander's real, current ship as captured from the journal's
    own Loadout event -- ship, mass, jump range, and every fitted module
    with its real engineering modifiers where present.

    Two jump ranges, never to be confused: jump_range_max is the game's
    own MaxJumpRange (empty hold, one jump of fuel); jump_range_now is
    computed here from the fuel and cargo journal_watcher has tracked, and
    is None (with state_note saying why) whenever those can't be trusted.
    summary is the same facts pre-written as one sentence, so a small model
    can relay it instead of re-deriving the numbers."""
    conn = _connect()
    try:
        status = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
        if not status or not status["ship_key"]:
            return {"found": False, "reason": "no Loadout event captured yet -- journal_watcher may not be running, or you haven't launched/undocked this session"}

        modules = conn.execute(
            "SELECT slot, item, is_on, priority, health, engineering_blueprint, "
            "engineering_level, engineering_quality, engineering_experimental, "
            "engineering_modifiers_json FROM current_loadout_modules ORDER BY slot"
        ).fetchall()
        params = _fitted_fsd_params(conn)
        fuel_now, cargo_now, usable, state_note = _current_ship_state(conn, params)
        warning = _jump_check_warning(conn, params)
        ship_row = _resolve_ship_stats(conn, status["ship_key"])
    finally:
        conn.close()

    module_list = []
    for m in modules:
        entry = {
            "slot": m["slot"], "item": m["item"], "on": bool(m["is_on"]),
            "health": m["health"],
        }
        if m["engineering_blueprint"]:
            entry["engineering"] = {
                "blueprint": m["engineering_blueprint"],
                "level": m["engineering_level"],
                "quality": m["engineering_quality"],
                "experimental_effect": m["engineering_experimental"],
                "modifiers": json.loads(m["engineering_modifiers_json"]) if m["engineering_modifiers_json"] else [],
            }
        module_list.append(entry)

    ship_name = ship_row["name"] if ship_row else status["ship_key"]
    unladen = status["unladen_mass"]
    jump_range_max = _round2(status["max_jump_range"])
    jump_range_now = mass_now = None
    if usable:
        jump_range_now = round(compute_current_jump_range(
            unladen, fuel_now, cargo_now, params["optmass"], params["maxfuel"],
            params["fuelmul"], params["fuelpower"], params["booster_bonus"],
        ), 2)
        mass_now = round(unladen + fuel_now + cargo_now, 2)
    # computed from the raw tracked values above; only what's reported is rounded
    fuel_now, cargo_now = _round2(fuel_now), _round2(cargo_now)

    if usable:
        summary = (
            f"{ship_name}: jump range now {jump_range_now} ly (fuel {fuel_now} t, cargo {cargo_now} t, "
            f"mass {mass_now} t); max {jump_range_max} ly with an empty hold and one jump of fuel."
        )
    else:
        summary = (
            f"{ship_name}: max jump range {jump_range_max} ly (empty hold, one jump of fuel). "
            f"Current-load jump range unavailable: {state_note}."
        )
    if warning:
        summary += " " + warning

    result = {
        "found": True,
        "ship": status["ship_key"],
        "ship_name": ship_name,
        "jump_range_now": jump_range_now,
        "jump_range_max": jump_range_max,
        "fuel_now": fuel_now,
        "cargo_now": cargo_now,
        "mass_now": mass_now,
        "unladen_mass": _round2(unladen),
        "cargo_capacity": status["cargo_capacity"],
        "fuel_tank_capacity": status["fuel_main"],
        "fuel_reservoir_capacity": status["fuel_reserve"],
        "state_note": state_note,
    }
    if warning:
        result["warning"] = warning
    result["summary"] = summary
    result["modules"] = module_list
    return result


@_needs_setup
def get_ship_info(ship_name: str) -> dict:
    """Base (unengineered) stats and slot layout for a named ship, from
    coriolis-data via ingest_ship_modules.py. Matches by display name
    (e.g. "Python") case-insensitively."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM ship_stats WHERE lower(name) = lower(?)", (ship_name,)
    ).fetchone()
    conn.close()
    if not row:
        return {"found": False, "ship_name": ship_name}
    return {
        "found": True,
        "name": row["name"],
        "manufacturer": row["manufacturer"],
        "hull_mass": row["hull_mass"],
        "slots": json.loads(row["slots_json"]),
    }


def _module_group_to_grp(module_group):
    key = module_group.strip().lower()
    if key in MODULE_GROUP_TO_GRP:
        return MODULE_GROUP_TO_GRP[key]
    # accept a raw grp code directly too (e.g. "fsd"), case-insensitively
    known_grps = set(MODULE_GROUP_TO_GRP.values())
    if key in known_grps:
        return key
    return None


def _resolve_explicit_slot_casing(conn, explicit_slot):
    """An explicit `slot` is matched case-insensitively (R5) -- against
    what's actually fitted first (the ground truth for real slot names),
    then against the 7 standard names, returning the REAL casing either
    way. A slot that matches neither (e.g. a genuinely empty utility
    mount nothing has ever been fitted to) is returned exactly as given;
    there's nothing to correct it against."""
    row = conn.execute(
        "SELECT slot FROM current_loadout_modules WHERE lower(slot) = lower(?)", (explicit_slot,)
    ).fetchone()
    if row:
        return row["slot"]
    for name in STANDARD_SLOT_NAMES.values():
        if name.lower() == explicit_slot.lower():
            return name
    return explicit_slot


def _resolve_target_slot(conn, ship_key, grp, explicit_slot):
    """Which slot a swap targets. An explicit slot always wins (casing
    corrected against reality, R5). Otherwise: resolve from what's
    CURRENTLY FITTED of that grp first (R1) -- this must run before the
    standard-slot fallback below, because a grp like `ft` covers both the
    one standard FuelTank slot AND any optional-internal fuel tanks, and
    a ship with both fitted is genuinely ambiguous, not automatically the
    standard slot. Only when NOTHING of that grp is fitted does a
    standard module (fsd/pp/t/ls/pd/s/ft) fall back to its one fixed
    journal slot name -- every ship has exactly one, so that's not a
    guess. Anything else with nothing fitted (optional internals,
    hardpoints, utility mounts) has no verified way to map Coriolis's
    slot-size layout to the journal's own slot names (see the plan's
    Review Focus #2 scope note) -- rather than guess, this asks for an
    explicit slot, the same honest treatment bulkheads already get."""
    if explicit_slot:
        return _resolve_explicit_slot_casing(conn, explicit_slot), None

    fitted = conn.execute(
        "SELECT clm.slot FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) "
        "WHERE ms.grp = ?", (grp,),
    ).fetchall()
    if len(fitted) == 1:
        return fitted[0]["slot"], None
    if len(fitted) > 1:
        return None, f"ship has {len(fitted)} slots of that type fitted -- specify which slot"
    if grp in STANDARD_SLOT_NAMES:
        return STANDARD_SLOT_NAMES[grp], None
    return None, "nothing of that type is currently fitted, so there's no slot to resolve automatically -- specify slot explicitly (e.g. an empty utility slot's name)"


def _resolve_ship_stats(conn, ship_key):
    """current_loadout.ship_key -> its ship_stats row: a direct match
    first, then through JOURNAL_SHIP_TO_CORIOLIS for the ships where the
    journal's internal name doesn't match Coriolis's own key."""
    if not ship_key:
        return None
    row = conn.execute("SELECT * FROM ship_stats WHERE lower(ship_key) = lower(?)", (ship_key,)).fetchone()
    if row:
        return row
    mapped = JOURNAL_SHIP_TO_CORIOLIS.get(ship_key.lower())
    if not mapped:
        return None
    return conn.execute("SELECT * FROM ship_stats WHERE lower(ship_key) = lower(?)", (mapped,)).fetchone()


def _classify_slot_kind(target_slot):
    """Which of the 4 recognized slot kinds target_slot is, or None for
    "anything else" (Military01, Cargo01, LimpetController01, FighterBay01,
    PlanetaryApproachSuite, Armour, ShipCockpit, CargoHatch, cosmetics, ...)
    -- those are a later extension (S1), never guessed at here. Names are
    already case-corrected by the time this is called (R5)."""
    if target_slot in STANDARD_SLOT_NAMES.values():
        return "standard"
    m = _HARDPOINT_SLOT_RE.match(target_slot)
    if m:
        return "utility" if m.group(1).lower() == "tiny" else "hardpoint"
    if _OPTIONAL_INTERNAL_SLOT_RE.match(target_slot):
        return "internal"
    return None


def _module_kind(symbol, grp, module_class):
    """Which of the 5 recognized module kinds a candidate module is,
    from its symbol prefix and class alone (S1) -- verified across all of
    module_stats: every hpt_ grp is consistently class-0-only (utility) or
    class->=1-only (weapon), so every candidate for one grp shares a kind,
    and this can be taken before any variant disambiguation."""
    if symbol.lower().startswith("hpt_"):
        return "utility" if module_class == 0 else "weapon"
    if grp in _CORE_GRPS:
        return "core"
    if grp == "ft":
        return "fuel tank"
    return "internal"


def _readable_module_name(module_group, grp):
    """A human-readable name for the module being fitted, for use in a
    rejection reason (T1) -- never the raw grp code (e.g. "sb", "t"),
    which reads as gibberish to the commander. STANDARD_GRP_DISPLAY for a
    core grp; else the caller's own module_group when that's already a
    recognized friendly name; else the first MODULE_GROUP_TO_GRP key that
    maps to this grp (covers a caller that passed the raw code)."""
    if grp in STANDARD_GRP_DISPLAY:
        return STANDARD_GRP_DISPLAY[grp]
    if module_group.strip().lower() in MODULE_GROUP_TO_GRP:
        return module_group
    for name, g in MODULE_GROUP_TO_GRP.items():
        if g == grp:
            return name
    return module_group


def _kind_mismatch_reason(module_group, grp, module_kind, slot_kind, target_slot):
    """None if module_kind is allowed in slot_kind/target_slot (S1's
    "Allowed pairs"), else a plain reason. Runs before variant
    disambiguation and before the size check -- an impossible pairing is
    impossible regardless of which specific variant or how big the slot
    turns out to be.

    NB1/T1: a core-module mismatch has TWO separate cases, kept in
    separate branches so neither can borrow the other's group -- naming
    the module being swapped IN when the real problem is which SLOT it's
    going into (or vice versa) tells the commander something false about
    his ship:
    - target_slot IS one of the 7 standard slots, just the WRONG one:
      name that slot's real owner (round 1's R5 case).
    - target_slot is any other kind entirely (internal/hardpoint/
      utility): name the module being swapped in -- there's no "wrong
      standard slot" to blame, it just doesn't belong outside its own."""
    if module_kind == "core":
        if target_slot == STANDARD_SLOT_NAMES[grp]:
            return None
        if slot_kind == "standard":
            owner_grp = next(g for g, name in STANDARD_SLOT_NAMES.items() if name == target_slot)
            return f"{target_slot} only takes a {STANDARD_GRP_DISPLAY[owner_grp]}"
        return f"a {STANDARD_GRP_DISPLAY[grp]} only fits the {STANDARD_SLOT_NAMES[grp]} slot"

    display = _readable_module_name(module_group, grp)
    if module_kind == "fuel tank":
        if slot_kind == "internal" or target_slot == STANDARD_SLOT_NAMES["ft"]:
            return None
        return f"a {display} only fits the standard fuel tank slot or an internal slot"
    if module_kind == "internal":
        if slot_kind == "internal":
            return None
        return f"a {display} only fits an internal slot"
    if module_kind == "weapon":
        if slot_kind == "hardpoint":
            return None
        return f"a {display} only fits a hardpoint mount"
    if module_kind == "utility":
        if slot_kind == "utility":
            return None
        return f"a {display} only fits a utility mount"
    return None


def _entry_class(entry):
    """A ship_stats slots_json hardpoints/internal entry's class -- a
    plain int, or a dict with a "class" key (a reserved/named slot)."""
    return entry if isinstance(entry, int) else entry.get("class")


def _hardpoint_slot_exists(ship_row, size_word, index):
    """S3: does the ship really have an Nth hardpoint of this size? Counts
    EVERY entry of that class in slots_json["hardpoints"], named or plain
    -- a reserved/named hardpoint still occupies a real slot of that size
    (verified: 3,021 real hardpoint slots, 0 violations)."""
    hardpoints = json.loads(ship_row["slots_json"]).get("hardpoints") or []
    target_class = _HARDPOINT_MAX_CLASS[size_word]
    count = sum(1 for e in hardpoints if _entry_class(e) == target_class)
    return index <= count


def _largest_plain_internal_class(ship_row):
    """S3(b): the largest ordinary (non-reserved) internal slot on this
    ship. Plain entries are ints, or dicts WITHOUT a "name" key -- named
    entries (Military, Cargo, Limpets, Fighter, PlanetaryApproachSuite)
    are reserved slots addressed by their own journal slot name, not by
    SlotNN_SizeM. Verified: 0 violations in 12,764 real internal slots."""
    internal = json.loads(ship_row["slots_json"]).get("internal") or []
    plain = [_entry_class(e) for e in internal if isinstance(e, int) or "name" not in e]
    return max(plain) if plain else None


def _internal_slot_collision(conn, nn):
    """S3(a): if a FITTED internal slot shares this NN under a different
    name, that fitted name is the real one -- the requested SlotNN_SizeM
    is wrong, not just unfitted. Verified: 0 repeated NN within one real
    loadout, across 12,764 internal slots."""
    for row in conn.execute("SELECT slot FROM current_loadout_modules").fetchall():
        m = _OPTIONAL_INTERNAL_SLOT_RE.match(row["slot"])
        if m and int(m.group(1)) == nn:
            return row["slot"]
    return None


def _slot_size_and_existence(conn, ship_row, ship_key, target_slot, slot_kind, old_module_row, old_stats):
    """The largest module class target_slot can hold, PLUS (for an
    explicit slot that isn't currently fitted) whether it plausibly
    exists at all (S3). Returns (max_class, caveat, early_return):
    early_return is a result dict to return immediately when set;
    otherwise max_class/caveat are valid and the caller proceeds.
    caveat is True only on the one path where existence was inferred,
    not confirmed (an unfitted internal slot that passed both checks).

    - Standard slots: sized from the slot's OWN owner grp (reverse
      STANDARD_SLOT_NAMES lookup), never from the swap's `grp` -- S2
      requires this to hold even if S1's kind check were absent. A
      standard slot always exists by definition, no S3 check needed.
      (Verified: 12,068 real-loadout checks, 0 violations.)
    - Hardpoint/utility, fitted: sized by the slot's own size word.
    - Hardpoint/utility, NOT fitted: S3 checks the ship really has an
      Nth hardpoint of that size before sizing it.
    - Internal, fitted: M, or the fitted module's class if that's larger
      (the one real exception -- the Keelback's Slot03_Size3 holding a
      class-4 cargo rack -- falls out of this generic rule).
    - Internal, NOT fitted: S3's number-collision and plain-max-size
      checks (Review Focus #2 -- empty internal slots aren't otherwise
      verifiable at all); passing both adds the caveat.
    """
    if slot_kind == "standard":
        owner_grp = next(g for g, name in STANDARD_SLOT_NAMES.items() if name == target_slot)
        if not ship_row:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s slot layout, so the fit can't be checked"}
        standard_sizes = json.loads(ship_row["slots_json"]).get("standard") or []
        if len(standard_sizes) != len(STANDARD_SLOTS_ORDER):
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s standard slot sizes, so the fit can't be checked"}
        return standard_sizes[STANDARD_SLOTS_ORDER.index(owner_grp)], False, None

    if slot_kind in ("hardpoint", "utility"):
        m = _HARDPOINT_SLOT_RE.match(target_slot)
        size_word, index = m.group(1).lower(), int(m.group(2))
        max_class = _HARDPOINT_MAX_CLASS[size_word]
        if not old_module_row:
            if not ship_row:
                return None, False, {"found": False, "reason": f"don't know {ship_key}'s hardpoint layout, so the fit can't be checked"}
            if not _hardpoint_slot_exists(ship_row, size_word, index):
                ship_name = ship_row["name"] or ship_key
                return None, False, {
                    "found": True, "compatible": False, "slot": target_slot,
                    "reason": f"your {ship_name} has no {target_slot}",
                }
        return max_class, False, None

    if slot_kind == "internal":
        m = _OPTIONAL_INTERNAL_SLOT_RE.match(target_slot)
        nn, size_m = int(m.group(1)), int(m.group(2))
        if old_module_row:
            max_class = size_m
            if old_stats and old_stats["class"] and old_stats["class"] > max_class:
                max_class = old_stats["class"]
            return max_class, False, None

        collision = _internal_slot_collision(conn, nn)
        if collision:
            return None, False, {"found": False, "reason": f"your ship's slot {nn} is {collision}"}
        if not ship_row:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s internal slot layout, so the fit can't be checked"}
        largest_plain = _largest_plain_internal_class(ship_row)
        if largest_plain is None:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s internal slot layout, so the fit can't be checked"}
        if size_m > largest_plain:
            ship_name = ship_row["name"] or ship_key
            return None, False, {
                "found": True, "compatible": False, "slot": target_slot,
                "reason": f"your {ship_name} has no size-{size_m} internal slot",
            }
        return size_m, True, None  # caveat: existence inferred, not confirmed

    return None, False, {"found": False, "reason": f"don't recognize '{target_slot}''s size, so the fit can't be checked"}


def _coerce_int_in_range(value, lo, hi, label):
    """Accepts an int, a whole-number float (5.0 -> 5, T2/NB2), or a
    whole-number string (N4/S5 -- a small model can easily pass
    module_class="5"); anything else -- a genuinely fractional value like
    5.5, or unparseable garbage -- or an out-of-range value, is a plain
    reason, never a TypeError from a later comparison."""
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None, f"'{value}' isn't a valid {label}"
            n = int(value)
        else:
            n = int(str(value).strip())
    except (ValueError, TypeError):
        return None, f"'{value}' isn't a valid {label}"
    if not (lo <= n <= hi):
        return None, f"{label} {n} is out of range ({lo}-{hi})"
    return n, None


def _engineered_modifier(modifiers_json, label):
    """One journal Engineering.Modifiers entry's real Value by Label, or
    None if there's no Engineering block or no modifier with that label."""
    if not modifiers_json:
        return None
    for mod in json.loads(modifiers_json):
        if mod.get("Label") == label:
            return mod.get("Value")
    return None


def _engineered_or_base(modifiers_json, label, base_value):
    """The journal's real engineered value for `label` if present (ground
    truth for a module the commander actually has fitted), else the base
    module_stats value -- NULL there means 0.0 (R2): Coriolis omits
    zero-valued fields, never treat a known module as massless/powerless
    just because that field is absent."""
    value = _engineered_modifier(modifiers_json, label)
    return value if value is not None else (base_value or 0.0)


def new_module_extra_get(module_row, key):
    """extra_json[key] for a module_stats row, or None if the row/key is
    absent. Small helper so the fsd-stat lookups above stay readable."""
    if module_row is None or module_row["extra_json"] is None:
        return None
    return json.loads(module_row["extra_json"]).get(key)


def _min_max_or_single(lo, hi, ndigits=2):
    """A genuine roll-dependent range as {"min", "max"}; collapses to one
    plain rounded number when lo == hi (deterministic) -- matches the
    spec's stated result shape and Review Focus #5: never present a
    fabricated spread when the real answer is exact."""
    lo_r, hi_r = round(lo, ndigits), round(hi, ndigits)
    return lo_r if lo_r == hi_r else {"min": lo_r, "max": hi_r}


_MOUNT_WORDS = {"F": "fixed", "G": "gimballed", "T": "turret"}


def _variant_search_text(module_row):
    """What a `variant` string is matched against: the symbol, Coriolis's
    display name, and the mount spelled out -- so "gimballed", "gimbal",
    "sco", "mk ii", "guardian" or an exact symbol all work."""
    extra = json.loads(module_row["extra_json"] or "{}")
    name = extra.get("name") or extra.get("ukName") or ""
    mount = _MOUNT_WORDS.get(extra.get("mount"), "")
    return f"{module_row['symbol']} {name} {mount}".lower()


def _describe_variant(module_row):
    extra = json.loads(module_row["extra_json"] or "{}")
    return {
        "symbol": module_row["symbol"],
        "name": extra.get("name") or extra.get("ukName"),
        "mount": _MOUNT_WORDS.get(extra.get("mount")),
    }


def _draws_nothing(item_lower):
    """Cockpits/armour/cosmetics are ship-cosmetic or bulkhead-only slots,
    never present in module_stats -- checked by name so they're never
    mistaken for an unknown module in the power-margin sum (R7)."""
    if item_lower.endswith(_ZERO_DRAW_SUFFIXES):
        return True
    if item_lower.startswith(_ZERO_DRAW_PREFIXES):
        return True
    return any(s in item_lower for s in _ZERO_DRAW_SUBSTRINGS)


def _power_draw_total(conn, target_slot):
    """Sum of power draw for every currently-On module except the one in
    target_slot (that one's being removed -- R5's "a removed module
    contributes nothing" applies to power too, not just the FSD
    booster's jump bonus). Hardpoints included; the cargo hatch counts as
    a flat 0.6 MW when On (R7); a module's own real engineered PowerDraw
    is preferred over its base module_stats value, consistent with this
    project's global "prefer the journal's ground truth" rule. Returns
    (total, unknown_items): unknown_items is non-empty when an On module
    outside the zero-draw/cargo-hatch categories isn't in module_stats --
    the caller must treat the margin as unknowable, never silently
    omit it (R7)."""
    rows = conn.execute(
        "SELECT slot, item, engineering_modifiers_json FROM current_loadout_modules "
        "WHERE is_on = 1 AND slot != ?", (target_slot,),
    ).fetchall()
    total = 0.0
    unknown = []
    for row in rows:
        item_lower = row["item"].lower()
        if item_lower.startswith("modularcargobaydoor"):
            total += 0.6
            continue
        if _draws_nothing(item_lower):
            continue
        engineered = _engineered_modifier(row["engineering_modifiers_json"], "PowerDraw")
        if engineered is not None:
            total += engineered
            continue
        stats = conn.execute("SELECT power FROM module_stats WHERE symbol = ?", (item_lower,)).fetchone()
        if stats is None:
            unknown.append(row["item"])
            continue
        total += stats["power"] or 0.0
    return total, unknown


def _effective_plant_output(conn, target_slot, new_module, features):
    """(min, max) power plant output in MW. If the plant itself is being
    swapped (the TARGET SLOT is the standard PowerPlant slot -- S2: keyed
    on the slot, not on `grp`, so this stays correct even if a kind-check
    elsewhere had a gap), that's the new module's pgen (+ the blueprint's
    pgen delta, if one applies); otherwise it's the CURRENTLY fitted
    plant's real reported output -- the journal's own PowerCapacity if
    it's engineered (ground truth), else its base module_stats pgen.
    Returns (None, None) when the currently-fitted plant can't be
    resolved at all, so the caller reports an honestly-unknown margin
    instead of a guess."""
    if target_slot == STANDARD_SLOT_NAMES["pp"]:
        base = new_module_extra_get(new_module, "pgen") or 0.0
        if "pgen" in features:
            return apply_blueprint_delta(base, features["pgen"])
        return base, base

    plant_row = conn.execute(
        "SELECT clm.engineering_modifiers_json, ms.extra_json FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE clm.slot = ?",
        (STANDARD_SLOT_NAMES["pp"],),
    ).fetchone()
    if not plant_row:
        return None, None
    capacity = _engineered_modifier(plant_row["engineering_modifiers_json"], "PowerCapacity")
    if capacity is not None:
        return capacity, capacity
    pgen = new_module_extra_get(plant_row, "pgen") or 0.0
    return pgen, pgen


@_needs_setup
def simulate_module_swap(module_group: str, module_class: int, module_rating: str,
                          blueprint_name: str = None, grade: int = None, slot: str = None,
                          variant: str = None) -> dict:
    """'What if I fit X instead' -- swaps one module in the commander's
    CURRENT loadout for a hypothetical one and reports the real resulting
    mass/jump-range/power deltas. Never invents a number: an
    unrecognized module group, an incompatible class/rating, an
    impossible slot/module pairing, or a blueprint that doesn't apply to
    this module category all return a plain not-found/incompatible
    result instead."""
    grp = _module_group_to_grp(module_group)
    if grp is None:
        return {"found": False, "reason": f"'{module_group}' isn't a recognized module type"}

    # N4/S5: a small model can easily pass "5" instead of 5 -- accept
    # either (or a whole-number float, T2/NB2), reject anything else (or
    # out-of-range) with a plain reason rather than a TypeError from a
    # later numeric comparison. `grade` is deliberately NOT coerced/
    # validated here -- it's only meaningful when a blueprint is actually
    # requested (T2/NB2: a plain swap with grade=0, a 3B model's plausible
    # way of saying "no grade", must be ignored exactly as it was before
    # this validation existed, not rejected as "out of range").
    module_class, class_error = _coerce_int_in_range(module_class, 0, 8, "module class")
    if class_error:
        return {"found": False, "reason": class_error}

    conn = _connect()
    try:
        baseline = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
        if not baseline or not baseline["ship_key"]:
            return {"found": False, "reason": "no current loadout captured yet -- journal_watcher may not be running"}

        target_slot, slot_error = _resolve_target_slot(conn, baseline["ship_key"], grp, slot)
        if slot_error:
            return {"found": False, "reason": slot_error}

        # S1: classify the target slot by name. "Anything else" (Military01,
        # Cargo01, LimpetController01, FighterBay01, PlanetaryApproachSuite,
        # Armour, ShipCockpit, CargoHatch, cosmetics, ...) is a later
        # extension -- fail closed exactly as an unrecognized slot size did
        # before this, never guess.
        slot_kind = _classify_slot_kind(target_slot)
        if slot_kind is None:
            return {"found": False, "reason": f"Ellie can't simulate swaps in {target_slot} yet"}

        # Several real modules can share one group/class/rating (standard vs SCO
        # FSDs, fixed/gimballed/turret mounts, standard vs Guardian power plants
        # -- 100 such combinations in coriolis-data, verified). Their stats differ
        # materially, so never pick one arbitrarily: narrow by `variant` if given,
        # otherwise list the candidates and ask, the same honest treatment an
        # ambiguous slot gets. raw_candidates (before the variant filter) is
        # also what S1's kind check uses -- every candidate for one grp shares
        # a kind, so this is valid before any variant disambiguation.
        raw_candidates = conn.execute(
            "SELECT * FROM module_stats WHERE grp = ? AND class = ? AND lower(rating) = lower(?)",
            (grp, module_class, module_rating),
        ).fetchall()

        # S1: does this module kind even fit this slot kind? Runs before the
        # variants ambiguity response and before the size check -- "a beam
        # laser in an internal slot" is wrong whichever mount is meant, so
        # don't make the user pick a mount first.
        if raw_candidates:
            module_kind = _module_kind(raw_candidates[0]["symbol"], grp, module_class)
            mismatch = _kind_mismatch_reason(module_group, grp, module_kind, slot_kind, target_slot)
            if mismatch:
                return {"found": True, "compatible": False, "slot": target_slot, "reason": mismatch}

        candidates = raw_candidates
        if variant:
            wanted = variant.strip().lower()
            exact = [c for c in candidates if c["symbol"] == wanted]
            candidates = exact or [c for c in candidates if wanted in _variant_search_text(c)]
        if not candidates:
            suffix = f" matching '{variant}'" if variant else ""
            return {"found": False, "reason": f"no {module_group} of class {module_class} rating {module_rating}{suffix} exists"}
        if len(candidates) > 1:
            return {
                "found": False,
                "reason": f"{len(candidates)} different modules are {module_group} class {module_class} rating {module_rating} -- specify which with `variant`",
                "variants": [_describe_variant(c) for c in candidates],
            }
        new_module = candidates[0]

        old_module_row = conn.execute(
            "SELECT * FROM current_loadout_modules WHERE slot = ?", (target_slot,)
        ).fetchone()
        old_stats = conn.execute(
            "SELECT * FROM module_stats WHERE symbol = ?",
            (old_module_row["item"].lower() if old_module_row else None,),
        ).fetchone() if old_module_row else None
        if old_module_row and not old_stats:
            # R2: never treat an unrecognized fitted module as massless --
            # refuse honestly instead of fabricating a "0" for it.
            return {
                "found": False,
                "reason": f"the module currently in {target_slot} ({old_module_row['item']}) isn't in Ellie's module data, so the change can't be computed",
            }

        # S2/S3: size the slot from the SLOT, not from `grp` -- and, for an
        # explicit slot that isn't currently fitted, check it plausibly
        # exists at all (Review Focus #2's scope note: empty slots are
        # otherwise unverifiable). caveat is True only when existence was
        # inferred rather than confirmed (an unfitted internal slot).
        ship_row = _resolve_ship_stats(conn, baseline["ship_key"])
        max_class, caveat, early_return = _slot_size_and_existence(
            conn, ship_row, baseline["ship_key"], target_slot, slot_kind, old_module_row, old_stats
        )
        if early_return:
            return early_return
        if module_class > max_class:
            return {
                "found": True, "compatible": False, "slot": target_slot,
                "reason": f"a class {module_class} {module_group} doesn't fit {target_slot} (max class {max_class})",
            }

        new_mass_min = new_mass_max = new_module["mass"] or 0.0
        new_power_min = new_power_max = new_module["power"] or 0.0
        blueprint_note = None
        features = {}
        unmodeled_keys = []

        if blueprint_name:
            # grade is only validated HERE, not up front (T2/NB2) -- it's
            # meaningless without a blueprint, so an unvalidated grade=0
            # from a caller that never asked for a blueprint must be
            # ignored, not rejected.
            if grade is not None:
                grade, grade_error = _coerce_int_in_range(grade, 1, 5, "grade")
                if grade_error:
                    return {"found": False, "reason": grade_error}
            if not grade:
                return {"found": False, "reason": "a blueprint grade (1-5) is required"}

            # S4: eligibility comes from Coriolis's own per-group allow-list
            # (modifications/modules.json, ingested into module_blueprints by
            # ingest_ship_modules.py), NOT a category-text match -- Coriolis's
            # applies_to text is far coarser than which blueprints actually
            # exist for a group (e.g. "Weapon" covers every weapon grp, but a
            # torpedo pylon only really has LightWeight/Sturdy; a multi-cannon
            # and a beam laser can both match "Overcharged" under two DIFFERENT,
            # differently-featured blueprint definitions).
            allow_list_count = conn.execute(
                "SELECT COUNT(*) FROM module_blueprints WHERE grp = ?", (grp,)
            ).fetchone()[0]
            if allow_list_count == 0:
                return {"found": False, "reason": f"no engineering blueprints exist for {module_group} in Ellie's data"}

            bp_rows = conn.execute(
                "SELECT cb.fdname, cb.feature_ranges_json FROM coriolis_blueprints cb "
                "JOIN module_blueprints mb ON mb.fdname = cb.fdname "
                "WHERE mb.grp = ? AND cb.grade = ? AND lower(cb.upgrade_name) = lower(?)",
                (grp, grade, blueprint_name),
            ).fetchall()
            if not bp_rows:
                return {"found": False, "reason": f"'{blueprint_name}' grade {grade} doesn't apply to {module_group}"}
            if len(bp_rows) > 1:
                # Never take the first row. Not reachable with today's data
                # (verified: 0 upgrade_name collisions within any grp's
                # allow-list at any grade) -- this guards future data.
                return {"found": False, "reason": f"multiple blueprint definitions match '{blueprint_name}' grade {grade} for {module_group} -- Ellie's data needs a closer look before this can be applied safely"}

            features = json.loads(bp_rows[0]["feature_ranges_json"])
            applied_keys = set()
            if "mass" in features:
                new_mass_min, new_mass_max = apply_blueprint_delta(new_module["mass"] or 0.0, features["mass"])
                applied_keys.add("mass")
            if "power" in features:
                new_power_min, new_power_max = apply_blueprint_delta(new_module["power"] or 0.0, features["power"])
                applied_keys.add("power")
            if target_slot == STANDARD_SLOT_NAMES["fsd"] and "optmass" in features:
                applied_keys.add("optmass")  # applied separately below, in the jump-range section
            if target_slot == STANDARD_SLOT_NAMES["pp"] and "pgen" in features:
                applied_keys.add("pgen")  # applied separately below, in the power-margin section
            blueprint_note = blueprint_name
            # honesty check: a features key this code doesn't know how to apply
            # (e.g. a weapon's "damage"/"rof") must be reported, never silently
            # dropped -- the numbers below would otherwise look complete while
            # missing a real part of the blueprint's effect.
            unmodeled_keys = sorted(set(features) - applied_keys)

        # R2: the removed module's REAL mass/power -- the journal's own
        # engineered Mass/PowerDraw if present (ground truth), else the base
        # module_stats value (NULL there means 0.0, never "unknown").
        if old_module_row:
            old_mass = _engineered_or_base(old_module_row["engineering_modifiers_json"], "Mass", old_stats["mass"])
            old_power = _engineered_or_base(old_module_row["engineering_modifiers_json"], "PowerDraw", old_stats["power"])
        else:
            old_mass = old_power = 0.0

        mass_change_min = new_mass_min - old_mass
        mass_change_max = new_mass_max - old_mass
        new_unladen_min = baseline["unladen_mass"] + mass_change_min
        new_unladen_max = baseline["unladen_mass"] + mass_change_max
        power_change_min = new_power_min - old_power
        power_change_max = new_power_max - old_power

        # jump range needs the ship's EFFECTIVE fsd stats -- if the FSD itself
        # is the slot being swapped (S2: keyed on the TARGET SLOT, not `grp`
        # -- after S1 the two agree, but the slot is what's actually true,
        # and this doesn't depend on S1 being in place), that's the new
        # module (+ blueprint, if given); otherwise it's the CURRENT fsd's
        # real reported stats (preferring the journal's own engineering
        # values over recomputed blueprint math -- see Global Constraints).
        if target_slot == STANDARD_SLOT_NAMES["fsd"]:
            fsd_optmass = new_module_extra_get(new_module, "optmass")
            fsd_maxfuel = new_module_extra_get(new_module, "maxfuel")
            fsd_fuelmul = new_module_extra_get(new_module, "fuelmul")
            fsd_fuelpower = new_module_extra_get(new_module, "fuelpower")
            if "optmass" in features:
                optmass_min, optmass_max = apply_blueprint_delta(fsd_optmass, features["optmass"])
            else:
                optmass_min = optmass_max = fsd_optmass
        else:
            fitted_fsd = _fitted_fsd_params(conn)
            fsd_maxfuel = fitted_fsd["maxfuel"] if fitted_fsd else None
            fsd_fuelmul = fitted_fsd["fuelmul"] if fitted_fsd else None
            fsd_fuelpower = fitted_fsd["fuelpower"] if fitted_fsd else None
            optmass_min = optmass_max = fitted_fsd["optmass"] if fitted_fsd else None

        # Guardian FSD Booster: a flat additive jump-range bonus (loadout_calc.compute_jump_range's
        # booster_bonus parameter -- see that function's own docstring for the honest caveat that
        # this addition, unlike the base formula, isn't cross-verified against a real boosted ship).
        # Stays keyed on `grp` (S2): a booster works from any internal slot, so there's no separate
        # "target slot" to prefer here. Resolved the same way as the effective FSD stats above: if
        # the booster ITSELF is the slot being swapped, use the new module's jumpboost; otherwise use
        # whatever booster (if any) is currently fitted EXCLUDING the target slot (R5) -- a booster
        # being removed contributes nothing, even though its row is still in current_loadout_modules
        # until this swap happens.
        if grp == "gfsb":
            booster_bonus = new_module_extra_get(new_module, "jumpboost") or 0.0
        else:
            booster_row = conn.execute(
                "SELECT ms.extra_json FROM current_loadout_modules clm "
                "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE ms.grp = 'gfsb' AND clm.slot != ?",
                (target_slot,),
            ).fetchone()
            booster_bonus = (new_module_extra_get(booster_row, "jumpboost") if booster_row else None) or 0.0

        if None in (fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, optmass_min):
            new_range_min = new_range_max = None
        else:
            new_range_min = compute_jump_range(new_unladen_max, optmass_min, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)
            new_range_max = compute_jump_range(new_unladen_min, optmass_max, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)

        # R7: power margin on a DEPLOYED basis -- effective plant output minus
        # every currently-On module's draw (target slot's old module excluded,
        # the new module's draw added instead), hardpoints and the cargo
        # hatch's flat 0.6 MW included. An On module this code can't identify
        # makes the margin honestly unknown, never a guess.
        plant_output_min, plant_output_max = _effective_plant_output(conn, target_slot, new_module, features)
        draw_total, unknown_power_items = _power_draw_total(conn, target_slot)
        if plant_output_min is None or unknown_power_items:
            new_power_margin = None
        else:
            margin_min = plant_output_min - (draw_total + new_power_max)
            margin_max = plant_output_max - (draw_total + new_power_min)
            new_power_margin = _min_max_or_single(margin_min, margin_max)

        result = {
            "found": True,
            "compatible": True,
            "slot": target_slot,
            "removed": {"item": old_module_row["item"] if old_module_row else None},
            "added": {
                "symbol": new_module["symbol"], "class": module_class, "rating": module_rating,
                "blueprint": blueprint_note,
                "mass": _min_max_or_single(new_mass_min, new_mass_max),
                "power": _min_max_or_single(new_power_min, new_power_max),
            },
            "mass_change": _min_max_or_single(mass_change_min, mass_change_max),
            "new_unladen_mass": _min_max_or_single(new_unladen_min, new_unladen_max),
            "new_jump_range": _min_max_or_single(new_range_min, new_range_max) if new_range_min is not None else None,
            "power_change": _min_max_or_single(power_change_min, power_change_max),
            "new_power_margin": new_power_margin,
            "power_unknown_modules": unknown_power_items,  # honestly-reported, never silently dropped
            "unmodeled_blueprint_effects": unmodeled_keys,  # honestly-reported, never silently dropped
        }
        if caveat:
            # S3(c): only on the "unfitted internal slot, existence inferred
            # not confirmed" path -- Task 6 relays this to the model.
            result["caveat"] = (
                f"{target_slot} isn't fitted in your current loadout, so Ellie can't confirm "
                "it's an empty slot on your ship -- these numbers assume it is"
            )
        return result
    finally:
        conn.close()
