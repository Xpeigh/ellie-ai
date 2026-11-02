import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism
from loadout_calc import compute_current_jump_range

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

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
    assert result["jump_range_max"] == 73.96
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


# ---------------------------------------------------------------------------
# Current-load jump range: the range with the fuel and cargo on board NOW,
# not the journal's MaxJumpRange (empty hold, one jump of fuel).
#
# The commander's real Type-9 (tests/fixtures/type9_loadout_2026-01-04.json):
# unladen 1122.0 t, standard class 6 drive (optmass 960, maxfuel 5.3, fuelmul
# 0.011, fuelpower 2.6), tank 64.0 t, reservoir 0.77 t, MaxJumpRange 9.164523.
# ---------------------------------------------------------------------------

TYPE9_OPTMASS, TYPE9_MAXFUEL, TYPE9_FUELMUL, TYPE9_FUELPOWER = 960, 5.3, 0.011, 2.6
TYPE9_FSD_ITEM = "int_hyperdrive_size6_class1"


def _type9_db(tmp_path):
    path = _db(tmp_path, with_loadout=False)
    conn = sqlite3.connect(path)
    jw.process_event(conn, json.load(open(FIXTURES / "type9_loadout_2026-01-04.json")))
    conn.close()
    return path


def _set_ship_state(path, **fields):
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE ship_state SET {cols} WHERE id = 1", fields)
    conn.commit()
    conn.close()


def _type9_now(tmp_path, fuel=10.0, cargo=100):
    path = _type9_db(tmp_path)
    _set_ship_state(path, fuel_main=fuel, cargo_count=cargo)
    return path


