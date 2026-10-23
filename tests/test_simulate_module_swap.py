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
import ingest_coriolis as ic
from loadout_calc import compute_current_jump_range, compute_jump_range

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
    ism.build_module_blueprints(conn, str(REPO))
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
    assert abs(result["new_jump_range_max"] - 47.15) < 0.01
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
    apply an FSD blueprint to thrusters. The whole-token match must not.

    N5: thrusters 5A is ITSELF ambiguous (int_engine_size5_class5 vs
    int_mkiiagileboost_engine_size5_class5) -- without `variant` the call
    fails on that ambiguity before ever reaching the blueprint check, which
    would make this test pass for the wrong reason. `variant` pins it to
    the blueprint-category check specifically."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="t", module_class=5, module_rating="A",
        variant="int_engine_size5_class5",
        blueprint_name="Increased range", grade=1,
    )
    assert result["found"] is False
    assert "doesn't apply" in result["reason"]


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
    miss it. MediumHardpoint1 is empty on the fixture ship, which is fine
    with an explicit slot (S3 confirms the ship really has one -- explorer_nx
    has 6 real Medium hardpoints, verified against coriolis-data; it has
    ZERO Small hardpoints, so a class-1 weapon must go somewhere bigger).
    Its maximum is class 2 by name, and a class-1 weapon fits a
    same-or-bigger slot."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="beam laser", module_class=1, module_rating="E",
        variant="hpt_beamlaser_fixed_small", slot="MediumHardpoint1",
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
    assert isinstance(without["new_jump_range_max"], (int, float))
    assert isinstance(with_booster["new_jump_range_max"], (int, float))
    assert abs((with_booster["new_jump_range_max"] - without["new_jump_range_max"]) - 6) < 0.01


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
    assert abs(result["new_jump_range_max"] - 74.04) < 0.01


def test_standard_slot_rejects_wrong_group(tmp_path):
    """R5/NB1/T1, repro F14: a standard slot only ever holds its own group
    -- life support can't be put into the FSD's slot just because an
    explicit slot was given. The rejection reason must name the SLOT's
    real owner (frame shift drive), not the module being swapped in
    (life support) -- round 2 regressed this and told the commander
    something false about his ship."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="life support", module_class=5, module_rating="D", slot="FrameShiftDrive",
    )
    assert result["compatible"] is False
    assert result["reason"] == "FrameShiftDrive only takes a frame shift drive"


def test_standard_slot_rejects_wrong_group_reverse(tmp_path):
    """NB1/T1, repro F14b: the mirror of the case above -- an FSD into the
    LifeSupport slot must name life support (the slot's owner), not frame
    shift drive (the module being swapped in)."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5", slot="LifeSupport",
    )
    assert result["compatible"] is False
    assert result["reason"] == "LifeSupport only takes a life support"


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


# ---------------------------------------------------------------------------
# Fix round 2: S1 (slot kind vs module kind), S2 (size from the slot, not
# the grp), S3 (slot existence + caveat), S4 (blueprint allow-list),
# S5 (N4/N5/N6).
# ---------------------------------------------------------------------------

def _type9_db(tmp_path):
    """Same setup as test_power_margin_matches_real_type9_deployed_power,
    factored out for the S1/S2/S3 repros that also need the Type-9 fixture
    (multiple internal/hardpoint slots to target, unlike explorer_nx's
    2-module fixture)."""
    path = _base_db(tmp_path)
    conn = sqlite3.connect(path)
    type9_event = json.load(open(FIXTURES / "type9_loadout_2026-01-04.json"))
    jw.process_event(conn, type9_event)
    conn.commit()
    conn.close()
    qf.DB_PATH = path
    return path


def test_core_module_into_internal_slot_is_incompatible(tmp_path):
    """N1/S1, repro F4: the FSD is a `core` module -- it must be rejected
    for ANY slot that isn't its own standard slot, not just the other 6
    standard slots (R5's old, narrower check). Before this round's fix,
    the grp=="fsd" branch treated this hypothetical drive as the ship's
    real FSD and returned compatible: True, mass_change 40.0, new_jump_range
    8.85 -- confidently wrong numbers for an impossible fit.

    NB1/T1: the reason must name the module being swapped in (frame shift
    drive), not the internal slot's non-existent "owner" -- round 2's bug
    was specific to naming the wrong group; this is the "core module in a
    non-standard slot" branch of that fix, not the "wrong standard slot"
    branch (that's the reverse test above)."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=6, module_rating="E",
        variant="int_hyperdrive_size6_class1", slot="Slot05_Size4",
    )
    assert result["found"] is True
    assert result["compatible"] is False
    assert result["reason"] == "a frame shift drive only fits the FrameShiftDrive slot"


def test_core_module_power_plant_into_internal_slot_is_incompatible(tmp_path):
    """N1/S1, repro F4b: same bug, the power-plant branch. Before the fix
    this swapped the ship's real output even though the real plant was
    untouched, returning new_power_margin 9.39 instead of the true 5.79."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="power plant", module_class=5, module_rating="A",
        slot="Slot04_Size5",
    )
    assert result["found"] is True
    assert result["compatible"] is False
    assert result["reason"] == "a power plant only fits the PowerPlant slot"


