"""
Ingests coriolis-data's ships/*.json and modules/{standard,internal,hardpoints}/*.json
into ship_stats and module_stats -- the base-game (unengineered) stats the
ship-loadout calculator needs for mass/jump-range/power-budget math. Same
convention as ingest_coriolis.py: only drops/recreates its own two tables,
no new download (coriolis-data is already cloned for blueprint ingestion),
safe to rerun any time.

CORE_FIELDS below are pulled into their own columns; everything else in a
module's dict is kept verbatim in extra_json -- including group-specific
fields like an FSD's optmass/fuelmul/fuelpower/maxfuel, or a power plant's
real output field, which is 'pgen', NOT 'power' (power plants have no
'power' field at all -- verified against modules/standard/power_plant.json
before this was written; every *consuming* module family uses 'power' for
draw, only the power plant itself is the odd one out).
"""
import json
import sqlite3
import sys
from pathlib import Path

import blueprint_mapping as bm

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_PATH = sys.argv[1] if len(sys.argv) > 1 else str(SCRIPT_DIR / "coriolis-data")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else str(SCRIPT_DIR / "elite-dangerous.db")

MODULE_DIRS = ["standard", "internal", "hardpoints"]
_CORE_FIELDS = {"symbol", "id", "edID", "grp", "class", "rating", "mass", "power", "cost"}


def require_inputs(repo_path):
    """Check, before the database is touched, that every input this ingest reads
    exists: the ships/ and modules/{standard,internal,hardpoints}/ folders (each
    holding .json files) and modifications/modules.json. Raises FileNotFoundError
    naming the missing path -- a half-cloned coriolis-data must fail here, not
    after the old tables have been dropped."""
    repo = Path(repo_path)
    for folder in [repo / "ships"] + [repo / "modules" / sub for sub in MODULE_DIRS]:
        if not folder.is_dir():
            raise FileNotFoundError(f"coriolis-data input folder not found: {folder}")
        if not any(folder.glob("*.json")):
            raise FileNotFoundError(f"coriolis-data input folder has no .json files: {folder}")
    allow_list = repo / "modifications" / "modules.json"
    if not allow_list.is_file():
        raise FileNotFoundError(f"coriolis-data blueprint allow-list not found: {allow_list}")


def build_schema(conn, commit=True):
    # Individual statements, not executescript(): executescript commits any open
    # transaction first, which would break ingest_all's single transaction.
    conn.execute("DROP TABLE IF EXISTS ship_stats")
    conn.execute("DROP TABLE IF EXISTS module_stats")
    conn.execute(
        """
        CREATE TABLE ship_stats (
            ship_key TEXT PRIMARY KEY, ed_id INTEGER, name TEXT, manufacturer TEXT,
            hull_mass REAL, properties_json TEXT, slots_json TEXT, defaults_json TEXT,
            bulkheads_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE module_stats (
            symbol TEXT PRIMARY KEY, coriolis_id TEXT, ed_id INTEGER, grp TEXT,
            class INTEGER, rating TEXT, mass REAL, power REAL, cost INTEGER,
            extra_json TEXT
        )
        """
    )
    # module_blueprints is CREATE TABLE IF NOT EXISTS, not DROP+CREATE like
    # the two tables above -- unlike ship_stats/module_stats, an install
    # that already has coriolis_blueprints skips setup_coriolis() entirely
    # (see setup.py), so this ingest script is the only place an EXISTING
    # install ever gets this table. Dropping it here would be harmless on
    # a fresh install but would defeat that upgrade path on an existing one.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS module_blueprints (
            grp TEXT, fdname TEXT, PRIMARY KEY (grp, fdname)
        )
        """
    )
    # Two more IF NOT EXISTS tables, for the same reason as module_blueprints: an
    # install that has the older tables gets these from this script, never lost to a DROP.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS module_group_names (
            grp TEXT, name TEXT, PRIMARY KEY (grp, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS module_specials (
            grp TEXT, edname TEXT, PRIMARY KEY (grp, edname)
        )
        """
    )
    # Rebuilt on every ingest, so DROP+CREATE: it also replaces the first version of this table, which had no
    # `grp` column and so merged the engineer lists of different module groups.
    conn.execute("DROP TABLE IF EXISTS blueprint_engineers")
    conn.execute(
        """
        CREATE TABLE blueprint_engineers (
            grp TEXT, fdname TEXT, grade INTEGER, engineer TEXT, PRIMARY KEY (grp, fdname, grade, engineer)
        )
        """
    )
    if commit:
        conn.commit()


