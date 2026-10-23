import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Event shapes below are trimmed from the commander's real Journal.2026-10-23
# (Python Mk II, tank 16 t): LoadGame fuel 11.655101, then a 4.285653 t jump
# left 7.369448 t -- 7.369448 + 4.285653 is exactly the 11.655101 he started
# with, which is what "fuel_before" has to reproduce.
LOAD_GAME = {
    "timestamp": "2026-10-23T09:28:26Z", "event": "LoadGame", "Commander": "Xpeigh",
    "Ship": "Python_NX", "Ship_Localised": "Python Mk II", "ShipID": 31,
    "FuelLevel": 11.655101, "FuelCapacity": 16.0, "Credits": 126591677,
}
FSD_JUMP = {
    "timestamp": "2026-10-23T09:34:43Z", "event": "FSDJump", "StarSystem": "Kayimali",
    "StarPos": [80.25, 4.46875, -8.875], "JumpDist": 20.252,
    "FuelUsed": 4.285653, "FuelLevel": 7.369448,
}
LOADOUT_WITH_FSD = {
    "timestamp": "2026-10-23T09:28:30Z", "event": "Loadout", "Ship": "python_nx", "ShipID": 31,
    "UnladenMass": 548.5, "CargoCapacity": 8, "MaxJumpRange": 52.0,
    "FuelCapacity": {"Main": 32.0, "Reserve": 0.83},
    "Modules": [
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_overcharge_size5_class5", "On": True, "Priority": 0, "Health": 1.0},
        {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5", "On": True, "Priority": 1, "Health": 1.0},
    ],
}


def _conn(tmp_path):
    # plain tuple rows, as journal_watcher.main() opens its connection
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    jw.build_schema(conn)
    return conn


def _state(conn):
    cur = conn.execute("SELECT * FROM ship_state WHERE id = 1")
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def _set_state(conn, **fields):
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE ship_state SET {cols} WHERE id = 1", fields)
    conn.commit()


def test_ship_state_row_exists_with_everything_unknown(tmp_path):
    state = _state(_conn(tmp_path))
    assert state["id"] == 1
    assert all(v is None for k, v in state.items() if k != "id")


def test_build_schema_twice_keeps_existing_ship_state(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=12.5, cargo_count=7)
    jw.build_schema(conn)
    state = _state(conn)
    assert (state["fuel_main"], state["cargo_count"]) == (12.5, 7)


def test_load_game_sets_fuel_and_forgets_cargo(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=3.0, cargo_count=50)
    jw.process_event(conn, LOAD_GAME)
    state = _state(conn)
    assert state["fuel_main"] == 11.655101
    assert state["cargo_count"] is None  # the Cargo event follows at launch


def test_load_game_without_fuel_level_leaves_fuel_unchanged(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=3.0, cargo_count=50)
    event = {k: v for k, v in LOAD_GAME.items() if k != "FuelLevel"}
    jw.process_event(conn, event)
    state = _state(conn)
    assert state["fuel_main"] == 3.0
    assert state["cargo_count"] is None


@pytest.mark.parametrize("event", [
    {"timestamp": "2026-10-23T10:00:00Z", "event": "ShipyardSwap", "ShipType": "type9", "ShipID": 43},
    # buying a ship writes ShipyardBuy + ShipyardNew + Loadout and NO ShipyardSwap (real journals: 55 purchases)
    {"timestamp": "2026-10-23T10:00:00Z", "event": "ShipyardNew", "ShipType": "type9", "NewShipID": 44},
    {"timestamp": "2026-10-23T10:00:00Z", "event": "Died", "KillerName": "Pirate"},
    {"timestamp": "2026-10-23T10:00:00Z", "event": "Resurrect", "Option": "rebuy", "Cost": 1000, "Bankrupt": False},
])
def test_ship_change_or_death_forgets_fuel_and_cargo(tmp_path, event):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=20.0, cargo_count=100)
    jw.process_event(conn, event)
    state = _state(conn)
    assert state["fuel_main"] is None
    assert state["cargo_count"] is None


def test_shipyard_swap_still_updates_current_ship(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, {"event": "ShipyardSwap", "ShipType": "type9", "ShipType_Localised": "Type-9 Heavy", "ShipID": 43})
    row = conn.execute("SELECT current_ship, current_ship_id FROM commander_status WHERE id = 1").fetchone()
    assert row == ("Type-9 Heavy", 43)