def test_fuel_tank_into_undersized_internal_slot_is_incompatible(tmp_path):
    """N1/S2, repro F3: a fuel tank IS allowed in an internal slot (S1
    passes), but Slot05_Size4's real maximum is 4 (from its own name,
    boosted only by whatever's actually fitted there -- a class-3 cargo
    rack, not a class-6 tank). Before the fix, the standard-slot branch
    was picked by `grp` alone and used the standard FuelTank's size (6),
    returning compatible: True. This is exactly the path the C1 fix (round
    1) sends users down: "specify which slot" when internal tanks exist."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="fuel tank", module_class=6, module_rating="C",
        slot="Slot05_Size4",
    )
    assert result["found"] is True
    assert result["compatible"] is False
    # T3: "4" alone is satisfied by the slot name ("Slot05_Size4") even if
    # the size check were broken -- "max class 4" is only satisfied by the
    # real reason text.
    assert "max class 4" in result["reason"]


def test_utility_module_into_hardpoint_is_incompatible(tmp_path):
    """N3/S1, repro F5a: a shield booster (utility kind) can't go into a
    hardpoint mount, even a fitted one with real numbers behind it.
    Before the fix this returned compatible: True, new_power_margin 4.98."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="shield booster", module_class=0, module_rating="A",
        slot="MediumHardpoint2",  # fitted (a pulse laser) on the Type-9 fixture
    )
    assert result["found"] is True
    assert result["compatible"] is False


def test_weapon_module_into_internal_slot_is_incompatible(tmp_path):
    """N3/S1, repro F5b: a beam laser (weapon kind) can't go into an
    internal slot, even a fitted one."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="beam laser", module_class=1, module_rating="E",
        variant="hpt_beamlaser_fixed_small",
        slot="Slot05_Size4",  # fitted (a cargo rack) on the Type-9 fixture
    )
    assert result["found"] is True
    assert result["compatible"] is False


def test_hardpoint_that_doesnt_exist_on_the_ship_is_incompatible(tmp_path):
    """N3/S3, repro F13: the Type-9 has no Huge hardpoint at all
    ([2,2,2,1,1,0,0,0,0] -- three Medium, two Small, four Tiny, no Large,
    no Huge). A class-3 cannon (Hpt_Cannon_Turret_Large, rating D, the only
    class-3-rating-D cannon -- unambiguous) is a real weapon that fits a
    hardpoint KIND (S1 passes); this proves S3's existence check, not S1's
    kind check."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="cannon", module_class=3, module_rating="D",
        slot="HugeHardpoint1",
    )
    assert result["found"] is True
    assert result["compatible"] is False
    # T3: `compatible` alone doesn't prove which check caught it -- S1's
    # kind check would ALSO return compatible: False, with a different
    # reason. Only S3's existence check produces this text.
    assert "has no HugeHardpoint1" in result["reason"]


def test_unfitted_tiny_hardpoint_beyond_ships_count_is_incompatible(tmp_path):
    """S3: the Type-9 has 4 Tiny hardpoints (4 zeros in
    [2,2,2,1,1,0,0,0,0]) -- TinyHardpoint5 doesn't exist. A shield booster
    (utility kind) matches the slot KIND fine; S3's existence check is
    what catches this."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="shield booster", module_class=0, module_rating="A",
        slot="TinyHardpoint5",
    )
    assert result["found"] is True
    assert result["compatible"] is False
    # T3: proves S3's existence check specifically, not just any
    # compatible: False (S1's kind check can't fire here -- shield
    # booster is utility kind, TinyHardpoint5 is utility slot kind).
    assert "has no TinyHardpoint5" in result["reason"]


def test_unfitted_internal_slot_that_passes_existence_checks_gets_caveat(tmp_path):
    """S3(c): Slot06_Size4 isn't fitted on the Type-9 fixture and its NN
    (6) doesn't collide with any real fitted slot, and its size (4) is
    within the ship's largest plain internal class (8) -- both checks
    pass, so the swap proceeds but must carry a caveat: existence was
    inferred, not confirmed (empty internal slots aren't otherwise
    verifiable at all -- Review Focus #2)."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=4, module_rating="E",
        variant="int_cargorack_size4_class1", slot="Slot06_Size4",
    )
    assert result["found"] is True
    assert result["compatible"] is True
    assert "caveat" in result
    assert "Slot06_Size4" in result["caveat"]


