import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism
import ingest_coriolis as ic

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


def _base_db(tmp_path):
    """Ship/module/blueprint tables only -- no Loadout event processed yet,
    qf.DB_PATH not set. Callers that need a specific fixture event (the
    Type-9 test) load it themselves; _db() below is the shared case."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    ism.build_schema(conn)
    ism.build_ships(conn, str(REPO))
    ism.build_modules(conn, str(REPO))
    ic.build_schema(conn)
    ic.ingest_blueprints(conn, json.load(open(REPO / "modifications" / "blueprints.json")))
    ic.ingest_specials(conn, json.load(open(REPO / "modifications" / "specials.json")))
    conn.commit()
    conn.close()
    return path


def _db(tmp_path):
    path = _base_db(tmp_path)
    conn = sqlite3.connect(path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)
    conn.commit()
    conn.close()
    qf.DB_PATH = path
    return path


def _insert_module(path, slot, item, is_on=1):
    """Shared helper for tests that need a module fitted beyond what
    REAL_LOADOUT_EVENT covers (a booster, an extra fuel tank, ...) --
    same row shape journal_watcher.set_loadout writes, with no
    Engineering block."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (slot, item, is_on, 1, 1.0, None, None, None, None, None, None, "2026-09-19T16:11:31Z"),
    )
    conn.commit()
    conn.close()


def test_swap_unengineered_module_reports_deltas(tmp_path):
    """R8.1: the removed module here is the commander's REAL engineered FSD
    (journal Mass 208 t, base 160 t) -- the deltas must be computed from
    that real mass, not the unengineered module_stats base. Expected
    values computed independently through loadout_calc, not through
    simulate_module_swap."""
    path = _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5",
    )
    assert result["found"] is True
    assert result["compatible"] is True
    assert result["slot"] == "FrameShiftDrive"
    assert result["mass_change"] == -48.0
    assert abs(result["new_unladen_mass"] - 1238.30) < 0.01
    assert abs(result["new_jump_range"] - 47.15) < 0.01
    assert abs(result["power_change"] - (-0.16)) < 0.01
    assert abs(result["new_power_margin"] - 14.55) < 0.01


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


def test_swap_nonexistent_class_is_not_found(tmp_path):
    """Renamed from ..._is_incompatible_not_wrong_number: class 9 doesn't
    exist as a module for ANY ship, so this is a "no such module" case
    (found: False), not the class-too-big-for-this-slot case (compatible:
    False), which test_swap_class_too_big_for_slot_returns_compatible_false
    covers with a class that DOES exist."""
    _db(tmp_path)
    # explorer_nx's FSD slot is class 8 -- class 9 doesn't exist for any ship.
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=9, module_rating="A")
    assert result["found"] is False


def test_swap_class_too_big_for_slot_returns_compatible_false(tmp_path):
    """R6/R8.7: a class that DOES exist as a real module, but is bigger
    than the target slot allows. Explorer NX standard sizes are
    [8, 7, 8, 5, 7, 8, 7], so LifeSupport's maximum is 5."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="life support", module_class=8, module_rating="E")
    assert result["found"] is True
    assert result["compatible"] is False
    assert "5" in result["reason"]


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


def test_blueprint_category_whole_token_rejects_false_substring_thrusters(tmp_path):
    """C4/R4: "t" (thrusters' raw grp code) is a substring of "shift", so a
    naive substring match against "Frame shift drive, FSD" would wrongly
    apply an FSD blueprint to thrusters. The whole-token match must not."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="t", module_class=5, module_rating="A",
        blueprint_name="Increased range", grade=1,
    )
    assert result["found"] is False


def test_blueprint_category_whole_token_rejects_false_substring_power_plant(tmp_path):
    """C4/R4: "pp" (power plant's raw grp code) is a substring of
    "su-pp-ort", so a naive substring match against "Life support" would
    wrongly apply a Life support blueprint to a power plant. There is no
    real Power plant Lightweight blueprint in Coriolis at all."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="power plant", module_class=4, module_rating="A",
        variant="int_powerplant_size4_class5",
        blueprint_name="Lightweight", grade=1,
    )
    assert result["found"] is False


def test_weapon_blueprint_applies_and_reports_unmodeled_effects(tmp_path):
    """I2: applies_to for weapons is just "Weapon" -- a category match
    keyed off the friendly module_group text ("beam laser") would always
    miss it. SmallHardpoint1 is empty on the fixture ship, which is fine
    with an explicit slot; its maximum is class 1 by name."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="beam laser", module_class=1, module_rating="E",
        variant="hpt_beamlaser_fixed_small", slot="SmallHardpoint1",
        blueprint_name="Long range", grade=1,
    )
    assert result["found"] is True
    # real Weapon "Long range" g1 features: fallofffromrange, mass, power,
    # range, shotspeed -- this code applies mass/power only.
    assert set(result["unmodeled_blueprint_effects"]) == {"fallofffromrange", "range", "shotspeed"}