def test_current_load_jump_range_from_tracked_fuel_and_cargo(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["ship"] == "type9"
    assert result["ship_name"] == "Type-9 Heavy"
    assert result["jump_range_now"] == 8.39      # compute_current_jump_range -> 8.385687
    assert result["jump_range_max"] == 9.16      # the journal's MaxJumpRange 9.164523
    assert result["fuel_now"] == 10.0
    assert result["cargo_now"] == 100
    assert result["mass_now"] == 1232.0          # 1122.0 + 10.0 + 100
    assert result["unladen_mass"] == 1122.0
    assert result["cargo_capacity"] == 348
    assert result["fuel_tank_capacity"] == 64.0
    assert result["fuel_reservoir_capacity"] == 0.77
    assert result["state_note"] is None
    assert "warning" not in result
    assert result["summary"] == (
        "Type-9 Heavy: jump range now 8.39 ly (fuel 10.0 t, cargo 100 t, mass 1232.0 t); "
        "max 9.16 ly with an empty hold and one jump of fuel."
    )


def test_old_key_names_are_gone(tmp_path):
    _type9_now(tmp_path)
    result = qf.get_current_loadout()
    for old in ("max_jump_range", "fuel_main", "fuel_reserve"):
        assert old not in result
    for new in ("jump_range_now", "jump_range_max", "fuel_tank_capacity", "fuel_reservoir_capacity",
                "fuel_now", "cargo_now", "mass_now", "ship_name", "state_note", "summary"):
        assert new in result


def test_current_load_range_uses_the_real_engineered_optmass(tmp_path):
    """The commander's Explorer NX with exactly one jump of fuel (6.8 t) and an
    empty hold is the journal's own MaxJumpRange scenario, so "now" must
    reproduce 73.96 -- which only happens if the FSD's engineered
    FSDOptimalMass (7333.77, base 4670) is used, not the module_stats base."""
    path = _db(tmp_path, with_loadout=False)
    engineered = json.loads(json.dumps(REAL_LOADOUT_EVENT))
    engineered["Modules"][1]["Engineering"] = {
        "BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 0.6,
        "Modifiers": [
            {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
            {"Label": "FSDOptimalMass", "Value": 7333.768066, "OriginalValue": 4670.0, "LessIsGood": 0},
        ],
    }
    conn = sqlite3.connect(path)
    jw.process_event(conn, engineered)
    conn.close()
    _set_ship_state(path, fuel_main=6.8, cargo_count=0)
    result = qf.get_current_loadout()
    assert result["ship_name"] == "Caspian Explorer"
    assert result["jump_range_now"] == 73.96
    assert result["jump_range_max"] == 73.96


def test_fuel_exactly_at_tank_capacity_is_usable(tmp_path):
    _type9_now(tmp_path, fuel=64.0, cargo=0)
    result = qf.get_current_loadout()
    assert result["state_note"] is None
    assert result["fuel_now"] == 64.0
    assert result["jump_range_now"] == 8.71      # 960 * (5.3/0.011)**(1/2.6) / (1122 + 64)


def test_empty_tank_gives_a_zero_range_not_unknown(tmp_path):
    _type9_now(tmp_path, fuel=0.0, cargo=0)
    result = qf.get_current_loadout()
    assert result["jump_range_now"] == 0.0
    assert result["mass_now"] == 1122.0
    assert result["state_note"] is None


FUEL_UNKNOWN = "current fuel isn't known yet (no refuel, jump or load event seen since the ship changed)"
CARGO_UNKNOWN = "current cargo isn't known yet (the game hasn't reported the cargo hold since the ship changed)"
FUEL_STALE = "the tracked fuel level doesn't fit this ship's tank, so it's probably stale"
FSD_UNKNOWN = "the fitted frame shift drive isn't in Ellie's module data"


@pytest.mark.parametrize("fuel, cargo, note", [
    (None, 100, FUEL_UNKNOWN),
    (10.0, None, CARGO_UNKNOWN),
    (99.0, 100, FUEL_STALE),
    (64.001, 100, FUEL_STALE),
    (-0.5, 100, FUEL_STALE),
    (None, None, FUEL_UNKNOWN),
])
def test_unusable_ship_state_gives_no_current_range_and_says_why(tmp_path, fuel, cargo, note):
    _type9_now(tmp_path, fuel=fuel, cargo=cargo)
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["jump_range_now"] is None
    assert result["fuel_now"] is None
    assert result["cargo_now"] is None
    assert result["mass_now"] is None
    assert result["state_note"] == note
    assert result["jump_range_max"] == 9.16
    assert result["summary"] == (
        f"Type-9 Heavy: max jump range 9.16 ly (empty hold, one jump of fuel). "
        f"Current-load jump range unavailable: {note}."
    )


def test_no_ship_state_table_yet_reads_as_unknown_not_as_a_setup_failure(tmp_path):
    """A database from before ship_state existed (the watcher hasn't been
    restarted since the upgrade) must still describe the ship."""
    path = _type9_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ship_state")
    conn.commit()
    conn.close()
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["jump_range_now"] is None
    assert result["state_note"] == FUEL_UNKNOWN


def test_fsd_missing_from_module_data_gives_no_current_range(tmp_path):
    path = _type9_now(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM module_stats WHERE symbol = ?", (TYPE9_FSD_ITEM,))
    conn.commit()
    conn.close()
    result = qf.get_current_loadout()
    assert result["jump_range_now"] is None
    assert result["state_note"] == FSD_UNKNOWN
    assert result["summary"] == (
        f"Type-9 Heavy: max jump range 9.16 ly (empty hold, one jump of fuel). "
        f"Current-load jump range unavailable: {FSD_UNKNOWN}."
    )


def test_fsd_without_optimal_mass_gives_no_current_range_not_a_near_zero_one(tmp_path):
    path = _type9_now(tmp_path)
    conn = sqlite3.connect(path)
    extra = json.loads(conn.execute("SELECT extra_json FROM module_stats WHERE symbol = ?", (TYPE9_FSD_ITEM,)).fetchone()[0])
    del extra["optmass"]
    conn.execute("UPDATE module_stats SET extra_json = ? WHERE symbol = ?", (json.dumps(extra), TYPE9_FSD_ITEM))
    conn.commit()
    conn.close()
    result = qf.get_current_loadout()
    assert result["jump_range_now"] is None
    assert result["state_note"] == FSD_UNKNOWN


def test_ship_missing_from_ship_stats_falls_back_to_the_journal_key(tmp_path):
    path = _type9_now(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout SET ship_key = 'some_new_ship' WHERE id = 1")
    conn.commit()
    conn.close()
    result = qf.get_current_loadout()
    assert result["ship"] == "some_new_ship"
    assert result["ship_name"] == "some_new_ship"
    assert result["summary"].startswith("some_new_ship: jump range now 8.39 ly")


def test_fitted_guardian_booster_adds_its_flat_bonus_to_the_current_range(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    plain = qf.get_current_loadout()["jump_range_now"]
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Slot08_Size4", "int_guardianfsdbooster_size2", 1, 1, 1.0, None, None, None, None, None, None, "2026-10-23T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    boosted = qf.get_current_loadout()["jump_range_now"]
    assert boosted == 14.39
    assert round(boosted - plain, 2) == 6.0


# --- the jump check: Ellie's mass model against the game's own fuel burn ----

OFF_BY_300 = "Ellie's mass estimate for your last jump was off by 300.0 t, so these jump ranges may be inaccurate."


def _consistent_last_jump(fuel_before=10.0, cargo=100, fuel_used=5.3):
    """A last-jump snapshot whose distance is built from the game's fuel
    formula, so the mass implied by the burn equals the modelled mass
    (unladen + fuel before + cargo) exactly."""
    mass = 1122.0 + fuel_before + cargo
    dist = TYPE9_OPTMASS * (fuel_used / TYPE9_FUELMUL) ** (1 / TYPE9_FUELPOWER) / mass
    return dict(
        last_jump_dist=dist, last_jump_fuel_used=fuel_used, last_jump_fuel_before=fuel_before,
        last_jump_cargo=cargo, last_jump_unladen=1122.0, last_jump_fsd_item=TYPE9_FSD_ITEM,
        last_jump_boosted=0, last_jump_time="2026-10-23T09:34:43Z",
    )


def test_jump_check_passes_silently_when_the_mass_model_matches_the_last_jump(tmp_path):
    path = _type9_now(tmp_path)
    _set_ship_state(path, **_consistent_last_jump())
    result = qf.get_current_loadout()
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]


def test_jump_check_stays_quiet_inside_the_one_ton_tolerance(tmp_path):
    path = _type9_now(tmp_path)
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 100.9})
    assert "warning" not in qf.get_current_loadout()


def test_jump_check_warns_when_the_mass_model_is_off(tmp_path):
    path = _type9_now(tmp_path)
    # the hold the model remembers is 300 t heavier than the burn says it was
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 400})
    result = qf.get_current_loadout()
    assert result["warning"] == OFF_BY_300
    assert result["summary"] == (
        "Type-9 Heavy: jump range now 8.39 ly (fuel 10.0 t, cargo 100 t, mass 1232.0 t); "
        "max 9.16 ly with an empty hold and one jump of fuel. " + OFF_BY_300
    )
    assert result["jump_range_now"] == 8.39  # the numbers are still given; the warning qualifies them