def test_unfitted_internal_slot_number_collision_is_not_found(tmp_path):
    """S3(a): the Type-9 fixture really has Slot05_Size4 fitted. Asking for
    "Slot05_Size9" (same NN, wrong Size suffix) is simply the wrong name
    for that real slot, not a genuinely different, empty one -- found:
    False, not a compatibility question."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=4, module_rating="E",
        variant="int_cargorack_size4_class1", slot="Slot05_Size9",
    )
    assert result["found"] is False
    assert "Slot05_Size4" in result["reason"]


def test_torpedo_pylon_blueprint_not_in_allow_list_is_not_found(tmp_path):
    """N2/S4, repro F6: coriolis-data/modifications/modules.json allows only
    Weapon_LightWeight and Weapon_Sturdy for tp -- "Long range" isn't in
    that allow-list at all, regardless of what Coriolis's coarse applies_to
    ("Weapon") text would suggest."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="torpedo pylon", module_class=1, module_rating="I",
        slot="MediumHardpoint1",  # empty but real on explorer_nx (6 Medium hardpoints)
        blueprint_name="Long range", grade=5,
    )
    assert result["found"] is False
    assert "doesn't apply" in result["reason"]


def test_beam_laser_overcharged_does_not_pick_up_multicannons_clip(tmp_path):
    """N2/S4, repro F6b: "Overcharged" resolves to TWO different real
    blueprints (Weapon_Overcharged vs MC_Overcharged) depending on the grp.
    bl's is Weapon_Overcharged, which has no `clip` feature at all."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="beam laser", module_class=1, module_rating="E",
        variant="hpt_beamlaser_fixed_small", slot="MediumHardpoint1",
        blueprint_name="Overcharged", grade=1,
    )
    assert result["found"] is True
    assert "clip" not in result["unmodeled_blueprint_effects"]


def test_multicannon_overcharged_does_pick_up_its_own_clip(tmp_path):
    """N2/S4: the OTHER half of the silent-pick bug -- mc's "Overcharged"
    allow-list entry is MC_Overcharged specifically, which DOES have a
    `clip` feature (this code doesn't model it, so it must show up in
    unmodeled_blueprint_effects)."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="multi-cannon", module_class=1, module_rating="F",
        variant="hpt_multicannon_fixed_small", slot="MediumHardpoint1",
        blueprint_name="Overcharged", grade=1,
    )
    assert result["found"] is True
    assert "clip" in result["unmodeled_blueprint_effects"]


def test_module_class_as_numeric_string_matches_int(tmp_path):
    """N4/S5: a small model can easily pass module_class="5" instead of 5
    -- before the fix this raised a bare TypeError from `module_class >
    max_class`. Must give the SAME result as the int form."""
    _db(tmp_path)
    as_int = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    as_str = qf.simulate_module_swap(module_group="life support", module_class="5", module_rating="D")
    assert as_str == as_int
    assert as_str["found"] is True


