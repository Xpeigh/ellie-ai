"""Final review, finding 3: the cost corrections must reach the database on EVERY setup run, not only
when the commander agrees to a git pull and re-ingest of the Coriolis data."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm
import ingest_coriolis as ic
import ingest_ship_modules as ism
import setup as setup_script

CORRECTIONS = {
    "graded": [{"fdname": "FSD_LongRange", "grade": 3, "materials": {"Carbon": 2}, "evidence": "e"}],
    "experimental": [{"edname": "special_weapon_lightweight", "materials": {"Carbon": 5, "Tin": 1}, "evidence": "e"}],
    "allowed_effects": [{"edname": "special_drag_munitions", "groups": ["mr"], "evidence": "e"}],
}


@pytest.fixture
def old_install(tmp_path, monkeypatch):
    """A database as it stood before the corrections: Coriolis costs as published, empty allow-list table."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    ic.build_schema(conn)
    ism.build_schema(conn)
    conn.execute("INSERT INTO coriolis_blueprints VALUES ('FSD_LongRange','Increased range','Frame shift drive',3,?,'{}')", (json.dumps({"Carbon": 1}),))
    conn.execute("INSERT INTO coriolis_experimental_effects VALUES ('special_weapon_lightweight','Stripped Down','d',?)", (json.dumps({"Carbon": 3, "Tin": 1}),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(setup_script, "DB_PATH", path)
    monkeypatch.setattr(bm, "load_corrections", lambda path=None: CORRECTIONS)
    return path


def _cost(path, sql):
    conn = sqlite3.connect(str(path))
    try:
        return json.loads(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def test_every_setup_run_applies_the_cost_corrections_without_a_reingest(old_install, capsys):
    assert setup_script.setup_corrections() is True
    assert _cost(old_install, "SELECT materials_json FROM coriolis_blueprints") == {"Carbon": 2}
    assert _cost(old_install, "SELECT materials_json FROM coriolis_experimental_effects") == {"Carbon": 5, "Tin": 1}
    assert "corrections: 2 of 2 applied" in capsys.readouterr().out


def test_running_it_again_changes_nothing(old_install):
    setup_script.setup_corrections()
    setup_script.setup_corrections()
    assert _cost(old_install, "SELECT materials_json FROM coriolis_blueprints") == {"Carbon": 2}
    conn = sqlite3.connect(str(old_install))
    assert conn.execute("SELECT COUNT(*) FROM module_specials WHERE grp = 'mr'").fetchone()[0] == 1
    conn.close()


def test_the_extra_effect_groups_reach_the_allow_list_too(old_install):
    setup_script.setup_corrections()
    conn = sqlite3.connect(str(old_install))
    assert conn.execute("SELECT edname FROM module_specials WHERE grp = 'mr'").fetchall() == [("special_drag_munitions",)]
    conn.close()


def test_without_the_effect_table_the_cost_corrections_still_apply(old_install):
    conn = sqlite3.connect(str(old_install))
    conn.execute("DROP TABLE module_specials")
    conn.commit()
    conn.close()
    assert setup_script.setup_corrections() is True
    assert _cost(old_install, "SELECT materials_json FROM coriolis_blueprints") == {"Carbon": 2}


def test_with_no_cost_data_loaded_it_says_so_and_skips(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(setup_script, "DB_PATH", path)
    assert setup_script.setup_corrections() is False
    assert "isn't loaded" in capsys.readouterr().out


def test_main_runs_the_corrections_step_after_the_tables_are_built(monkeypatch):
    order = []
    for name in ("ensure_live_tables", "check_ollama", "setup_coriolis", "setup_ship_modules", "setup_corrections",
                 "setup_edsm", "setup_excel", "verify"):
        monkeypatch.setattr(setup_script, name, (lambda n: lambda *a, **k: order.append(n))(name))
    setup_script.main()
    assert order.index("setup_corrections") > order.index("setup_ship_modules")
    assert order.index("setup_corrections") < order.index("verify")