def test_cargo_event_for_the_ship_sets_count(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship", "Count": 128, "Inventory": []})
    assert _state(conn)["cargo_count"] == 128


def test_cargo_event_for_another_vessel_is_ignored(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, cargo_count=128)
    jw.process_event(conn, {"event": "Cargo", "Vessel": "SRV", "Count": 4, "Inventory": []})
    assert _state(conn)["cargo_count"] == 128


def test_cargo_event_without_count_leaves_cargo_unchanged(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, cargo_count=128)
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship"})
    assert _state(conn)["cargo_count"] == 128


def test_reservoir_replenished_sets_main_tank_level(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, {"event": "ReservoirReplenished", "FuelMain": 6.539448, "FuelReservoir": 0.83})
    assert _state(conn)["fuel_main"] == 6.539448


def test_reservoir_replenished_without_fuel_main_leaves_fuel_unchanged(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=9.0)
    jw.process_event(conn, {"event": "ReservoirReplenished", "FuelReservoir": 0.83})
    assert _state(conn)["fuel_main"] == 9.0


def test_fuel_scoop_sets_total(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, {"event": "FuelScoop", "Scooped": 5.007082, "Total": 31.931078})
    assert _state(conn)["fuel_main"] == 31.931078


def test_fuel_scoop_without_total_leaves_fuel_unchanged(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=9.0)
    jw.process_event(conn, {"event": "FuelScoop", "Scooped": 5.0})
    assert _state(conn)["fuel_main"] == 9.0


@pytest.mark.parametrize("etype", ["RefuelAll", "RefuelPartial"])
def test_refuel_adds_amount(tmp_path, etype):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=10.0)
    jw.process_event(conn, {"event": etype, "Cost": 476, "Amount": 5.5})
    assert _state(conn)["fuel_main"] == 15.5


def test_refuel_all_is_capped_at_the_tank_capacity(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, {**LOADOUT_WITH_FSD, "FuelCapacity": {"Main": 32.0, "Reserve": 0.83}})
    _set_state(conn, fuel_main=31.0)
    jw.process_event(conn, {"event": "RefuelAll", "Cost": 100, "Amount": 5.0})
    assert _state(conn)["fuel_main"] == 32.0


def test_refuel_without_a_known_tank_capacity_is_not_capped(tmp_path):
    conn = _conn(tmp_path)  # no Loadout yet: current_loadout.fuel_main is NULL
    _set_state(conn, fuel_main=31.0)
    jw.process_event(conn, {"event": "RefuelAll", "Cost": 100, "Amount": 5.0})
    assert _state(conn)["fuel_main"] == 36.0


@pytest.mark.parametrize("etype", ["RefuelAll", "RefuelPartial"])
def test_refuel_with_unknown_fuel_stays_unknown(tmp_path, etype):
    conn = _conn(tmp_path)
    jw.process_event(conn, {"event": etype, "Cost": 100, "Amount": 5.0})
    assert _state(conn)["fuel_main"] is None


def test_refuel_without_amount_leaves_fuel_unchanged(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=10.0)
    jw.process_event(conn, {"event": "RefuelAll", "Cost": 100})
    assert _state(conn)["fuel_main"] == 10.0


def test_loadout_event_does_not_wipe_fuel_or_cargo(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=12.5, cargo_count=77)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    state = _state(conn)
    assert (state["fuel_main"], state["cargo_count"]) == (12.5, 77)


def test_fsd_jump_records_the_jump_snapshot_then_updates_fuel(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, LOAD_GAME)
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship", "Count": 6})
    jw.process_event(conn, {**FSD_JUMP, "BoostUsed": 1})
    state = _state(conn)
    assert state["last_jump_dist"] == 20.252
    assert state["last_jump_fuel_used"] == 4.285653
    assert state["last_jump_fuel_before"] == pytest.approx(11.655101, abs=1e-9)  # FuelLevel + FuelUsed
    assert state["last_jump_cargo"] == 6
    assert state["last_jump_unladen"] == 548.5
    assert state["last_jump_fsd_item"] == "int_hyperdrive_overcharge_size5_class5"
    assert state["last_jump_boosted"] == 1
    assert state["last_jump_time"] == "2026-10-23T09:34:43Z"
    # and only AFTER the snapshot: the tank now holds what the jump left
    assert state["fuel_main"] == 7.369448
    assert state["cargo_count"] == 6


def test_fsd_jump_without_boost_is_recorded_unboosted(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, FSD_JUMP)
    assert _state(conn)["last_jump_boosted"] == 0


def test_fsd_jump_with_unknown_cargo_records_null_cargo(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, FSD_JUMP)  # no Cargo event seen: cargo is unknown, not 0
    assert _state(conn)["last_jump_cargo"] is None


def test_fsd_jump_before_any_loadout_records_null_unladen_and_fsd(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, FSD_JUMP)
    state = _state(conn)
    assert state["last_jump_unladen"] is None
    assert state["last_jump_fsd_item"] is None
    assert state["last_jump_dist"] == 20.252  # the rest of the snapshot is still real