def test_module_class_out_of_range_fails_cleanly(tmp_path):
    """N4/S5: garbage or out-of-range input (class is 0-8) must return a
    plain reason, never a crash."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="life support", module_class=99, module_rating="D")
    assert result["found"] is False

    result = qf.simulate_module_swap(module_group="life support", module_class="not a number", module_rating="D")
    assert result["found"] is False


def test_missing_optmass_stays_none_not_zero(tmp_path):
    """N6 (latent), strengthened per T3: exercises the REAL path end to
    end, not just the helper. No real fsd row lacks optmass (Q1), so this
    directly corrupts the currently-fitted FSD's module_stats.extra_json
    to remove it, then swaps something ELSE (life support, on the
    Type-9 fixture, whose FSD carries no journal Engineering block -- so
    the code must fall back to module_stats' base optmass, not a journal
    override). new_jump_range_max must come back None, not a near-zero number
    from routing that missing optmass through the NULL->0.0 helper that's
    correct for mass/power but wrong here.

    Confirmed genuinely RED on revert (see the fix-round-3 report): with
    the FSDOptimalMass lookup temporarily reverted to route through
    _engineered_or_base, this test fails with new_jump_range_max ~= 0.0 in
    a {"min", "max"} dict, not None."""
    path = _type9_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT extra_json FROM module_stats WHERE symbol = 'int_hyperdrive_size6_class1'"
    ).fetchone()
    extra = json.loads(row["extra_json"])
    del extra["optmass"]
    conn.execute(
        "UPDATE module_stats SET extra_json = ? WHERE symbol = 'int_hyperdrive_size6_class1'",
        (json.dumps(extra),),
    )
    conn.commit()
    conn.close()

    result = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="E")
    assert result["found"] is True
    assert result["new_jump_range_max"] is None


# ---------------------------------------------------------------------------
# Fix round 3: T1 (rejection messages name the right group; readable module
# names, not raw grp codes), T2 (grade validated only when used; whole-
# number floats accepted), T3 (see the three strengthened tests above).
# ---------------------------------------------------------------------------

def test_kind_mismatch_reason_uses_readable_name_not_raw_grp_code(tmp_path):
    """T1: the S1 kind-mismatch reason must never echo a raw grp code back
    at the commander (e.g. "a sb only fits a utility mount") -- it must
    read as a real module name ("a shield booster ...")."""
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="sb", module_class=0, module_rating="A",
        slot="MediumHardpoint2",  # fitted (a pulse laser) on the Type-9 fixture
    )
    assert result["compatible"] is False
    assert "a sb " not in result["reason"]
    assert "shield booster" in result["reason"]


def test_grade_zero_with_no_blueprint_is_ignored(tmp_path):
    """T2/NB2: grade is only validated when a blueprint is actually
    requested. A plain swap with grade=0 (a 3B model's plausible way of
    saying "no grade") must give the exact same result as no grade at
    all, not a spurious "out of range" rejection."""
    _db(tmp_path)
    with_zero = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D", grade=0)
    without = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    assert with_zero == without
    assert with_zero["found"] is True


def test_module_class_as_whole_number_float_matches_int(tmp_path):
    """T2/NB2: a whole-number float (5.0) must be accepted, giving the
    same result as the int form."""
    _db(tmp_path)
    as_int = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    as_float = qf.simulate_module_swap(module_group="life support", module_class=5.0, module_rating="D")
    assert as_float == as_int
    assert as_float["found"] is True


def test_module_class_as_fractional_float_fails_cleanly(tmp_path):
    """T2/NB2: a genuinely fractional value (5.5) is never a valid module
    class -- rejected with the existing plain reason, never silently
    truncated or rounded."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="life support", module_class=5.5, module_rating="D")
    assert result["found"] is False


# ---------------------------------------------------------------------------
# Current-load jump ranges, baselines and the pre-built summary.
#
# Why the result carries "old" figures and the ship's name: a real-model test
# showed a 3B model told only the NEW range invented "your range expands" and
# named the wrong ship. The summary is the same facts as one sentence.
# ---------------------------------------------------------------------------

FUEL_UNKNOWN = "current fuel isn't known yet (no refuel, jump or load event seen since the ship changed)"
CARGO_UNKNOWN = "current cargo isn't known yet (the game hasn't reported the cargo hold since the ship changed)"
FSD_UNKNOWN = "the fitted frame shift drive isn't in Ellie's module data"


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


def _type9_fsd_swap():
    """Stock 6E (optmass 960, maxfuel 5.3, fuelmul 0.011) -> standard 6A
    (optmass 1800, maxfuel 8, fuelmul 0.012, fuelpower 2.6). Same 40 t, so
    the unladen mass stays 1122.0."""
    return qf.simulate_module_swap(
        module_group="frame shift drive", module_class=6, module_rating="A",
        variant="int_hyperdrive_size6_class5",
    )


