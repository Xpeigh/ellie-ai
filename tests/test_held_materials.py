import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw
import query_functions as qf

PAIRS = json.load(open(Path(__file__).resolve().parent / "fixtures" / "held_material_names.json", encoding="utf-8"))


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    path = str(tmp_path / "held.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    for i, (key, shown, category) in enumerate(PAIRS):
        conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", (key, category, shown, i + 1, "2026-11-06T00:00:00Z"))
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("iron", "Raw", "Iron", 40, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(qf, "DB_PATH", path)
    return path


def test_all_34_material_types_the_old_lookup_missed_are_found_by_the_name_the_game_shows(inventory):
    assert len(PAIRS) == 34
    for i, (key, shown, category) in enumerate(PAIRS):
        r = qf.get_material_count(shown)
        assert r == {"found": True, "material_name": shown, "category": category, "count": i + 1}, shown


def test_the_journal_s_own_key_and_loose_spellings_work_too(inventory):
    assert qf.get_material_count("disruptedwakeechoes")["count"] == 8      # position 8 in the fixture
    assert qf.get_material_count("ATYPICAL disrupted-wake echoes")["material_name"] == "Atypical Disrupted Wake Echoes"
    assert qf.get_material_count("iron")["count"] == 40 and qf.get_material_count("Iron")["count"] == 40


def test_a_material_never_held_is_not_found_and_does_not_raise(inventory):
    assert qf.get_material_count("Unobtainium") == {"found": False, "material_name": "Unobtainium"}
    assert qf.get_material_count("")["found"] is False


def test_the_stored_key_wins_over_a_display_name_that_squashes_the_same(inventory):
    conn = sqlite3.connect(inventory)
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("zinc", "Raw", "Zinc", 7, "2026-11-06T00:00:00Z"))
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("other", "Raw", "Z-inc", 99, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()
    assert qf.get_material_count("zinc")["count"] == 7
