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

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_PATH = sys.argv[1] if len(sys.argv) > 1 else str(SCRIPT_DIR / "coriolis-data")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else str(SCRIPT_DIR / "elite-dangerous.db")

MODULE_DIRS = ["standard", "internal", "hardpoints"]
_CORE_FIELDS = {"symbol", "id", "edID", "grp", "class", "rating", "mass", "power", "cost"}


def build_schema(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS ship_stats;
        DROP TABLE IF EXISTS module_stats;
        CREATE TABLE ship_stats (
            ship_key TEXT PRIMARY KEY, ed_id INTEGER, name TEXT, manufacturer TEXT,
            hull_mass REAL, properties_json TEXT, slots_json TEXT, defaults_json TEXT,
            bulkheads_json TEXT
        );
        CREATE TABLE module_stats (
            symbol TEXT PRIMARY KEY, coriolis_id TEXT, ed_id INTEGER, grp TEXT,
            class INTEGER, rating TEXT, mass REAL, power REAL, cost INTEGER,
            extra_json TEXT
        );
        """
    )
    conn.commit()


def build_ships(conn, repo_path):
    count = 0
    for path in sorted((Path(repo_path) / "ships").glob("*.json")):
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
    conn.commit()
    return count


def build_modules(conn, repo_path):
    count = 0
    for subdir in MODULE_DIRS:
        for path in sorted((Path(repo_path) / "modules" / subdir).glob("*.json")):
            data = json.load(open(path, encoding="utf-8"))
            for _group_key, variants in data.items():
                if not isinstance(variants, list):
                    continue
                for m in variants:
                    symbol = m.get("symbol")
                    if not symbol:
                        continue
                    extra = {k: v for k, v in m.items() if k not in _CORE_FIELDS}
                    conn.execute(
                        "INSERT OR REPLACE INTO module_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            symbol.lower(),
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
                    count += 1
    conn.commit()
    return count


def main():
    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)
    ship_count = build_ships(conn, REPO_PATH)
    module_count = build_modules(conn, REPO_PATH)
    print(f"ship_stats: {ship_count} rows")
    print(f"module_stats: {module_count} rows")
    conn.close()


if __name__ == "__main__":
    main()
