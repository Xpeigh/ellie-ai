import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw

# the commander's own real Loadout event (Journal.*.log, 2026-09-19T16:11:31Z),
# trimmed to the fields this test needs -- not synthesized.
REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z",
    "event": "Loadout",
    "Ship": "explorer_nx",
    "ShipID": 48,
    "ShipName": " ",
    "ShipIdent": "XP-13E",
    "HullValue": 177925284,
    "ModulesValue": 180610931,
    "HullHealth": 1.0,
    "UnladenMass": 1286.300049,
    "CargoCapacity": 4,
    "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14},
    "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0, "Value": 1405203},
        {
            "Slot": "FrameShiftDrive",
            "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
            "On": True, "Priority": 0, "Health": 1.0, "Value": 79991013,
            "Engineering": {
                "Engineer": "Elvira Martuuk", "EngineerID": 300160,
                "BlueprintID": 128673694, "BlueprintName": "FSD_LongRange",
                "Level": 5, "Quality": 0.6,
                "ExperimentalEffect": "special_fsd_heavy",
                "ExperimentalEffect_Localised": "Mass Manager",
                "Modifiers": [
                    {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
                    {"Label": "Integrity", "Value": 147.016006, "OriginalValue": 188.0, "LessIsGood": 0},
                    {"Label": "PowerDraw", "Value": 1.2075, "OriginalValue": 1.05, "LessIsGood": 1},
                    {"Label": "FSDOptimalMass", "Value": 7333.768066, "OriginalValue": 4670.0, "LessIsGood": 0},
                ],
            },
        },
        {"Slot": "CargoHatch", "Item": "modularcargobaydoor", "On": True, "Priority": 2, "Health": 1.0},
    ],
}


def _fresh_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    jw.build_schema(conn)
    return conn


def test_loadout_populates_current_loadout(tmp_path):
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    row = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
    assert row is not None
    cols = [d[0] for d in conn.execute("SELECT * FROM current_loadout").description]
    data = dict(zip(cols, row))
    assert data["ship_key"] == "explorer_nx"
    assert data["max_jump_range"] == 73.964989
    assert data["unladen_mass"] == 1286.300049
    assert data["fuel_main"] == 128.0
    assert data["fuel_reserve"] == 1.14
    conn.close()


def test_loadout_populates_modules_with_real_engineering(tmp_path):
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    row = conn.execute(
        "SELECT item, engineering_blueprint, engineering_level, engineering_quality, "
        "engineering_experimental, engineering_modifiers_json "
        "FROM current_loadout_modules WHERE slot = 'FrameShiftDrive'"
    ).fetchone()
    assert row is not None
    item, blueprint, level, quality, experimental, modifiers_json = row
    assert item == "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii"
    assert blueprint == "FSD_LongRange"
    assert level == 5
    assert quality == 0.6
    assert experimental == "special_fsd_heavy"
    modifiers = json.loads(modifiers_json)
    optmass_mod = next(m for m in modifiers if m["Label"] == "FSDOptimalMass")
    assert optmass_mod["Value"] == 7333.768066

    # a module with no Engineering block has NULL engineering fields, not "None" strings
    plant = conn.execute(
        "SELECT engineering_blueprint FROM current_loadout_modules WHERE slot = 'PowerPlant'"
    ).fetchone()
    assert plant[0] is None
    conn.close()


def test_second_loadout_event_replaces_modules_wholesale(tmp_path):
    """A slot no longer present in a later Loadout event must not linger --
    same snapshot-replace semantics as Materials/reset_materials."""
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    smaller_event = dict(REAL_LOADOUT_EVENT)
    smaller_event["Modules"] = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0}
    ]
    jw.process_event(conn, smaller_event)

    remaining = conn.execute("SELECT slot FROM current_loadout_modules").fetchall()
    assert remaining == [("PowerPlant",)]
    conn.close()