def test_jump_check_warning_is_withheld_when_there_is_no_current_range(tmp_path):
    """The warning qualifies the jump ranges we give; with fuel unknown there is
    no jump_range_now to qualify, and warning about a mass model nobody is
    using would only cast doubt on the game's own MaxJumpRange."""
    path = _type9_now(tmp_path, fuel=None, cargo=None)
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 400})  # would fire
    result = qf.get_current_loadout()
    assert result["jump_range_now"] is None
    assert "warning" not in result
    assert result["summary"] == (
        f"Type-9 Heavy: max jump range 9.16 ly (empty hold, one jump of fuel). "
        f"Current-load jump range unavailable: {FUEL_UNKNOWN}."
    )


@pytest.mark.parametrize("skipped", [
    {"last_jump_boosted": 1},
    {"last_jump_fsd_item": "int_hyperdrive_size5_class1"},
    {"last_jump_fsd_item": None},
    {"last_jump_cargo": None},
    {"last_jump_dist": None},
    {"last_jump_fuel_used": None},
    {"last_jump_unladen": None},
    {"last_jump_fuel_before": None},
])
def test_jump_check_is_skipped_silently_when_a_condition_is_missing(tmp_path, skipped):
    path = _type9_now(tmp_path)
    # off by 300 t -- would warn, were the check not skipped
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 400, **skipped})
    result = qf.get_current_loadout()
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]


def test_jump_check_is_skipped_when_there_is_no_fsd_data(tmp_path):
    path = _type9_now(tmp_path)
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 400})
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM module_stats WHERE symbol = ?", (TYPE9_FSD_ITEM,))
    conn.commit()
    conn.close()
    assert "warning" not in qf.get_current_loadout()


def test_jump_check_warns_just_outside_the_one_ton_tolerance(tmp_path):
    path = _type9_now(tmp_path)
    _set_ship_state(path, **{**_consistent_last_jump(), "last_jump_cargo": 101.5})
    assert qf.get_current_loadout()["warning"] == (
        "Ellie's mass estimate for your last jump was off by 1.5 t, so these jump ranges may be inaccurate."
    )


def test_fuel_a_hair_over_the_tank_is_float_noise_not_stale(tmp_path):
    _type9_now(tmp_path, fuel=64.0000005, cargo=0)
    result = qf.get_current_loadout()
    assert result["state_note"] is None
    assert result["jump_range_now"] == 8.71


