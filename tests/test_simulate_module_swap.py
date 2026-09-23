import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism
import ingest_coriolis as ic

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"

REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z", "event": "Loadout",
    "Ship": "explorer_nx", "ShipID": 48, "ShipName": " ", "ShipIdent": "XP-13E",
    "HullValue": 177925284, "ModulesValue": 180610931, "HullHealth": 1.0,
    "UnladenMass": 1286.300049, "CargoCapacity": 4, "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14}, "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0},
        {
            "Slot": "FrameShiftDrive",
            "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
            "On": True, "Priority": 0, "Health": 1.0,
            "Engineering": {
                "Engineer": "Elvira Martuuk", "BlueprintName": "FSD_LongRange",
                "Level": 5, "Quality": 0.6, "ExperimentalEffect": "special_fsd_heavy",
                "Modifiers": [
                    {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
                    {"Label": "PowerDraw", "Value": 1.2075, "OriginalValue": 1.05, "LessIsGood": 1},
                    {"Label": "FSDOptimalMass", "Value": 7333.768066, "OriginalValue": 4670.0, "LessIsGood": 0},
                ],
            },
        },
    ],
}


def _db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    ism.build_schema(conn)
    ism.build_ships(conn, str(REPO))
    ism.build_modules(conn, str(REPO))
    ic.build_schema(conn)
    ic.ingest_blueprints(conn, __import__("json").load(open(REPO / "modifications" / "blueprints.json")))
    ic.ingest_specials(conn, __import__("json").load(open(REPO / "modifications" / "specials.json")))
    jw.process_event(conn, REAL_LOADOUT_EVENT)
    conn.commit()
    conn.close()
    qf.DB_PATH = path
    return path


def test_swap_unengineered_module_reports_deltas(tmp_path):
    _db(tmp_path)
    # explorer_nx's FrameShiftDrive is a size-8 slot. Class 8 rating A has two
    # real variants (both SCO: the Mk II the commander fits, and the plain SCO
    # drive) -- ask for the plain one explicitly, unengineered.
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5",
    )
    assert result["found"] is True
    assert result["compatible"] is True
    assert result["slot"] == "FrameShiftDrive"
    assert "new_jump_range" in result
    assert "new_unladen_mass" in result


def test_swap_with_blueprint_applies_deterministic_delta(tmp_path):
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name="Increased range", grade=1,
        variant="int_hyperdrive_size7_class1",  # the standard (non-SCO) 7E drive
    )
    assert result["found"] is True
    assert result["compatible"] is True
    # grade 1 mass delta [0.1, 0.1] is deterministic -- a single number,
    # not a {"min", "max"} dict presenting a fake spread (Review Focus #5)
    assert isinstance(result["added"]["mass"], (int, float))


def test_swap_class_too_big_for_slot_is_incompatible_not_wrong_number(tmp_path):
    _db(tmp_path)
    # explorer_nx's FSD slot is class 8 -- class 9 doesn't exist for any ship.
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=9, module_rating="A")
    assert result["found"] is False


def test_unknown_module_group_fails_cleanly(tmp_path):
    """Review Focus #3: a module_group the mapping doesn't recognize."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="warp core", module_class=1, module_rating="A")
    assert result["found"] is False
    assert "reason" in result


def test_blueprint_not_applicable_to_module_category_fails_cleanly(tmp_path):
    """Review Focus #4: a real blueprint name, wrong category."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="life support", module_class=5, module_rating="D",
        blueprint_name="Increased range", grade=1,  # an FSD blueprint
    )
    assert result["found"] is False


def test_blueprint_effect_this_code_cant_apply_is_reported_not_dropped(tmp_path):
    """A features key with no numeric-application logic (this plan's
    simulate_module_swap only knows mass/power/optmass) must show up in
    unmodeled_blueprint_effects, never be silently ignored."""
    _db(tmp_path)
    # FSD_Shielded grade 1's real features (verified against
    # modifications/blueprints.json): integrity, mass, optmass, thermload.
    # This code applies mass and optmass (fsd) -- integrity and thermload
    # must be named as unmodeled, not silently dropped.
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name="Shielded", grade=1,
        variant="int_hyperdrive_size7_class1",
    )
    assert result["found"] is True
    assert set(result["unmodeled_blueprint_effects"]) == {"integrity", "thermload"}


def test_guardian_fsd_booster_adds_flat_bonus_to_jump_range(tmp_path):
    """A fitted Guardian FSD Booster contributes a flat additive bonus to
    the reported jump range -- verified against real coriolis-data (class
    2 -> jumpboost 6 ly). Swapping something OTHER than the FSD or the
    booster must still pick up the currently-fitted booster's
    contribution when computing the resulting range."""
    path = _db(tmp_path)
    without = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Slot08_Size4", "int_guardianfsdbooster_size2", 1, 1, 1.0, None,
         None, None, None, None, None, "2026-09-19T16:11:31Z"),
    )
    conn.commit()
    conn.close()
    with_booster = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    assert without["found"] is True and with_booster["found"] is True
    # no blueprint on this swap, so both sides collapse to a plain float
    # (see _min_max_or_single) rather than a {"min","max"} dict
    assert isinstance(without["new_jump_range"], (int, float))
    assert isinstance(with_booster["new_jump_range"], (int, float))
    assert abs((with_booster["new_jump_range"] - without["new_jump_range"]) - 6) < 0.01


def test_nothing_fitted_of_that_group_fails_cleanly_not_a_crash(tmp_path):
    """Review Focus #2: nothing of this group is currently fitted (no row
    in current_loadout_modules at all) -- Phase A doesn't guess which
    empty slot is meant (see the plan's honest scope note), but it must
    fail with a clear, actionable reason, never a KeyError/crash, and
    never silently pick a slot."""
    _db(tmp_path)
    # shield boosters are utility mounts: class 0, ratings A-E, one module
    # each (verified against coriolis-data)
    result = qf.simulate_module_swap(module_group="shield booster", module_class=0, module_rating="A")
    assert result["found"] is False
    assert "slot" in result["reason"].lower()

    # the explicit-slot escape hatch must work for this same case --
    # "TinyHardpoint2" is the journal's real naming for a utility mount
    # (the commander's real ship has a heat sink in TinyHardpoint1)
    result_with_slot = qf.simulate_module_swap(
        module_group="shield booster", module_class=0, module_rating="A", slot="TinyHardpoint2"
    )
    assert result_with_slot["found"] is True


def test_ambiguous_module_lists_variants_instead_of_guessing(tmp_path):
    """Several real modules share one group/class/rating with materially
    different stats (FSD 8A: the plain SCO drive has maxfuel 20.7, the Mk II
    has 6.8). Without a variant the tool must list them, never pick one;
    a partial variant string must narrow to exactly one."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=8, module_rating="A")
    assert result["found"] is False
    symbols = {v["symbol"] for v in result["variants"]}
    assert symbols == {
        "int_hyperdrive_overcharge_size8_class5",
        "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
    }

    narrowed = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A", variant="mk ii"
    )
    assert narrowed["found"] is True
    assert narrowed["added"]["symbol"] == "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii"