def test_swap_carries_baselines_and_current_load_ranges(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = _type9_fsd_swap()
    # every expected figure computed independently through loadout_calc
    old_now = compute_current_jump_range(1122.0, 10.0, 100, 960, 5.3, 0.011, 2.6)
    new_now = compute_current_jump_range(1122.0, 10.0, 100, 1800, 8, 0.012, 2.6)
    new_max = compute_jump_range(1122.0, 1800, 8, 0.012, 2.6)
    journal_max = 9.164523

    assert result["found"] is True and result["compatible"] is True
    assert result["ship_name"] == "Type-9 Heavy"
    assert result["fuel_now"] == 10.0
    assert result["cargo_now"] == 100
    assert result["state_note"] is None
    assert result["old_jump_range_now"] == 8.39 == round(old_now, 2)
    assert result["new_jump_range_now"] == 17.81 == round(new_now, 2)
    assert result["jump_range_change_now"] == 9.43 == round(new_now - old_now, 2)
    assert result["old_jump_range_max"] == 9.16
    assert result["new_jump_range_max"] == 19.42 == round(new_max, 2)
    assert result["jump_range_change_max"] == 10.26 == round(new_max - journal_max, 2)
    assert result["old_unladen_mass"] == 1122.0
    assert result["new_unladen_mass"] == 1122.0
    assert "new_jump_range" not in result  # renamed to new_jump_range_max


def test_swap_old_current_range_is_the_same_figure_get_current_loadout_reports(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    swap = _type9_fsd_swap()
    loadout = qf.get_current_loadout()
    assert loadout["jump_range_now"] == 8.39
    assert swap["old_jump_range_now"] == loadout["jump_range_now"]
    assert swap["old_jump_range_max"] == loadout["jump_range_max"]
    assert swap["ship_name"] == loadout["ship_name"]
    assert swap["fuel_now"] == loadout["fuel_now"] and swap["cargo_now"] == loadout["cargo_now"]


def test_swap_summary_exact_for_the_pinned_explorer_fsd_swap(tmp_path):
    """The same scenario as test_swap_unengineered_module_reports_deltas
    (the commander's real engineered Explorer NX FSD -> a stock 8A SCO), now
    with 40.0 t of fuel and 4 t of cargo aboard. Expected ranges computed
    independently through loadout_calc: now 71.8967 -> 46.2886 ly, max
    73.964989 (journal) -> 47.1453 ly."""
    path = _db(tmp_path)
    _set_ship_state(path, fuel_main=40.0, cargo_count=4)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5",
    )
    assert result["old_jump_range_now"] == 71.9
    assert result["new_jump_range_now"] == 46.29
    assert result["jump_range_change_now"] == -25.61
    assert result["old_jump_range_max"] == 73.96
    assert result["new_jump_range_max"] == 47.15
    assert result["jump_range_change_max"] == -26.82
    assert result["summary"] == (
        "Caspian Explorer: swapping int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii "
        "for int_hyperdrive_overcharge_size8_class5 in FrameShiftDrive: "
        "jump range now 71.9 -> 46.29 ly (-25.61); max 73.96 -> 47.15 ly (-26.82); "
        "unladen mass 1286.3 -> 1238.3 t (-48.00); power margin 14.55 MW."
    )


def test_swap_with_unknown_fuel_drops_the_current_clause_and_says_why(tmp_path):
    _db(tmp_path)  # a Loadout was seen but no fuel/cargo event yet
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5",
    )
    for key in ("fuel_now", "cargo_now", "old_jump_range_now", "new_jump_range_now", "jump_range_change_now"):
        assert result[key] is None, key
    assert result["state_note"] == FUEL_UNKNOWN
    assert result["new_jump_range_max"] == 47.15  # the maximum figures are unaffected
    assert result["summary"] == (
        "Caspian Explorer: swapping int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii "
        "for int_hyperdrive_overcharge_size8_class5 in FrameShiftDrive: "
        "max 73.96 -> 47.15 ly (-26.82); unladen mass 1286.3 -> 1238.3 t (-48.00); power margin 14.55 MW. "
        f"Current-load jump range unavailable: {FUEL_UNKNOWN}."
    )


def test_swap_with_unknown_cargo_uses_the_cargo_note(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=None)
    result = _type9_fsd_swap()
    assert result["state_note"] == CARGO_UNKNOWN
    assert result["old_jump_range_now"] is None and result["fuel_now"] is None
    assert result["summary"].endswith(f" Current-load jump range unavailable: {CARGO_UNKNOWN}.")


def test_swap_with_stale_fuel_does_not_relay_the_stale_number(tmp_path):
    _type9_now(tmp_path, fuel=99.0, cargo=100)  # tank is 64.0 t
    result = _type9_fsd_swap()
    assert result["fuel_now"] is None
    assert result["state_note"] == "the tracked fuel level doesn't fit this ship's tank, so it's probably stale"
    assert "99" not in result["summary"]


