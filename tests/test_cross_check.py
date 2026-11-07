import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cross_check as cc
import ingest_excel as ie


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ie.build_schema(conn)
    return conn


def _row(conn, module, upgrade, grade, items, category="core_internal"):
    conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)", (category, module, upgrade, grade, json.dumps(items), None))
    return conn.execute("SELECT category, module_name, upgrade, grade, materials_json FROM graded_blueprints ORDER BY rowid DESC LIMIT 1").fetchone()


def test_a_core_module_row_is_compared_against_coriolis(coriolis_db):
    conn = _db(coriolis_db)
    same = _row(conn, "Frame Shift Drive", "Increased range", 5,
                [{"qty": 1, "item": "Arsenic"}, {"qty": 1, "item": "Chemical Manipulators"}, {"qty": 1, "item": "Datamined Wake Exceptions"}])
    assert cc.compare_row(conn, same)[0] == "match"
    differs = _row(conn, "Frame Shift Drive", "Increased range", 5, [{"qty": 2, "item": "Arsenic"}])
    status, detail = cc.compare_row(conn, differs)
    assert status == "mismatch" and detail["coriolis"] == {"arsenic": 1, "chemical manipulators": 1, "datamined wake exceptions": 1}
    assert cc.compare_row(conn, _row(conn, "Frame Shift Drive", "Turbo", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_coriolis_row"
    assert cc.compare_row(conn, _row(conn, "Guardian FSD Booster", "Shielded", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_reference"
    assert cc.compare_row(conn, _row(conn, "Nonexistent", "Dirty", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_reference"
    conn.close()


def test_names_no_other_source_knows_are_listed_for_the_owner(coriolis_db):
    conn = _db(coriolis_db)
    _row(conn, "Fuel Scoop", "Shielded", 3, [{"qty": 1, "item": "Carbon"}, {"qty": 5, "item": "Merc Coin"},
                                              {"qty": 1, "item": "Adaptiv Encryptors Capture"}, {"qty": None, "item": "Needs personal research"}])
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES ('merccoin','Other','Merc Coin',3,'2026-11-06T00:00:00Z')")
    suspects = cc.suspect_material_names(conn)
    assert "Adaptiv Encryptors Capture" in suspects and "Needs personal research" in suspects
    assert "Carbon" not in suspects                       # Coriolis uses it
    assert "Merc Coin" not in suspects                    # the journal shows the commander holds it
    conn.close()