def test_watcher_events_feed_the_current_range_end_to_end(tmp_path):
    """journal_watcher writes ship_state, query_functions reads it: a launch
    with 10.0 t aboard and 100 t of cargo, then a real-looking jump whose
    distance is built from the game's own fuel formula."""
    path = _type9_db(tmp_path)
    conn = sqlite3.connect(path)
    jw.process_event(conn, {"event": "LoadGame", "Ship": "Type9", "ShipID": 43, "FuelLevel": 10.0, "FuelCapacity": 64.0})
    jw.process_event(conn, {"event": "Cargo", "Vessel": "Ship", "Count": 100})
    before = qf.get_current_loadout()
    assert before["jump_range_now"] == 8.39
    assert before["fuel_now"] == 10.0 and before["cargo_now"] == 100

    jump = _consistent_last_jump(fuel_before=10.0, cargo=100, fuel_used=5.3)
    jw.process_event(conn, {
        "event": "FSDJump", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0], "timestamp": "2026-10-23T09:34:43Z",
        "JumpDist": jump["last_jump_dist"], "FuelUsed": 5.3, "FuelLevel": 4.7,
    })
    conn.close()
    after = qf.get_current_loadout()
    assert after["fuel_now"] == 4.7
    assert after["mass_now"] == 1226.7           # 1122.0 + 4.7 + 100
    assert after["jump_range_now"] == 8.04       # 4.7 t aboard: less than a full jump's fuel
    assert "warning" not in after                # the jump agreed with the mass model