def test_swap_empty_slot_reads_adding_not_swapping(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(
        module_group="shield generator", module_class=4, module_rating="A", slot="Slot06_Size4",
    )
    assert result["found"] is True and result["removed"] == {"item": None}
    assert result["old_unladen_mass"] == 1122.0
    assert result["summary"] == (
        "Type-9 Heavy: adding int_shieldgenerator_size4_class5 in Slot06_Size4: "
        "jump range now 8.39 -> 8.32 ly (-0.07); max 9.16 -> 9.08 ly (-0.08); "
        "unladen mass 1122.0 -> 1132.0 t (+10.00); power margin 2.71 MW."
    )


def test_swap_with_unknown_power_margin_drops_the_margin_clause(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "Slot08_Size4", "int_notarealmodule_size4")  # On, but not in module_stats
    result = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    assert result["new_power_margin"] is None
    assert result["power_unknown_modules"] == ["int_notarealmodule_size4"]
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
        "jump range now 8.39 -> 8.47 ly (+0.08); max 9.16 -> 9.26 ly (+0.10); "
        "unladen mass 1122.0 -> 1110.0 t (-12.00)."
    )


def test_swap_ranged_blueprint_reads_between_and_omits_the_change(tmp_path):
    """Lightweight g1 on a life support: the module mass is a genuine roll
    range, so the unladen mass and both jump ranges are {"min", "max"}."""
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(
        module_group="life support", module_class=5, module_rating="D",
        blueprint_name="Lightweight", grade=1,
    )
    assert result["new_unladen_mass"] == {"min": 1106.4, "max": 1110.0}
    assert result["mass_change"] == {"min": -15.6, "max": -12.0}
    assert result["new_jump_range_max"] == {"min": 9.26, "max": 9.29}
    assert result["jump_range_change_max"] == {"min": 0.1, "max": 0.13}
    # now: unladen 1110.0 (heavier end) -> 8.47, unladen 1106.4 (lighter end) -> 8.49
    assert result["new_jump_range_now"] == {"min": 8.47, "max": 8.49}
    assert result["old_jump_range_now"] == 8.39
    assert result["jump_range_change_now"] == {"min": 0.08, "max": 0.11}
    # the old_* figures are always one plain number
    assert isinstance(result["old_jump_range_now"], float)
    assert isinstance(result["old_jump_range_max"], float)
    assert isinstance(result["old_unladen_mass"], float)
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
        "jump range now 8.39 -> between 8.47 and 8.49 ly; "
        "max 9.16 -> between 9.26 and 9.29 ly; "
        "unladen mass 1122.0 -> between 1106.4 and 1110.0 t; "
        "power margin 5.72 MW."
    )
    assert "(" not in result["summary"]  # no change in parentheses next to a range


def test_swap_ranged_power_margin_reads_between(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(
        module_group="power plant", module_class=6, module_rating="A",
        blueprint_name="Overcharged", grade=3, variant="int_powerplant_size6_class5",
    )
    assert result["new_power_margin"] == {"min": 18.98, "max": 20.74}
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_powerplant_size6_class1 for int_powerplant_size6_class5 in PowerPlant: "
        "jump range now 8.39 -> 8.52 ly (+0.14); max 9.16 -> 9.33 ly (+0.17); "
        "unladen mass 1122.0 -> 1102.0 t (-20.00); power margin between 18.98 and 20.74 MW."
    )


def test_swap_current_range_counts_a_fitted_booster_for_old_and_new_alike(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "Slot08_Size4", "int_guardianfsdbooster_size2")
    # swapping something else: the booster stays, +6 ly on both figures
    # (8.385687 + 6 -> 8.468170 + 6, computed through loadout_calc)
    other = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    assert other["old_jump_range_now"] == 14.39
    assert other["new_jump_range_now"] == 14.47
    assert other["jump_range_change_now"] == 0.08
    assert other["new_jump_range_max"] == 15.26
    # swapping the booster out: the removed booster adds nothing to the new
    # figure (1120.7 t unladen, no bonus -> 8.394545), so the loss shows up
    removed = qf.simulate_module_swap(
        module_group="cargo rack", module_class=4, module_rating="E",
        variant="int_cargorack_size4_class1", slot="Slot08_Size4",
    )
    assert removed["new_unladen_mass"] == 1120.7
    assert removed["old_jump_range_now"] == 14.39
    assert removed["new_jump_range_now"] == 8.39
    assert removed["jump_range_change_now"] == -5.99
    assert removed["new_jump_range_max"] == 9.18


