"""
Ingest EDCD/coriolis-data's modifications/blueprints.json and specials.json
into their own tables.

IMPORTANT SCHEMA NOTE (read before touching the query layer):
Coriolis keys blueprint materials by a broad category ("Weapon", "Power
plant", "Bulkheads", ...), NOT by a specific module name -- because a
blueprint's material cost is the same for every module in that category;
only the stat changes differ per module (and those aren't in this file).
Your Excel sheet, by contrast, lists materials per SPECIFIC module
("Beam Laser"). These don't merge into one table cleanly -- the join key
is different. Kept as two separate tables on purpose:
    graded_blueprints   -- from Excel, keyed by (module_name, upgrade, grade)
    coriolis_blueprints -- from Coriolis, keyed by (applies_to, upgrade, grade)
The query layer (Phase 3) is what decides, per lookup: "check
graded_blueprints for this exact module first; if not found, look up its
broad category and check coriolis_blueprints." That module -> category
mapping doesn't exist yet -- it's a real task, not a detail to gloss over.
"""
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_PATH = sys.argv[1] if len(sys.argv) > 1 else str(SCRIPT_DIR / "coriolis-data")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else str(SCRIPT_DIR / "elite-dangerous.db")


def build_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coriolis_blueprints (
            fdname TEXT, upgrade_name TEXT, applies_to TEXT,
            grade INTEGER, materials_json TEXT, feature_ranges_json TEXT
        );
        CREATE TABLE IF NOT EXISTS coriolis_experimental_effects (
            edname TEXT, name TEXT, description TEXT, materials_json TEXT
        );
        """
    )


def ingest_blueprints(conn, data):
    count = 0
    for fdname, entry in data.items():
        upgrade_name = entry.get("name")
        applies_to = ", ".join(entry.get("modulename", []))
        for grade_str, grade_data in entry.get("grades", {}).items():
            materials = grade_data.get("components", {})  # already {name: qty}
            features = grade_data.get("features", {})
            conn.execute(
                "INSERT INTO coriolis_blueprints VALUES (?,?,?,?,?,?)",
                (
                    fdname,
                    upgrade_name,
                    applies_to,
                    int(grade_str),
                    json.dumps(materials),
                    json.dumps(features),
                ),
            )
            count += 1
    return count


def ingest_specials(conn, data):
    count = 0
    for edname, entry in data.items():
        materials = entry.get("components", {})
        conn.execute(
            "INSERT INTO coriolis_experimental_effects VALUES (?,?,?,?)",
            (edname, entry.get("name"), entry.get("description"), json.dumps(materials)),
        )
        count += 1
    return count


def main():
    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)

    # clear any previous coriolis import so reruns don't duplicate rows
    conn.execute("DELETE FROM coriolis_blueprints")
    conn.execute("DELETE FROM coriolis_experimental_effects")

    blueprints = json.load(open(f"{REPO_PATH}/modifications/blueprints.json"))
    specials = json.load(open(f"{REPO_PATH}/modifications/specials.json"))

    bp_count = ingest_blueprints(conn, blueprints)
    sp_count = ingest_specials(conn, specials)
    conn.commit()

    print(f"coriolis_blueprints: {bp_count} rows ({len(blueprints)} blueprint types x grades)")
    print(f"coriolis_experimental_effects: {sp_count} rows")
    conn.close()


if __name__ == "__main__":
    main()