@pytest.mark.parametrize("fuel, cargo", [(10.0, 100), (11.655101, 36), (None, None)])
def test_every_number_in_the_summary_is_in_the_structured_result(tmp_path, fuel, cargo):
    """The model is told to relay the summary, so each number in it must be
    one the structured result also carries, rounded to 2 dp."""
    _type9_now(tmp_path, fuel=fuel, cargo=cargo)
    result = qf.get_current_loadout()
    text = result["summary"].replace(result["ship_name"], " ")
    if result["state_note"]:
        text = text.replace(result["state_note"], " ")
    in_summary = [float(m) for m in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text)]
    in_result = {round(float(v), 2) for k, v in result.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert in_summary, "the summary should contain numbers"
    assert [n for n in in_summary if n not in in_result] == []


# --- fix round 1: the jump check judges the drive that made the jump ---------

def _type9_event(fsd_optmass=None):
    """The Type-9 fixture Loadout, optionally with its drive engineered to the
    given FSDOptimalMass (engineering changes the optimal mass, not the item)."""
    event = json.load(open(FIXTURES / "type9_loadout_2026-01-04.json"))
    if fsd_optmass is not None:
        for module in event["Modules"]:
            if module["Slot"] == "FrameShiftDrive":
                module["Engineering"] = {
                    "BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 0.6,
                    "Modifiers": [{"Label": "FSDOptimalMass", "Value": fsd_optmass, "OriginalValue": 960.0, "LessIsGood": 0}],
                }
    return event


def _process(path, *events):
    conn = sqlite3.connect(path)
    for event in events:
        jw.process_event(conn, event)
    conn.close()


def test_re_engineering_the_drive_after_a_jump_does_not_blame_the_mass_model(tmp_path):
    """A consistent jump with the stock drive, then the same drive symbol
    engineered to FSDOptimalMass 1488: the jump was made with 960, and the
    range now is computed with 1488."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump())
    _process(path, _type9_event(1488.0))
    result = qf.get_current_loadout()
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]
    assert result["jump_range_now"] == 13.0     # 1488 * (5.3/0.011)**(1/2.6) / 1232 = 12.9978
    assert result["jump_range_now"] == round(compute_current_jump_range(1122.0, 10.0, 100, 1488.0, 5.3, 0.011, 2.6), 2)


def test_a_jump_made_with_an_engineered_drive_is_judged_by_that_drive(tmp_path):
    """The reverse: the jump happens while the drive is engineered (optmass
    1488), then a Loadout shows it stock again. The snapshot's own optimal
    mass is what the game burned fuel against."""
    path = _type9_db(tmp_path)
    mass = 1122.0 + 10.0 + 100
    dist = 1488.0 * (5.3 / TYPE9_FUELMUL) ** (1 / TYPE9_FUELPOWER) / mass
    _process(
        path,
        _type9_event(1488.0),
        {"event": "LoadGame", "Ship": "Type9", "ShipID": 43, "FuelLevel": 10.0, "FuelCapacity": 64.0},
        {"event": "Cargo", "Vessel": "Ship", "Count": 100},
        {"event": "FSDJump", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0], "timestamp": "2026-10-23T09:34:43Z",
         "JumpDist": dist, "FuelUsed": 5.3, "FuelLevel": 4.7},
        _type9_event(),   # no longer engineered
    )
    result = qf.get_current_loadout()
    assert result["fuel_now"] == 4.7 and result["cargo_now"] == 100
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]


def test_a_wrong_mass_model_still_warns_when_the_drive_changed_since(tmp_path):
    """The snapshot optmass must not turn the check off: same engineered-then-
    stock flow as above, but the remembered cargo is 300 t off."""
    path = _type9_db(tmp_path)
    mass = 1122.0 + 10.0 + 100
    dist = 1488.0 * (5.3 / TYPE9_FUELMUL) ** (1 / TYPE9_FUELPOWER) / mass
    _process(
        path,
        _type9_event(1488.0),
        {"event": "LoadGame", "Ship": "Type9", "ShipID": 43, "FuelLevel": 10.0, "FuelCapacity": 64.0},
        {"event": "Cargo", "Vessel": "Ship", "Count": 400},   # the game burned fuel for 100 t, we remember 400
        {"event": "FSDJump", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0], "timestamp": "2026-10-23T09:34:43Z",
         "JumpDist": dist, "FuelUsed": 5.3, "FuelLevel": 4.7},
        _type9_event(),
    )
    assert qf.get_current_loadout()["warning"] == OFF_BY_300


def test_a_ship_change_discards_the_old_jump_so_a_look_alike_drive_is_not_judged_by_it(tmp_path):
    """A consistent jump, a swap to another Type-9 whose drive (same symbol)
    is engineered to 1200, fuel and cargo known again: nothing to compare
    against, so no warning about a jump this ship never made."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump())
    _process(path, {"event": "ShipyardSwap", "ShipType": "type9", "ShipID": 44}, _type9_event(1200.0))
    _set_ship_state(path, fuel_main=10.0, cargo_count=100)
    conn = sqlite3.connect(path)
    snapshot = conn.execute("SELECT last_jump_dist, last_jump_fuel_used, last_jump_fsd_item, last_jump_fsd_optmass FROM ship_state").fetchone()
    conn.close()
    assert snapshot == (None, None, None, None)
    result = qf.get_current_loadout()
    assert result["state_note"] is None
    assert "warning" not in result
    assert result["jump_range_now"] == round(compute_current_jump_range(1122.0, 10.0, 100, 1200.0, 5.3, 0.011, 2.6), 2)


# --- fix round 1: never crash, never print "None" ----------------------------

NO_EMPTY_MASS = "the ship's empty mass isn't known yet"


def _null_loadout_column(path, column):
    conn = sqlite3.connect(path)
    conn.execute(f"UPDATE current_loadout SET {column} = NULL WHERE id = 1")
    conn.commit()
    conn.close()


def test_unknown_empty_mass_makes_the_current_range_unavailable_instead_of_crashing(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _null_loadout_column(path, "unladen_mass")
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["jump_range_now"] is None
    assert result["mass_now"] is None and result["fuel_now"] is None and result["cargo_now"] is None
    assert result["unladen_mass"] is None
    assert result["state_note"] == NO_EMPTY_MASS
    assert result["summary"] == (
        f"Type-9 Heavy: max jump range 9.16 ly (empty hold, one jump of fuel). "
        f"Current-load jump range unavailable: {NO_EMPTY_MASS}."
    )


def test_unknown_max_jump_range_leaves_the_max_clause_out(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _null_loadout_column(path, "max_jump_range")
    result = qf.get_current_loadout()
    assert result["jump_range_max"] is None
    assert result["jump_range_now"] == 8.39
    assert result["summary"] == "Type-9 Heavy: jump range now 8.39 ly (fuel 10.0 t, cargo 100 t, mass 1232.0 t)."
    assert "None" not in result["summary"]


def test_unknown_max_jump_range_with_an_unusable_state(tmp_path):
    path = _type9_now(tmp_path, fuel=None, cargo=100)
    _null_loadout_column(path, "max_jump_range")
    result = qf.get_current_loadout()
    assert result["summary"] == f"Type-9 Heavy: Current-load jump range unavailable: {FUEL_UNKNOWN}."
    assert "None" not in result["summary"]


# --- fix round 2 --------------------------------------------------------------

# ship_state as the 6fe3013 watcher created it, before last_jump_fsd_optmass
OLD_SHIP_STATE_DDL = """
    CREATE TABLE ship_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        fuel_main REAL, cargo_count INTEGER, fuel_updated TEXT, cargo_updated TEXT,
        last_jump_dist REAL, last_jump_fuel_used REAL, last_jump_fuel_before REAL,
        last_jump_cargo INTEGER, last_jump_unladen REAL, last_jump_fsd_item TEXT,
        last_jump_boosted INTEGER, last_jump_time TEXT
    )
"""


def _use_the_old_ship_state_table(path, fuel=10.0, cargo=100, last_jump=None):
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ship_state")
    conn.execute(OLD_SHIP_STATE_DDL)
    conn.execute("INSERT INTO ship_state (id, fuel_main, cargo_count) VALUES (1, ?, ?)", (fuel, cargo))
    for column, value in (last_jump or {}).items():
        conn.execute(f"UPDATE ship_state SET {column} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()


def test_a_ship_state_table_the_watcher_has_not_migrated_yet_still_gives_the_range_and_no_warning(tmp_path):
    """The watcher hasn't been restarted since the upgrade: the table has no
    last_jump_fsd_optmass. The jump check can't judge that snapshot (even one
    that looks 300 t off), so it stays quiet, and nothing raises."""
    path = _type9_db(tmp_path)
    _use_the_old_ship_state_table(path, last_jump=_consistent_last_jump(cargo=400))
    result = qf.get_current_loadout()
    assert result["jump_range_now"] == 8.39
    assert "warning" not in result
    assert result["summary"] == (
        "Type-9 Heavy: jump range now 8.39 ly (fuel 10.0 t, cargo 100 t, mass 1232.0 t); "
        "max 9.16 ly with an empty hold and one jump of fuel."
    )


# W3: malformed engineering_modifiers_json on the fitted FSD must never raise.

MALFORMED_MODIFIERS = ["{not json", "null", "{}", "[1, 2]", ""]


def _malform_the_fsd_engineering(path, bad, with_blueprint):
    conn = sqlite3.connect(path)
    if with_blueprint:
        conn.execute(
            "UPDATE current_loadout_modules SET engineering_blueprint = 'FSD_LongRange', engineering_level = 5, "
            "engineering_modifiers_json = ? WHERE slot = 'FrameShiftDrive'", (bad,))
    else:
        conn.execute("UPDATE current_loadout_modules SET engineering_modifiers_json = ? WHERE slot = 'FrameShiftDrive'", (bad,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("with_blueprint", [False, True])
@pytest.mark.parametrize("bad", MALFORMED_MODIFIERS)
def test_malformed_engineering_modifiers_on_the_drive_read_as_no_engineering(tmp_path, bad, with_blueprint):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _malform_the_fsd_engineering(path, bad, with_blueprint)
    result = qf.get_current_loadout()
    assert result["jump_range_now"] == 8.39      # the stock drive's 960, as if never engineered
    assert result["state_note"] is None
    fsd = next(m for m in result["modules"] if m["slot"] == "FrameShiftDrive")
    if with_blueprint:
        assert fsd["engineering"]["modifiers"] == []
    else:
        assert "engineering" not in fsd


def test_a_non_numeric_optimal_mass_value_reads_as_no_engineering(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _malform_the_fsd_engineering(path, '[{"Label": "FSDOptimalMass", "Value": "abc"}]', with_blueprint=True)
    assert qf.get_current_loadout()["jump_range_now"] == 8.39


def test_no_base_optimal_mass_and_no_jump_time_one_means_no_warning_not_a_crash(tmp_path):
    """The fitted drive's module_stats row has no optmass, the drive is
    engineered now (so the range can still be computed), and the last-jump
    snapshot was taken before the engineering (NULL snapshot optmass): there is
    no optimal mass to judge that jump by."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))   # would warn, were it judged
    conn = sqlite3.connect(path)
    extra = json.loads(conn.execute("SELECT extra_json FROM module_stats WHERE symbol = ?", (TYPE9_FSD_ITEM,)).fetchone()[0])
    del extra["optmass"]
    conn.execute("UPDATE module_stats SET extra_json = ? WHERE symbol = ?", (json.dumps(extra), TYPE9_FSD_ITEM))
    conn.commit()
    conn.close()
    _process(path, _type9_event(1488.0))
    result = qf.get_current_loadout()
    assert result["jump_range_now"] == 13.0       # computed with the engineered 1488
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]