def build_ships(conn, repo_path, commit=True):
    ships_dir = Path(repo_path) / "ships"
    if not ships_dir.is_dir():
        raise FileNotFoundError(f"coriolis-data input folder not found: {ships_dir}")
    count = 0
    for path in sorted(ships_dir.glob("*.json")):
        data = json.load(open(path, encoding="utf-8"))
        for ship_key, entry in data.items():
            props = entry.get("properties", {})
            conn.execute(
                "INSERT OR REPLACE INTO ship_stats VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ship_key,
                    entry.get("edID"),
                    props.get("name"),
                    props.get("manufacturer"),
                    props.get("hullMass"),
                    json.dumps(props),
                    json.dumps(entry.get("slots", {})),
                    json.dumps(entry.get("defaults", {})),
                    json.dumps(entry.get("bulkheads", [])),
                ),
            )
            count += 1
    if commit:
        conn.commit()
    return count


def _is_better_entry(candidate, existing):
    """Compare two candidate entries for a symbol, return True if candidate is better.

    Precedence:
    1. Prefer non-preEngineered over preEngineered
    2. Prefer non-null mass over null mass
    3. Otherwise keep existing (first encountered)
    """
    candidate_has_pre = "preEngineered" in candidate
    existing_has_pre = "preEngineered" in existing

    # Rule 1: prefer non-preEngineered
    if candidate_has_pre != existing_has_pre:
        return not candidate_has_pre  # True if candidate is non-preEngineered

    # Rule 2: prefer non-null mass
    candidate_mass = candidate.get("mass")
    existing_mass = existing.get("mass")
    if (candidate_mass is not None) != (existing_mass is not None):
        return candidate_mass is not None

    # Rule 3: keep existing (first encountered)
    return False


def build_modules(conn, repo_path, commit=True):
    # Collect all candidate entries per symbol, selecting the best per our precedence rules
    best_by_symbol = {}

    for subdir in MODULE_DIRS:
        module_dir = Path(repo_path) / "modules" / subdir
        if not module_dir.is_dir():
            raise FileNotFoundError(f"coriolis-data input folder not found: {module_dir}")
        for path in sorted(module_dir.glob("*.json")):
            data = json.load(open(path, encoding="utf-8"))
            for _group_key, variants in data.items():
                if not isinstance(variants, list):
                    continue
                for m in variants:
                    symbol = m.get("symbol")
                    if not symbol:
                        continue
                    lower_symbol = symbol.lower()

                    # Compare with existing best entry for this symbol
                    if lower_symbol not in best_by_symbol:
                        best_by_symbol[lower_symbol] = m
                    elif _is_better_entry(m, best_by_symbol[lower_symbol]):
                        best_by_symbol[lower_symbol] = m

    # Insert the best entry for each symbol
    count = len(best_by_symbol)
    for symbol, m in best_by_symbol.items():
        extra = {k: v for k, v in m.items() if k not in _CORE_FIELDS}
        conn.execute(
            "INSERT OR REPLACE INTO module_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                symbol,
                m.get("id"),
                m.get("edID"),
                m.get("grp"),
                m.get("class"),
                m.get("rating"),
                m.get("mass"),
                m.get("power"),
                m.get("cost"),
                json.dumps(extra),
            ),
        )
    if commit:
        conn.commit()
    return count


def build_module_blueprints(conn, repo_path, commit=True):
    """Ingests modifications/modules.json's per-grp blueprint allow-list --
    which fdnames (e.g. "MC_Overcharged") a grp (e.g. "mc") can actually
    take, per Coriolis's own data. This is NOT the same thing as
    coriolis_blueprints' broad applies_to category text (e.g. "Weapon"):
    that text is far coarser than reality -- a torpedo pylon (tp) only
    really has LightWeight/Sturdy, and "Overcharged" resolves to two
    DIFFERENT, differently-featured blueprints depending on whether the
    grp is a multi-cannon (MC_Overcharged) or another weapon
    (Weapon_Overcharged). Clears and rebuilds the table every run, the
    same convention ingest_coriolis.py uses for its own tables (this
    table isn't dropped in build_schema -- see the comment there -- so a
    rerun must explicitly clear stale rows itself)."""
    data = json.load(open(Path(repo_path) / "modifications" / "modules.json", encoding="utf-8"))
    conn.execute("DELETE FROM module_blueprints")
    count = 0
    for grp, entry in data.items():
        for fdname in (entry.get("blueprints") or {}):
            conn.execute("INSERT OR REPLACE INTO module_blueprints VALUES (?,?)", (grp, fdname))
            count += 1
    if commit:
        conn.commit()
    return count


def _stem_names(stem):
    """'frame_shift_drive' -> {'frame shift drive'}. A trailing plural ('thrusters',
    'collector_limpet_controllers') is also stored singular, because players say both."""
    name = " ".join(stem.lower().split("_"))
    names = {name}
    if name.endswith("s") and not name.endswith("ss"):
        names.add(name[:-1])
    return names