def test_guardian_fsd_booster_adds_flat_bonus_to_jump_range(tmp_path):
    """A fitted Guardian FSD Booster contributes a flat additive bonus to
    the reported jump range -- verified against real coriolis-data (class
    2 -> jumpboost 6 ly). Swapping something OTHER than the FSD or the
    booster must still pick up the currently-fitted booster's
    contribution when computing the resulting range."""
    path = _db(tmp_path)
    without = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    _insert_module(path, "Slot08_Size4", "int_guardianfsdbooster_size2")
    with_booster = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    assert without["found"] is True and with_booster["found"] is True
    # no blueprint on this swap, so both sides collapse to a plain float
    # (see _min_max_or_single) rather than a {"min","max"} dict
    assert isinstance(without["new_jump_range"], (int, float))
    assert isinstance(with_booster["new_jump_range"], (int, float))
    assert abs((with_booster["new_jump_range"] - without["new_jump_range"]) - 6) < 0.01


def test_removed_booster_does_not_contribute_its_bonus(tmp_path):
    """C5/R5/R8.6a: swapping the booster's OWN slot for something else must
    not still add the removed booster's +6 ly. Cargo rack 4E is itself
    ambiguous (standard vs corrosion-proof), hence the variant. Expected
    value computed independently through loadout_calc: compute_jump_range
    (1286.300049 - 1.3 booster + 0.0 rack, 7333.768066, 6.8, 0.011,
    2.5025) with NO booster bonus."""
    path = _db(tmp_path)
    _insert_module(path, "Slot08_Size4", "int_guardianfsdbooster_size2")

    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=4, module_rating="E",
        variant="int_cargorack_size4_class1", slot="Slot08_Size4",
    )
    assert result["found"] is True
    assert abs(result["new_jump_range"] - 74.04) < 0.01


def test_standard_slot_rejects_wrong_group(tmp_path):
    """R5: a standard slot only ever holds its own group -- life support
    can't be put into the FSD's slot just because an explicit slot was
    given."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="life support", module_class=5, module_rating="D", slot="FrameShiftDrive",
    )
    assert result["compatible"] is False


def test_explicit_slot_case_insensitive_resolves_to_real_casing(tmp_path):
    """R5: an explicit slot is matched case-insensitively against what's
    actually fitted, and the result carries the real casing."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5", slot="frameshiftdrive",
    )
    assert result["found"] is True
    assert result["slot"] == "FrameShiftDrive"


def test_fuel_tank_swap_with_internal_tank_fitted_is_ambiguous(tmp_path):
    """C1/R1: STANDARD_SLOT_NAMES must only be a FALLBACK. grp `ft` covers
    both the standard FuelTank slot and optional-internal tanks -- with an
    internal tank ALSO fitted, this is genuinely ambiguous and must never
    silently resolve to the standard slot."""
    path = _db(tmp_path)
    _insert_module(path, "Slot05_Size4", "int_fueltank_size4_class3")
    _insert_module(path, "FuelTank", "int_fueltank_size7_class3")

    result = qf.simulate_module_swap(module_group="fuel tank", module_class=5, module_rating="C")
    assert result["found"] is False
    assert "slot" in result["reason"].lower()


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


def test_power_margin_matches_real_type9_deployed_power(tmp_path):
    """R7/R8.8: the commander's real Type-9 (Journal, 2026-01-04), verified
    against the game's own outfitting screen: DEPLOYED 11.01MW / TOTAL
    POWER 16.80MW, a margin of 5.79. Swapping the FSD for the identical
    module (power_change ~= 0) must reproduce that margin exactly. Type-9
    standard sizes are [6, 7, 6, 5, 6, 4, 6], so FSD maximum 6, and class 6
    fits."""
    path = _base_db(tmp_path)
    conn = sqlite3.connect(path)
    type9_event = json.load(open(FIXTURES / "type9_loadout_2026-01-04.json"))
    jw.process_event(conn, type9_event)
    conn.commit()
    conn.close()
    qf.DB_PATH = path

    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=6, module_rating="E",
        variant="int_hyperdrive_size6_class1",
    )
    assert result["found"] is True
    assert result["compatible"] is True
    assert abs(result["power_change"]) < 0.01
    assert abs(result["new_power_margin"] - 5.79) < 0.01