def test_swap_current_range_with_the_fitted_fsd_missing_optmass_is_unknown(tmp_path):
    path = _type9_now(tmp_path)
    conn = sqlite3.connect(path)
    extra = json.loads(conn.execute("SELECT extra_json FROM module_stats WHERE symbol = 'int_hyperdrive_size6_class1'").fetchone()[0])
    del extra["optmass"]
    conn.execute("UPDATE module_stats SET extra_json = ? WHERE symbol = 'int_hyperdrive_size6_class1'", (json.dumps(extra),))
    conn.commit()
    conn.close()
    result = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")
    assert result["new_jump_range_max"] is None
    assert result["jump_range_change_max"] is None
    assert result["old_jump_range_now"] is None and result["new_jump_range_now"] is None
    assert result["state_note"] == FSD_UNKNOWN
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
        "unladen mass 1122.0 -> 1110.0 t (-12.00); power margin 5.72 MW. "
        f"Current-load jump range unavailable: {FSD_UNKNOWN}."
    )


def test_incompatible_and_not_found_results_have_no_summary(tmp_path):
    _type9_now(tmp_path)
    too_big = qf.simulate_module_swap(module_group="life support", module_class=8, module_rating="E")
    assert too_big["compatible"] is False and "summary" not in too_big
    nothing = qf.simulate_module_swap(module_group="frame shift drive", module_class=9, module_rating="A")
    assert nothing["found"] is False and "summary" not in nothing


def _numbers_in(value):
    """Every int/float anywhere inside a result (rounded to 2 dp)."""
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float)):
        return {round(float(value), 2)}
    if isinstance(value, dict):
        return set().union(*[_numbers_in(v) for v in value.values()]) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*[_numbers_in(v) for v in value]) if value else set()
    return set()


def _summary_numbers(result):
    """The standalone numbers in result["summary"] -- names that merely
    contain digits (Type-9 Heavy, Slot06_Size4, int_hyperdrive_size6_class5)
    are blanked out first."""
    text = result["summary"]
    for name in (result["ship_name"], result["removed"]["item"], result["added"]["symbol"], result["slot"], result["state_note"]):
        if name:
            text = text.replace(name, " ")
    return [float(m) for m in re.findall(r"(?<![\w.])[+-]?\d+(?:\.\d+)?", text)]


@pytest.mark.parametrize("scenario", ["fsd_swap", "empty_slot", "ranged_unladen", "ranged_margin", "unknown_fuel", "stale_fuel"])
def test_every_number_in_the_summary_is_in_the_structured_result(tmp_path, scenario):
    fuel, cargo = {"unknown_fuel": (None, None), "stale_fuel": (99.0, 100)}.get(scenario, (10.0, 100))
    _type9_now(tmp_path, fuel=fuel, cargo=cargo)
    calls = {
        "fsd_swap": dict(module_group="frame shift drive", module_class=6, module_rating="A", variant="int_hyperdrive_size6_class5"),
        "empty_slot": dict(module_group="shield generator", module_class=4, module_rating="A", slot="Slot06_Size4"),
        "ranged_unladen": dict(module_group="life support", module_class=5, module_rating="D", blueprint_name="Lightweight", grade=1),
        "ranged_margin": dict(module_group="power plant", module_class=6, module_rating="A", blueprint_name="Overcharged", grade=3, variant="int_powerplant_size6_class5"),
        "unknown_fuel": dict(module_group="frame shift drive", module_class=6, module_rating="A", variant="int_hyperdrive_size6_class5"),
        "stale_fuel": dict(module_group="frame shift drive", module_class=6, module_rating="A", variant="int_hyperdrive_size6_class5"),
    }
    result = qf.simulate_module_swap(**calls[scenario])
    assert result["found"] is True and result["compatible"] is True
    in_summary = _summary_numbers(result)
    assert in_summary, "the summary should contain numbers"
    in_result = _numbers_in({k: v for k, v in result.items() if k != "summary"})
    missing = [n for n in in_summary if round(n, 2) not in in_result]
    assert missing == [], f"summary numbers not in the result: {missing}\n{result['summary']}"


# ---------------------------------------------------------------------------
# Fix round 1: the swap carries the same jump check as get_current_loadout, and
# never crashes / prints "None" for a Loadout missing UnladenMass or MaxJumpRange.
# ---------------------------------------------------------------------------

OFF_BY_300 = "Ellie's mass estimate for your last jump was off by 300.0 t, so these jump ranges may be inaccurate."
NO_EMPTY_MASS = "the ship's empty mass isn't known yet"
TYPE9_FSD_ITEM = "int_hyperdrive_size6_class1"
LS_SWAP_SUMMARY = (
    "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
    "jump range now 8.39 -> 8.47 ly (+0.08); max 9.16 -> 9.26 ly (+0.10); "
    "unladen mass 1122.0 -> 1110.0 t (-12.00); power margin 5.72 MW."
)