def build_module_group_names(conn, repo_path, commit=True):
    """Each module group's name, taken from the file its modules live in
    (modules/*/frame_shift_drive.json -> 'frame shift drive'), so a group Coriolis adds
    later gets a name after the next ingest with no code change. Clears and rebuilds."""
    conn.execute("DELETE FROM module_group_names")
    count = 0
    for subdir in MODULE_DIRS:
        module_dir = Path(repo_path) / "modules" / subdir
        if not module_dir.is_dir():
            raise FileNotFoundError(f"coriolis-data input folder not found: {module_dir}")
        for path in sorted(module_dir.glob("*.json")):
            data = json.load(open(path, encoding="utf-8"))
            groups = {m.get("grp") for variants in data.values() if isinstance(variants, list)
                      for m in variants if m.get("grp")}
            for grp in groups:
                for name in _stem_names(path.stem):
                    count += conn.execute("INSERT OR IGNORE INTO module_group_names VALUES (?,?)", (grp, name)).rowcount
    if commit:
        conn.commit()
    return count


def apply_effect_corrections(conn, corrections=None):
    """Add the groups the commander's own crafts proved to module_specials (blueprint_corrections.json,
    "allowed_effects"). Idempotent. Returns how many rows were new."""
    added = 0
    for entry in (corrections if corrections is not None else bm.load_corrections()).get("allowed_effects", []):
        for grp in entry["groups"]:
            added += conn.execute("INSERT OR IGNORE INTO module_specials VALUES (?,?)", (grp, entry["edname"])).rowcount
    return added


def build_module_specials(conn, repo_path, commit=True, corrections=None):
    """Which experimental effects (specials) each group can take, from the same
    modifications/modules.json as the blueprint allow-list, plus the groups the commander's own
    crafts proved (blueprint_corrections.json, "allowed_effects"). Clears and rebuilds."""
    data = json.load(open(Path(repo_path) / "modifications" / "modules.json", encoding="utf-8"))
    conn.execute("DELETE FROM module_specials")
    count = 0
    for grp, entry in data.items():
        for edname in (entry.get("specials") or []):
            count += conn.execute("INSERT OR IGNORE INTO module_specials VALUES (?,?)", (grp, edname)).rowcount
    count += apply_effect_corrections(conn, corrections)
    if commit:
        conn.commit()
    return count


def build_blueprint_engineers(conn, repo_path, commit=True):
    """Which engineers offer each blueprint at each grade FOR EACH MODULE GROUP, from modifications/modules.json
    (the same file as the allow-lists). The group matters: Weapon_LongRange is Broo Tarquin and Mel Brandon for a
    pulse laser but Tod McQuinn and Marsha Hicks for a multi-cannon. Names are stored exactly as Coriolis spells
    them -- including its own misspellings, which the lookups correct -- so the table mirrors the source.
    Clears and rebuilds."""
    data = json.load(open(Path(repo_path) / "modifications" / "modules.json", encoding="utf-8"))
    conn.execute("DELETE FROM blueprint_engineers")
    count = 0
    for grp, entry in data.items():
        for fdname, blueprint in (entry.get("blueprints") or {}).items():
            for grade, detail in blueprint.get("grades", {}).items():
                for engineer in detail.get("engineers", []):
                    count += conn.execute("INSERT OR IGNORE INTO blueprint_engineers VALUES (?,?,?,?)",
                                          (grp, fdname, int(grade), engineer)).rowcount
    if commit:
        conn.commit()
    return count


def ingest_all(conn, repo_path):
    """Rebuild ship_stats, module_stats, module_blueprints, module_group_names, module_specials and blueprint_engineers from coriolis-data
    as ONE transaction, after checking that every input exists. A failure part
    way (a bad file, a full disk) rolls everything back, so the previous three
    tables are still there, complete, instead of a half-built set. Returns
    (ship_count, module_count, blueprint_count)."""
    require_inputs(repo_path)
    conn.execute("BEGIN")
    try:
        build_schema(conn, commit=False)
        ship_count = build_ships(conn, repo_path, commit=False)
        module_count = build_modules(conn, repo_path, commit=False)
        blueprint_count = build_module_blueprints(conn, repo_path, commit=False)
        build_module_group_names(conn, repo_path, commit=False)
        build_module_specials(conn, repo_path, commit=False)
        build_blueprint_engineers(conn, repo_path, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ship_count, module_count, blueprint_count


def main():
    require_inputs(REPO_PATH)    # before even opening (and so creating) the database
    conn = sqlite3.connect(DB_PATH)
    try:
        ship_count, module_count, blueprint_count = ingest_all(conn, REPO_PATH)
    finally:
        conn.close()
    print(f"ship_stats: {ship_count} rows")
    print(f"module_stats: {module_count} rows")
    print(f"module_blueprints: {blueprint_count} rows")


if __name__ == "__main__":
    main()
