"""Task 10a X5: setup.py judges the ship & module ingest "done" only when ALL
six tables (ship_stats, module_stats, module_blueprints, module_group_names, module_specials, blueprint_engineers) have rows."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_ship_modules as ism
import setup as setup_script

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"
TABLES = ("ship_stats", "module_stats", "module_blueprints", "module_group_names", "module_specials", "blueprint_engineers")


@pytest.fixture
def setup_db(tmp_path, monkeypatch):
    """A temp database that already holds a complete ingest, wired into setup.py."""
    path = tmp_path / "setup.db"
    conn = sqlite3.connect(str(path))
    ism.ingest_all(conn, str(REPO))
    conn.close()
    monkeypatch.setattr(setup_script, "DB_PATH", path)
    monkeypatch.setattr(setup_script, "CORIOLIS_DIR", REPO)
    monkeypatch.setattr(setup_script, "CORIOLIS_MARKER", REPO / "modifications" / "blueprints.json")
    return path


def _count(path, table):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _empty(path, table):
    conn = sqlite3.connect(str(path))
    conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def _must_not_ask(monkeypatch):
    def asked(*args, **kwargs):
        raise AssertionError("setup asked a question it should have answered itself")
    monkeypatch.setattr(setup_script, "_ask_yes_no", asked)


@pytest.mark.parametrize("empty_table", TABLES)
def test_a_partly_empty_ingest_is_redone_without_asking_and_names_the_empty_table(setup_db, monkeypatch, capsys, empty_table):
    _empty(setup_db, empty_table)
    assert _count(setup_db, empty_table) == 0
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    out = capsys.readouterr().out
    assert f"{empty_table} is empty -- re-ingesting ship & module stats." in out
    assert all(_count(setup_db, t) > 0 for t in TABLES)


def test_two_empty_tables_are_both_named(setup_db, monkeypatch, capsys):
    _empty(setup_db, "ship_stats")
    _empty(setup_db, "module_blueprints")
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "ship_stats, module_blueprints are empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert all(_count(setup_db, t) > 0 for t in TABLES)


def test_a_complete_ingest_is_left_alone_unless_the_commander_says_yes(setup_db, monkeypatch, capsys):
    before = {t: _count(setup_db, t) for t in TABLES}
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: False)
    assert setup_script.setup_ship_modules() is True
    out = capsys.readouterr().out
    assert "Skipped." in out and "is empty" not in out
    assert {t: _count(setup_db, t) for t in TABLES} == before


def test_a_missing_table_counts_as_empty_too(setup_db, monkeypatch, capsys):
    conn = sqlite3.connect(str(setup_db))
    conn.execute("DROP TABLE module_blueprints")
    conn.commit()
    conn.close()
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "module_blueprints is empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert _count(setup_db, "module_blueprints") > 0


def test_an_install_from_before_the_name_tables_is_repaired_without_asking(setup_db, monkeypatch, capsys):
    conn = sqlite3.connect(str(setup_db))
    conn.execute("DROP TABLE module_group_names")
    conn.execute("DROP TABLE module_specials")
    conn.commit()
    conn.close()
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "module_group_names, module_specials are empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert _count(setup_db, "module_group_names") > 0 and _count(setup_db, "module_specials") > 0


def test_an_install_from_before_the_engineer_table_is_repaired_without_asking(setup_db, monkeypatch, capsys):
    conn = sqlite3.connect(str(setup_db))
    conn.execute("DROP TABLE blueprint_engineers")
    conn.commit()
    conn.close()
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "blueprint_engineers is empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert _count(setup_db, "blueprint_engineers") > 0


def test_after_a_coriolis_refresh_the_ship_tables_are_rebuilt_without_asking(setup_db, monkeypatch, capsys):
    monkeypatch.setattr(setup_script, "coriolis_refreshed", True, raising=False)
    _must_not_ask(monkeypatch)
    before = _count(setup_db, "module_group_names")
    assert before > 0                                           # complete tables: only the refresh makes it rebuild
    assert setup_script.setup_ship_modules() is True
    assert "Coriolis data was just refreshed -- rebuilding the ship & module tables from it." in capsys.readouterr().out
    assert _count(setup_db, "module_group_names") == before
