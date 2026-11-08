"""Task 10a X7: one snapshot per tool call, and no connection left open.

The watcher commits while a tool call is running. With every SELECT of a call
inside one read transaction (WAL mode, so it never blocks the watcher), a call
sees one consistent database; without it, a Loadout landing between two reads
produces an answer stitched from two different ships."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"
TYPE9 = Path(__file__).resolve().parent / "fixtures" / "type9_loadout_2026-01-04.json"


def _type9_wal_db(tmp_path):
    path = str(tmp_path / "snap.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")      # as journal_watcher.main() and app.py open it
    jw.build_schema(conn)
    ism.ingest_all(conn, str(REPO))
    jw.process_event(conn, json.load(open(TYPE9)))
    conn.execute("UPDATE ship_state SET fuel_main = 10.0, cargo_count = 100 WHERE id = 1")
    conn.commit()
    conn.close()
    qf.DB_PATH = path
    return path


def test_a_loadout_landing_mid_call_does_not_split_the_answer(tmp_path, monkeypatch):
    path = _type9_wal_db(tmp_path)
    real = qf._fitted_fsd_params

    def reads_then_a_loadout_lands(conn):
        result = real(conn)
        other = sqlite3.connect(path, timeout=5)       # the watcher, committing mid-call
        event = json.load(open(TYPE9))
        del event["UnladenMass"]                       # the new Loadout has no empty mass
        jw.process_event(other, event)
        other.close()
        return result

    monkeypatch.setattr(qf, "_fitted_fsd_params", reads_then_a_loadout_lands)
    during = qf.get_current_loadout()
    # everything this call returned comes from the pre-commit snapshot
    assert during["unladen_mass"] == 1122.0
    assert during["state_note"] is None
    assert during["jump_range_now"] == 8.39

    monkeypatch.setattr(qf, "_fitted_fsd_params", real)
    after = qf.get_current_loadout()                   # a NEW call sees the commit
    assert after["unladen_mass"] is None
    assert after["state_note"] == "the ship's empty mass isn't known yet"


def test_a_swap_also_reads_one_snapshot(tmp_path, monkeypatch):
    path = _type9_wal_db(tmp_path)
    real = qf._current_ship_state

    def reads_then_the_ship_changes(conn, params):
        result = real(conn, params)
        other = sqlite3.connect(path, timeout=5)
        jw.process_event(other, {"event": "ShipyardSwap", "ShipType": "type9", "ShipID": 44})   # forgets fuel and cargo
        other.close()
        return result

    monkeypatch.setattr(qf, "_current_ship_state", reads_then_the_ship_changes)
    result = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    assert result["fuel_now"] == 10.0 and result["cargo_now"] == 100
    assert result["old_jump_range_now"] == 8.39
    assert "warning" not in result and result["state_note"] is None
    monkeypatch.undo()
    assert qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")["fuel_now"] is None


class _Tracked(sqlite3.Connection):
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_closed = False
        _Tracked.opened.append(self)

    def close(self):
        self.was_closed = True
        super().close()


TOOL_CALLS = {
    # the Type-9 test database has no Excel/EDSM tables, so these also exercise
    # the "no such table" exit that used to leave the connection open
    "get_blueprint": lambda: qf.get_blueprint("Beam Laser", "Efficient", 1),
    "get_experimental_effect": lambda: qf.get_experimental_effect("Beam Laser", "Drag Munitions"),
    "missing_materials": lambda: qf.missing_materials("Beam Laser", "Efficient", 1),
    "get_engineer": lambda: qf.get_engineer("Bill Turner"),
    "player_state": lambda: qf.player_state(),
    "get_material_count": lambda: qf.get_material_count("Chromium"),
    "distance_to_system": lambda: qf.distance_to_system("Sol"),
    "get_current_loadout": lambda: qf.get_current_loadout(),
    "get_ship_info": lambda: qf.get_ship_info("Python"),
    "get_ship_info (unknown ship)": lambda: qf.get_ship_info("No Such Ship"),
    "simulate_module_swap": lambda: qf.simulate_module_swap("life support", 5, "D"),
    "simulate_module_swap (refused)": lambda: qf.simulate_module_swap("frame shift drive", 1, "A"),
    "plan_upgrade": lambda: qf.plan_upgrade("Beam Laser", "Efficient", 3),
}


@pytest.mark.parametrize("name", list(TOOL_CALLS))
def test_every_tool_closes_every_connection_it_opened(tmp_path, monkeypatch, name):
    _type9_wal_db(tmp_path)
    real_connect = sqlite3.connect
    _Tracked.opened = []
    monkeypatch.setattr(qf.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_Tracked, **k))
    TOOL_CALLS[name]()
    monkeypatch.undo()
    assert _Tracked.opened, "the tool should have opened a connection"
    assert all(c.was_closed for c in _Tracked.opened)


def test_a_tool_that_raises_still_closes_its_connection(tmp_path, monkeypatch):
    _type9_wal_db(tmp_path)
    real_connect = sqlite3.connect
    _Tracked.opened = []
    monkeypatch.setattr(qf.sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=_Tracked, **k))
    monkeypatch.setattr(qf.json, "loads", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        qf.get_ship_info("Python")
    monkeypatch.undo()
    assert [c.was_closed for c in _Tracked.opened] == [True]


def test_a_tool_connection_is_one_read_transaction(tmp_path):
    _type9_wal_db(tmp_path)
    conn = qf._connect()
    try:
        assert conn.in_transaction is True
        assert conn.isolation_level is None
    finally:
        conn.close()
