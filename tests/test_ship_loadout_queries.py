import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"

REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z", "event": "Loadout",
    "Ship": "explorer_nx", "ShipID": 48, "ShipName": " ", "ShipIdent": "XP-13E",
    "HullValue": 177925284, "ModulesValue": 180610931, "HullHealth": 1.0,
    "UnladenMass": 1286.300049, "CargoCapacity": 4, "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14}, "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0},
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii", "On": True, "Priority": 0, "Health": 1.0},
    ],
}


def _db(tmp_path, with_loadout=True, with_ship_modules=True):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    if with_ship_modules:
        ism.build_schema(conn)
        ism.build_ships(conn, str(REPO))
        ism.build_modules(conn, str(REPO))
    if with_loadout:
        jw.process_event(conn, REAL_LOADOUT_EVENT)
    conn.close()
    qf.DB_PATH = path
    return path


def test_get_current_loadout_returns_real_data(tmp_path):
    _db(tmp_path)
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["ship"] == "explorer_nx"
    assert result["max_jump_range"] == 73.964989
    assert any(m["slot"] == "FrameShiftDrive" for m in result["modules"])


def test_get_current_loadout_before_any_loadout_event(tmp_path):
    """Review Focus #1: the table exists but is empty (fresh session,
    haven't undocked yet) -- a clean message, not a crash."""
    _db(tmp_path, with_loadout=False)
    result = qf.get_current_loadout()
    assert result["found"] is False
    assert "reason" in result


def test_get_ship_info_known_ship(tmp_path):
    _db(tmp_path)
    result = qf.get_ship_info("Python")
    assert result["found"] is True
    assert result["hull_mass"] == 350

def test_get_ship_info_unknown_ship(tmp_path):
    _db(tmp_path)
    result = qf.get_ship_info("Not A Real Ship")
    assert result["found"] is False
