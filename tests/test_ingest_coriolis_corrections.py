import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm
import ingest_coriolis as ic

ROOT = Path(__file__).resolve().parent.parent


def _db():
    conn = sqlite3.connect(":memory:")
    ic.build_schema(conn)
    conn.execute("INSERT INTO coriolis_blueprints VALUES ('FSD_LongRange','Increased range','Frame shift drive',3,?,'{}')", (json.dumps({"Carbon": 1}),))
    conn.execute("INSERT INTO coriolis_experimental_effects VALUES ('special_weapon_lightweight','Stripped Down','d',?)", (json.dumps({"Carbon": 3, "Tin": 1}),))
    return conn


def test_a_correction_replaces_the_cost_and_reports_how_many_rows_it_changed():
    conn = _db()
    applied = ic.apply_corrections(conn, {
        "graded": [{"fdname": "FSD_LongRange", "grade": 3, "materials": {"Carbon": 2}, "evidence": "e"}],
        "experimental": [{"edname": "Special_Weapon_Lightweight", "materials": {"Carbon": 5, "Tin": 1}, "evidence": "e"}]})
    assert applied == 2
    assert json.loads(conn.execute("SELECT materials_json FROM coriolis_blueprints").fetchone()[0]) == {"Carbon": 2}
    assert json.loads(conn.execute("SELECT materials_json FROM coriolis_experimental_effects").fetchone()[0]) == {"Carbon": 5, "Tin": 1}


def test_a_correction_that_matches_nothing_is_reported_not_ignored():
    conn = _db()
    assert ic.apply_corrections(conn, {"graded": [], "experimental": [{"edname": "special_gone", "materials": {}, "evidence": "e"}]}) == 0


def test_a_missing_corrections_file_is_the_same_as_an_empty_one(tmp_path):
    assert bm.load_corrections(tmp_path / "nope.json") == {"graded": [], "experimental": [], "allowed_effects": []}


def test_the_shipped_corrections_file_is_well_formed_and_every_entry_has_evidence():
    data = json.load(open(ROOT / "blueprint_corrections.json", encoding="utf-8"))
    assert set(data) == {"graded", "experimental", "allowed_effects"}
    for entry in data["graded"] + data["experimental"]:
        assert entry["evidence"].strip() and isinstance(entry["materials"], dict) and entry["materials"]
    for entry in data["allowed_effects"]:
        assert entry["evidence"].strip() and entry["groups"] and all(g == g.lower() for g in entry["groups"])
    assert {e["edname"] for e in data["experimental"]} == {
        "special_weapon_lightweight", "special_drag_munitions", "special_lock_breaker"}
    assert {e["edname"] for e in data["allowed_effects"]} == {"special_drag_munitions", "special_thermal_cascade"}