def test_fsd_jump_without_fuel_fields_leaves_the_snapshot_and_fuel_alone(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, FSD_JUMP)
    before = _state(conn)
    incomplete = {"event": "FSDJump", "StarSystem": "Elsewhere", "StarPos": [1.0, 2.0, 3.0], "JumpDist": 9.9}
    jw.process_event(conn, incomplete)
    assert _state(conn) == before


def test_fsd_jump_still_updates_the_current_system_and_position(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, FSD_JUMP)
    row = conn.execute(
        "SELECT current_system, current_station, docked, current_x, current_y, current_z "
        "FROM commander_status WHERE id = 1"
    ).fetchone()
    assert tuple(row) == ("Kayimali", None, 0, 80.25, 4.46875, -8.875)


def test_location_event_does_not_touch_ship_state(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=12.5, cargo_count=77)
    before = _state(conn)
    jw.process_event(conn, {"event": "Location", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0]})
    assert _state(conn) == before


def test_updated_columns_are_stamped_only_for_the_value_written(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(jw, "_now_iso", lambda: "2026-10-23T12:00:00Z")
    jw.process_event(conn, {"event": "ReservoirReplenished", "FuelMain": 6.5})
    state = _state(conn)
    assert state["fuel_updated"] == "2026-10-23T12:00:00Z"
    assert state["cargo_updated"] is None

    monkeypatch.setattr(jw, "_now_iso", lambda: "2026-10-23T12:05:00Z")
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship", "Count": 3})
    state = _state(conn)
    assert state["cargo_updated"] == "2026-10-23T12:05:00Z"
    assert state["fuel_updated"] == "2026-10-23T12:00:00Z"


def test_forgetting_fuel_and_cargo_is_also_stamped(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(jw, "_now_iso", lambda: "2026-10-23T12:00:00Z")
    jw.process_event(conn, {"event": "Died"})
    state = _state(conn)
    assert state["fuel_updated"] == "2026-10-23T12:00:00Z"
    assert state["cargo_updated"] == "2026-10-23T12:00:00Z"


def test_real_type9_loadout_fixture_does_not_disturb_ship_state(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=40.0, cargo_count=200)
    jw.process_event(conn, json.load(open(FIXTURES / "type9_loadout_2026-01-04.json")))
    state = _state(conn)
    assert (state["fuel_main"], state["cargo_count"]) == (40.0, 200)


# --- fix round 1 ------------------------------------------------------------

LAST_JUMP_COLUMNS = (
    "last_jump_dist", "last_jump_fuel_used", "last_jump_fuel_before", "last_jump_cargo",
    "last_jump_unladen", "last_jump_fsd_item", "last_jump_fsd_optmass", "last_jump_boosted", "last_jump_time",
)


def _engineered_fsd_loadout(optmass):
    """LOADOUT_WITH_FSD with the drive engineered to the given FSDOptimalMass."""
    event = json.loads(json.dumps(LOADOUT_WITH_FSD))
    event["Modules"][0]["Engineering"] = {
        "BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 0.6,
        "Modifiers": [
            {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
            {"Label": "FSDOptimalMass", "Value": optmass, "OriginalValue": 4670.0, "LessIsGood": 0},
        ],
    }
    return event


def _record_a_jump(conn):
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, LOAD_GAME)
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship", "Count": 6})
    jw.process_event(conn, FSD_JUMP)
    assert _state(conn)["last_jump_dist"] == 20.252  # sanity: there is a snapshot to lose


def test_old_ship_state_table_gains_the_jump_optmass_column_without_losing_data(tmp_path):
    """A database from before last_jump_fsd_optmass existed: CREATE TABLE IF
    NOT EXISTS won't add the column, so build_schema has to."""
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.executescript(
        """
        CREATE TABLE ship_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            fuel_main REAL, cargo_count INTEGER, fuel_updated TEXT, cargo_updated TEXT,
            last_jump_dist REAL, last_jump_fuel_used REAL, last_jump_fuel_before REAL,
            last_jump_cargo INTEGER, last_jump_unladen REAL, last_jump_fsd_item TEXT,
            last_jump_boosted INTEGER, last_jump_time TEXT
        );
        INSERT INTO ship_state (id, fuel_main, cargo_count, last_jump_dist) VALUES (1, 12.5, 7, 20.252);
        """
    )
    jw.build_schema(conn)
    state = _state(conn)
    assert (state["fuel_main"], state["cargo_count"], state["last_jump_dist"]) == (12.5, 7, 20.252)
    assert state["last_jump_fsd_optmass"] is None
    jw.process_event(conn, {"event": "ReservoirReplenished", "FuelMain": 3.0})  # and it is writable
    assert _state(conn)["fuel_main"] == 3.0


def test_fsd_jump_snapshots_the_engineered_optimal_mass_in_force_at_the_jump(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, _engineered_fsd_loadout(1488.0))
    jw.process_event(conn, FSD_JUMP)
    assert _state(conn)["last_jump_fsd_optmass"] == 1488.0


def test_fsd_jump_snapshot_optmass_is_null_for_an_unengineered_drive(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, FSD_JUMP)
    assert _state(conn)["last_jump_fsd_optmass"] is None


def test_fsd_jump_snapshot_optmass_is_null_when_the_drive_has_no_such_modifier(tmp_path):
    conn = _conn(tmp_path)
    event = _engineered_fsd_loadout(1488.0)
    event["Modules"][0]["Engineering"]["Modifiers"] = [{"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1}]
    jw.process_event(conn, event)
    jw.process_event(conn, FSD_JUMP)
    assert _state(conn)["last_jump_fsd_optmass"] is None


def test_a_later_loadout_does_not_rewrite_the_recorded_jump_optmass(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, _engineered_fsd_loadout(1488.0))
    jw.process_event(conn, FSD_JUMP)
    jw.process_event(conn, LOADOUT_WITH_FSD)  # re-engineered / un-engineered since
    assert _state(conn)["last_jump_fsd_optmass"] == 1488.0


@pytest.mark.parametrize("event", [
    {"event": "ShipyardSwap", "ShipType": "type9", "ShipID": 43},
    {"event": "ShipyardNew", "ShipType": "type9", "NewShipID": 44},
    {"event": "Died", "KillerName": "Pirate"},
    {"event": "Resurrect", "Option": "rebuy", "Cost": 1000, "Bankrupt": False},
])
def test_ship_change_or_death_also_forgets_the_last_jump_snapshot(tmp_path, event):
    """The snapshot describes a jump by the ship we've left; keeping it would
    let the jump check judge the next ship's drive by it."""
    conn = _conn(tmp_path)
    _record_a_jump(conn)
    jw.process_event(conn, event)
    state = _state(conn)
    assert {c: state[c] for c in LAST_JUMP_COLUMNS} == {c: None for c in LAST_JUMP_COLUMNS}
    assert state["fuel_main"] is None and state["cargo_count"] is None


def test_buying_a_ship_forgets_the_old_ships_fuel_and_cargo(tmp_path):
    conn = _conn(tmp_path)
    _set_state(conn, fuel_main=20.0, cargo_count=100)
    jw.process_event(conn, {"event": "ShipyardBuy", "ShipType": "type9", "ShipPrice": 1})  # not a ship change by itself
    assert _state(conn)["fuel_main"] == 20.0
    jw.process_event(conn, {"event": "ShipyardNew", "ShipType": "type9", "NewShipID": 44})
    state = _state(conn)
    assert state["fuel_main"] is None and state["cargo_count"] is None


# Real Taxi event (Journal, trimmed): the jump reports the TAXI's fuel, not the
# commander's ship's -- 27 of 4,056 real FSDJump events.
TAXI_JUMP = {
    "timestamp": "2026-10-23T09:40:00Z", "event": "FSDJump", "Taxi": True, "Multicrew": False,
    "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0],
    "JumpDist": 20.022, "FuelUsed": 1.501045, "FuelLevel": 6.498955,
}


@pytest.mark.parametrize("flags", [{"Taxi": True, "Multicrew": False}, {"Taxi": False, "Multicrew": True}, {"Multicrew": True}])
def test_taxi_and_multicrew_jumps_leave_the_ships_fuel_cargo_and_snapshot_alone(tmp_path, flags):
    conn = _conn(tmp_path)
    _record_a_jump(conn)
    before = _state(conn)
    jw.process_event(conn, {**TAXI_JUMP, **flags})
    assert _state(conn) == before
    assert before["fuel_main"] == 7.369448 and before["cargo_count"] == 6


def test_taxi_jump_still_moves_the_commander(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, FSD_JUMP)
    jw.process_event(conn, TAXI_JUMP)
    row = conn.execute("SELECT current_system, current_x, current_y, current_z FROM commander_status WHERE id = 1").fetchone()
    assert row == ("Sol", 0.0, 0.0, 0.0)


def test_a_normal_jump_after_a_taxi_ride_records_as_before(tmp_path):
    conn = _conn(tmp_path)
    jw.process_event(conn, LOADOUT_WITH_FSD)
    jw.process_event(conn, LOAD_GAME)
    jw.process_event(conn, TAXI_JUMP)
    jw.process_event(conn, FSD_JUMP)
    state = _state(conn)
    assert state["fuel_main"] == 7.369448
    assert state["last_jump_dist"] == 20.252
    assert state["last_jump_fuel_before"] == pytest.approx(11.655101, abs=1e-9)