def _consistent_last_jump(cargo=100):
    """Type-9 last-jump snapshot whose distance is built from the game's fuel
    formula for mass 1122.0 + 10.0 + 100 (optmass 960, fuelmul 0.011, power
    2.6, 5.3 t burned); `cargo` is what Ellie REMEMBERS, so 400 is 300 t off."""
    dist = 960 * (5.3 / 0.011) ** (1 / 2.6) / (1122.0 + 10.0 + 100)
    return dict(
        last_jump_dist=dist, last_jump_fuel_used=5.3, last_jump_fuel_before=10.0,
        last_jump_cargo=cargo, last_jump_unladen=1122.0, last_jump_fsd_item=TYPE9_FSD_ITEM,
        last_jump_boosted=0, last_jump_time="2026-10-23T09:34:43Z",
    )


def _ls_swap():
    return qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")


def test_swap_with_a_consistent_last_jump_has_no_warning(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump())
    result = _ls_swap()
    assert "warning" not in result
    assert result["summary"] == LS_SWAP_SUMMARY


def test_swap_carries_the_jump_check_warning_and_appends_it_to_the_summary(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))
    result = _ls_swap()
    assert result["warning"] == OFF_BY_300
    assert result["summary"] == LS_SWAP_SUMMARY + " " + OFF_BY_300
    assert result["summary"].endswith("power margin 5.72 MW. " + OFF_BY_300)
    # the same sentence get_current_loadout gives for the same state
    assert qf.get_current_loadout()["warning"] == result["warning"]
    assert result["old_jump_range_now"] == 8.39  # numbers still given; the warning qualifies them


def test_swap_warning_goes_after_the_unavailable_note_never_with_it(tmp_path):
    """No current range -> no warning (it would qualify nothing)."""
    path = _type9_now(tmp_path, fuel=None, cargo=None)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))
    result = _ls_swap()
    assert result["old_jump_range_now"] is None
    assert "warning" not in result
    assert "may be inaccurate" not in result["summary"]
    assert result["summary"].endswith(
        "Current-load jump range unavailable: current fuel isn't known yet "
        "(no refuel, jump or load event seen since the ship changed)."
    )


def test_swap_after_re_engineering_the_drive_has_no_false_warning(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump())
    event = json.load(open(FIXTURES / "type9_loadout_2026-01-04.json"))
    for module in event["Modules"]:
        if module["Slot"] == "FrameShiftDrive":
            module["Engineering"] = {"BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 0.6, "Modifiers": [
                {"Label": "FSDOptimalMass", "Value": 1488.0, "OriginalValue": 960.0, "LessIsGood": 0}]}
    conn = sqlite3.connect(path)
    jw.process_event(conn, event)
    conn.close()
    result = _ls_swap()
    assert "warning" not in result
    assert result["old_jump_range_now"] == 13.0  # computed with the drive as it is now


def test_swap_incompatible_results_carry_no_warning(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))
    too_big = qf.simulate_module_swap(module_group="life support", module_class=8, module_rating="E")
    assert too_big["compatible"] is False
    assert "warning" not in too_big and "summary" not in too_big


def test_swap_with_unknown_empty_mass_does_not_crash_and_says_why(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout SET unladen_mass = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    result = _ls_swap()
    assert result["found"] is True and result["compatible"] is True
    assert result["state_note"] == NO_EMPTY_MASS
    assert result["fuel_now"] is None and result["cargo_now"] is None
    assert result["old_jump_range_now"] is None and result["new_jump_range_now"] is None
    assert result["old_unladen_mass"] is None and result["new_unladen_mass"] is None
    assert result["new_jump_range_max"] is None and result["jump_range_change_max"] is None
    assert result["mass_change"] == -12.0  # the module's own mass difference is still known
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
        f"power margin 5.72 MW. Current-load jump range unavailable: {NO_EMPTY_MASS}."
    )
    assert "None" not in result["summary"]


def test_swap_with_unknown_max_jump_range_leaves_the_max_clause_out(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout SET max_jump_range = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    result = _ls_swap()
    assert result["old_jump_range_max"] is None
    assert result["jump_range_change_max"] is None
    assert result["new_jump_range_max"] == 9.26  # still computed
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport: "
        "jump range now 8.39 -> 8.47 ly (+0.08); "
        "unladen mass 1122.0 -> 1110.0 t (-12.00); power margin 5.72 MW."
    )
    assert "None" not in result["summary"]
