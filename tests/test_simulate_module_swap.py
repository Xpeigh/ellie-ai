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
from loadout_calc import apply_blueprint_delta, compute_current_jump_range, compute_jump_range

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
    """Renamed from ..._is_incompatible_not_wrong_number: no class 1 frame shift
    drive exists for ANY ship (the smallest is class 2), so this is a "no such
    module" case (found: False) that reaches the candidate lookup, not the
    class-too-big-for-this-slot case (compatible: False), which
    test_swap_class_too_big_for_slot_returns_compatible_false covers with a class
    that DOES exist. (It used class 9, which is rejected as out of range 0-8
    before any lookup, so it never tested this.)"""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=1, module_rating="A")
    assert result["found"] is False
    assert result["reason"] == "no frame shift drive of class 1 rating A exists"


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
    # (Task 10a X8: the summary now also ends with the empty-slot caveat, pinned in
    # test_summary_ends_with_the_caveat_for_an_unfitted_internal_slot)
    assert result["summary"].startswith(
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
        "power margin 5.72 MW. "
        "Blueprint: Lightweight grade 1. Not reflected in these numbers: integrity."
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
        "unladen mass 1122.0 -> 1102.0 t (-20.00); power margin between 18.98 and 20.74 MW. "
        "Blueprint: Overcharged grade 3. Not reflected in these numbers: eff, integrity."
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


def test_swap_has_no_warning_when_there_is_no_current_range(tmp_path):
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


# ---------------------------------------------------------------------------
# Fix round 2
# ---------------------------------------------------------------------------

OLD_SHIP_STATE_DDL = """
    CREATE TABLE ship_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        fuel_main REAL, cargo_count INTEGER, fuel_updated TEXT, cargo_updated TEXT,
        last_jump_dist REAL, last_jump_fuel_used REAL, last_jump_fuel_before REAL,
        last_jump_cargo INTEGER, last_jump_unladen REAL, last_jump_fsd_item TEXT,
        last_jump_boosted INTEGER, last_jump_time TEXT
    )
"""
MALFORMED_MODIFIERS = ["{not json", "null", "{}", "[1, 2]", ""]


def _fsd_swap_6a():
    return qf.simulate_module_swap(
        module_group="frame shift drive", module_class=6, module_rating="A",
        variant="int_hyperdrive_size6_class5",
    )


def test_swaps_on_a_ship_state_table_the_watcher_has_not_migrated_yet_do_not_raise(tmp_path):
    """No last_jump_fsd_optmass column yet: the jump check stays quiet (even for
    a snapshot that looks 300 t off) and the range figures still work."""
    path = _type9_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ship_state")
    conn.execute(OLD_SHIP_STATE_DDL)
    conn.execute("INSERT INTO ship_state (id, fuel_main, cargo_count) VALUES (1, 10.0, 100)")
    for column, value in _consistent_last_jump(cargo=400).items():
        conn.execute(f"UPDATE ship_state SET {column} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()

    life_support = _ls_swap()
    assert "warning" not in life_support
    assert life_support["summary"] == LS_SWAP_SUMMARY

    fsd = _fsd_swap_6a()
    assert "warning" not in fsd
    assert fsd["old_jump_range_now"] == 8.39 and fsd["new_jump_range_now"] == 17.81
    assert "may be inaccurate" not in fsd["summary"]


@pytest.mark.parametrize("with_blueprint", [False, True])
@pytest.mark.parametrize("bad", MALFORMED_MODIFIERS)
def test_swaps_with_malformed_engineering_modifiers_on_the_drive_read_as_no_engineering(tmp_path, bad, with_blueprint):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    conn = sqlite3.connect(path)
    if with_blueprint:
        conn.execute(
            "UPDATE current_loadout_modules SET engineering_blueprint = 'FSD_LongRange', engineering_level = 5, "
            "engineering_modifiers_json = ? WHERE slot = 'FrameShiftDrive'", (bad,))
    else:
        conn.execute("UPDATE current_loadout_modules SET engineering_modifiers_json = ? WHERE slot = 'FrameShiftDrive'", (bad,))
    conn.commit()
    conn.close()

    fsd = _fsd_swap_6a()
    assert fsd["mass_change"] == 0.0               # the removed drive weighed its stock 40 t
    assert fsd["old_jump_range_now"] == 8.39 and fsd["new_jump_range_now"] == 17.81
    life_support = _ls_swap()
    assert life_support["old_jump_range_now"] == 8.39
    assert life_support["summary"] == LS_SWAP_SUMMARY


def test_swaps_with_no_base_optimal_mass_and_no_jump_time_one_have_no_warning_and_do_not_raise(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))   # would warn, were it judged
    conn = sqlite3.connect(path)
    extra = json.loads(conn.execute("SELECT extra_json FROM module_stats WHERE symbol = ?", (TYPE9_FSD_ITEM,)).fetchone()[0])
    del extra["optmass"]
    conn.execute("UPDATE module_stats SET extra_json = ? WHERE symbol = ?", (json.dumps(extra), TYPE9_FSD_ITEM))
    event = json.load(open(FIXTURES / "type9_loadout_2026-01-04.json"))
    for module in event["Modules"]:
        if module["Slot"] == "FrameShiftDrive":
            module["Engineering"] = {"BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 0.6, "Modifiers": [
                {"Label": "FSDOptimalMass", "Value": 1488.0, "OriginalValue": 960.0, "LessIsGood": 0}]}
    jw.process_event(conn, event)
    conn.close()

    for result in (_ls_swap(), _fsd_swap_6a()):
        assert result["old_jump_range_now"] == 13.0
        assert "warning" not in result
        assert "may be inaccurate" not in result["summary"]


def test_swap_summary_has_no_stray_colon_when_every_clause_is_unknown(tmp_path):
    """Unknown empty mass drops the jump and unladen clauses and an On module
    missing from module_stats drops the power margin: nothing is left to list."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "Slot08_Size4", "int_notarealmodule_size4")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout SET unladen_mass = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    result = _ls_swap()
    assert result["new_power_margin"] is None and result["new_unladen_mass"] is None
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_lifesupport_size5_class1 for int_lifesupport_size5_class2 in LifeSupport. "
        f"Current-load jump range unavailable: {NO_EMPTY_MASS}."
    )


# ---------------------------------------------------------------------------
# Task 10a, piece 1: swap-engine fixes (X1 capacities, X2 booster, X3 blueprint
# names, X6 slot spellings, X8 summary qualifiers)
# ---------------------------------------------------------------------------

TYPE9_P = (960, 5.3, 0.011, 2.6)   # the Type-9's stock drive: optmass, maxfuel, fuelmul, fuelpower


def _set_cargo_capacity(path, capacity, slot_modifiers=None):
    """Make current_loadout.cargo_capacity (and optionally one rack's journal
    modifiers) say what a real Loadout would."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout SET cargo_capacity = ? WHERE id = 1", (capacity,))
    for slot, modifiers in (slot_modifiers or {}).items():
        conn.execute(
            "UPDATE current_loadout_modules SET engineering_modifiers_json = ? WHERE slot = ?",
            (json.dumps(modifiers), slot))
    conn.commit()
    conn.close()


# --- X1: a swap caps fuel and cargo at the NEW capacities --------------------

def test_swapping_to_a_smaller_tank_caps_the_fuel_in_the_new_range(tmp_path):
    """64 t standard tank -> 8 t tank (fuel tank class 3): the ship can't hold
    the 10 t it has now, so the new range assumes 8 t. old_* and the reported
    fuel/cargo stay the real current values."""
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(module_group="fuel tank", module_class=3, module_rating="C")
    new_now = compute_current_jump_range(1122.0, 8.0, 100, *TYPE9_P)       # 8.3993, not the uncapped 8.3857
    assert result["new_jump_range_now"] == round(new_now, 2) == 8.4
    assert result["old_jump_range_now"] == 8.39
    assert result["jump_range_change_now"] == 0.01
    assert result["fuel_now"] == 10.0 and result["cargo_now"] == 100
    assert result["capacity_note"] == "the new tank holds only 8.0 t, so the new range assumes 8.0 t of fuel instead of 10.0 t"
    assert result["summary"] == (
        "Type-9 Heavy: swapping int_fueltank_size6_class3 for int_fueltank_size3_class3 in FuelTank: "
        "jump range now 8.39 -> 8.4 ly (+0.01); max 9.16 -> 9.16 ly (+0.00); "
        "unladen mass 1122.0 -> 1122.0 t (+0.00); power margin 5.79 MW. "
        "Note: the new tank holds only 8.0 t, so the new range assumes 8.0 t of fuel instead of 10.0 t."
    )


def test_a_tank_that_holds_what_is_aboard_has_no_capacity_note(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(module_group="fuel tank", module_class=4, module_rating="C")   # 16 t
    assert "capacity_note" not in result
    assert result["new_jump_range_now"] == 8.39
    assert "Note:" not in result["summary"]


def test_a_tank_exactly_as_big_as_the_fuel_aboard_is_not_a_cap(tmp_path):
    _type9_now(tmp_path, fuel=8.0, cargo=100)
    result = qf.simulate_module_swap(module_group="fuel tank", module_class=3, module_rating="C")   # 8 t
    assert "capacity_note" not in result


def test_removing_a_loaded_cargo_rack_caps_the_cargo_in_the_new_range(tmp_path):
    """348 t of racks (128+128+64+16+8+4); 340 t aboard; the 128 t rack in
    Slot00_Size8 becomes an 8 t one -> 348 - 128 + 8 = 228 t of hold."""
    _type9_now(tmp_path, fuel=10.0, cargo=340)
    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=3, module_rating="E",
        variant="int_cargorack_size3_class1", slot="Slot00_Size8",
    )
    assert result["old_jump_range_now"] == round(compute_current_jump_range(1122.0, 10.0, 340, *TYPE9_P), 2) == 7.02
    assert result["new_jump_range_now"] == round(compute_current_jump_range(1122.0, 10.0, 228.0, *TYPE9_P), 2) == 7.6
    assert result["cargo_now"] == 340
    assert result["capacity_note"] == "the new hold carries only 228.0 t, so the new range assumes 228.0 t of cargo instead of 340 t"
    assert result["summary"].endswith(
        " Note: the new hold carries only 228.0 t, so the new range assumes 228.0 t of cargo instead of 340 t.")


def test_an_engineered_racks_journal_capacity_is_what_gets_subtracted(tmp_path):
    """The rack in Slot00_Size8 is engineered to hold 200 t (base 128), so the
    Loadout's CargoCapacity is 420. Removing it leaves 420 - 200 + 8 = 228 t,
    and 300 t aboard no longer fit. Subtracting the base 128 would leave 300 and
    miss the cap."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=300)
    _set_cargo_capacity(path, 420.0, {"Slot00_Size8": [{"Label": "CargoCapacity", "Value": 200.0}]})
    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=3, module_rating="E",
        variant="int_cargorack_size3_class1", slot="Slot00_Size8",
    )
    assert result["capacity_note"] == "the new hold carries only 228.0 t, so the new range assumes 228.0 t of cargo instead of 300 t"
    assert result["new_jump_range_now"] == round(compute_current_jump_range(1122.0, 10.0, 228.0, *TYPE9_P), 2)


def test_adding_a_rack_never_caps_and_a_hypothetical_rack_uses_its_base_capacity(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=340)
    result = qf.simulate_module_swap(
        module_group="cargo rack", module_class=3, module_rating="E",
        variant="int_cargorack_size3_class1", slot="Slot06_Size4",     # empty: 348 + 8 = 356 t
    )
    assert "capacity_note" not in result
    assert result["new_jump_range_now"] == result["old_jump_range_now"]


def test_a_swap_that_touches_neither_tank_nor_rack_has_no_capacity_note(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=340)
    result = _ls_swap()
    assert "capacity_note" not in result


def test_no_current_range_means_nothing_to_cap(tmp_path):
    _type9_now(tmp_path, fuel=None, cargo=None)
    result = qf.simulate_module_swap(module_group="fuel tank", module_class=3, module_rating="C")
    assert result["new_jump_range_now"] is None
    assert "capacity_note" not in result


def test_both_caps_read_as_one_note_joined_with_a_semicolon():
    """A single swap can't shrink a tank and a rack at once, so the note
    builder is checked directly."""
    assert qf._capacity_note(10.0, 8.0, 340, 228.0) == (
        "the new tank holds only 8.0 t, so the new range assumes 8.0 t of fuel instead of 10.0 t; "
        "the new hold carries only 228.0 t, so the new range assumes 228.0 t of cargo instead of 340 t"
    )
    assert qf._capacity_note(10.0, 16.0, 340, 348.0) is None
    assert qf._capacity_note(10.0, None, 340, None) is None
    assert qf._capacity_note(10.0, -3.0, 0, 0.0) == (
        "the new tank holds only 0.0 t, so the new range assumes 0.0 t of fuel instead of 10.0 t")


# --- X2: no false mass-model warning on a ship with a Guardian booster -------

def test_a_fitted_guardian_booster_silences_the_jump_check_in_both_tools(tmp_path):
    """The check infers the ship's mass from the jump distance with no booster
    term, but the range model adds the booster's jumpboost; how the booster
    changes JumpDist is unverified, so the check stays out of it."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _set_ship_state(path, **_consistent_last_jump(cargo=400))
    assert qf.get_current_loadout()["warning"] == OFF_BY_300     # control: no booster -> warns
    assert _ls_swap()["warning"] == OFF_BY_300

    _insert_module(path, "Slot08_Size4", "int_guardianfsdbooster_size2")
    loadout = qf.get_current_loadout()
    swap = _ls_swap()
    assert "warning" not in loadout and "may be inaccurate" not in loadout["summary"]
    assert "warning" not in swap and "may be inaccurate" not in swap["summary"]
    assert loadout["jump_range_now"] == 14.39        # the booster's +6 is still in the range


# --- X3: blueprint names ------------------------------------------------------

FSD_BLUEPRINTS = "Faster boot sequence, Increased range, Shielded"
PD_BLUEPRINTS = "Charge enhanced, Engine focused, High charge capacity, Shielded, System focused, Weapon focused"


def _fsd_blueprint_swap(name, grade=1):
    return qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name=name, grade=grade, variant="int_hyperdrive_size7_class1",
    )


@pytest.mark.parametrize("spelling", [
    "FSD_LongRange", "fsd_longrange", "LongRange", "  Increased  range ", "increased-range",
    "INCREASED RANGE", "Increased_range", "increasedrange",
])
def test_blueprint_spellings_resolve_to_the_same_blueprint(tmp_path, spelling):
    _db(tmp_path)
    expected = _fsd_blueprint_swap("Increased range")
    assert expected["found"] is True and expected["added"]["blueprint"] == "Increased range"
    assert _fsd_blueprint_swap(spelling) == expected


def test_a_blueprint_that_exists_for_another_group_says_so_and_lists_this_groups(tmp_path):
    _db(tmp_path)
    result = _fsd_blueprint_swap("Overcharged")
    assert result == {
        "found": False,
        "reason": f"'Overcharged' grade 1 doesn't apply to frame shift drive. Blueprints available for frame shift drive: {FSD_BLUEPRINTS}.",
    }


def test_a_blueprint_spelled_as_a_journal_name_for_another_group_is_still_a_doesnt_apply(tmp_path):
    _db(tmp_path)
    result = _fsd_blueprint_swap("PowerPlant_Boosted")
    assert result["reason"] == (
        f"'PowerPlant_Boosted' grade 1 doesn't apply to frame shift drive. Blueprints available for frame shift drive: {FSD_BLUEPRINTS}.")


def test_a_made_up_blueprint_is_not_known(tmp_path):
    _db(tmp_path)
    result = _fsd_blueprint_swap("Totally Made Up")
    assert result == {
        "found": False,
        "reason": f"Ellie doesn't know a blueprint called 'Totally Made Up'. Blueprints available for frame shift drive: {FSD_BLUEPRINTS}.",
    }


def test_a_power_distributor_lists_its_own_blueprints(tmp_path):
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="power distributor", module_class=5, module_rating="A",
        variant="int_powerdistributor_size5_class5", blueprint_name="Totally Made Up", grade=1,
    )
    assert result["found"] is False
    assert result["reason"] == (
        f"Ellie doesn't know a blueprint called 'Totally Made Up'. Blueprints available for power distributor: {PD_BLUEPRINTS}.")
    other = qf.simulate_module_swap(
        module_group="power distributor", module_class=5, module_rating="A",
        variant="int_powerdistributor_size5_class5", blueprint_name="Increased range", grade=1,
    )
    assert other["reason"] == (
        f"'Increased range' grade 1 doesn't apply to power distributor. Blueprints available for power distributor: {PD_BLUEPRINTS}.")


def test_a_group_with_no_blueprints_keeps_its_existing_reason(tmp_path):
    _type9_db(tmp_path)
    result = qf.simulate_module_swap(module_group="fuel tank", module_class=3, module_rating="C",
                                     blueprint_name="Lightweight", grade=1)
    assert result == {"found": False, "reason": "no engineering blueprints exist for fuel tank in Ellie's data"}


# --- X6: non-canonical spellings of an occupied hardpoint / utility slot ----------

def _type9_with_booster_in_tiny_1(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "TinyHardpoint1", "hpt_shieldbooster_size0_class1")    # 0.5 t, 0.2 MW
    return path


def _booster_swap(slot):
    return qf.simulate_module_swap(module_group="shield booster", module_class=0, module_rating="A", slot=slot)


@pytest.mark.parametrize("spelling", [
    "Tiny Hardpoint 1", "tinyhardpoint01", "TINYHARDPOINT1", " tiny_hardpoint_1 ", "Tiny  Hardpoint  001",
])
def test_other_spellings_of_an_occupied_utility_slot_find_the_fitted_module(tmp_path, spelling):
    _type9_with_booster_in_tiny_1(tmp_path)
    canonical = _booster_swap("TinyHardpoint1")
    assert canonical["removed"] == {"item": "hpt_shieldbooster_size0_class1", "module": "Shield Booster", "class": 0, "rating": "E"}
    assert canonical["mass_change"] == 3.0 and canonical["power_change"] == 1.0
    assert _booster_swap(spelling) == canonical     # incl. slot == "TinyHardpoint1", the real name


def test_an_empty_utility_slot_spelled_loosely_is_canonicalised(tmp_path):
    _type9_with_booster_in_tiny_1(tmp_path)
    loose = _booster_swap("tiny hardpoint 02")
    assert loose["slot"] == "TinyHardpoint2"
    assert loose == _booster_swap("TinyHardpoint2")


def test_the_fitted_slots_own_name_wins_over_the_canonical_one(tmp_path):
    """Real journals name hardpoints canonically, so this is defensive: if the
    fitted slot's name is spelled differently from TinyHardpoint1 (here a
    zero-padded TinyHardpoint01), a loose spelling resolves to the REAL fitted
    name, not to a canonical name that would look like an empty slot."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "TinyHardpoint01", "hpt_shieldbooster_size0_class1")
    for spelling in ("Tiny Hardpoint 1", "tinyhardpoint1", "TINYHARDPOINT01"):
        result = _booster_swap(spelling)
        assert result["slot"] == "TinyHardpoint01"
        assert result["removed"] == {"item": "hpt_shieldbooster_size0_class1", "module": "Shield Booster", "class": 0, "rating": "E"}


def test_hardpoint_number_zero_is_rejected_with_a_plain_reason(tmp_path):
    _type9_with_booster_in_tiny_1(tmp_path)
    assert _booster_swap("TinyHardpoint0") == {
        "found": False, "reason": "'TinyHardpoint0' isn't a valid hardpoint slot -- numbering starts at 1"}
    assert _booster_swap("tiny hardpoint 00")["found"] is False


@pytest.mark.parametrize("slot", ["TinyHardpoint1x", "TinyHardpoint1 extra", "xTinyHardpoint1"])
def test_trailing_or_leading_text_is_not_a_hardpoint_slot(tmp_path, slot):
    _type9_with_booster_in_tiny_1(tmp_path)
    result = _booster_swap(slot)
    assert result["found"] is False
    assert result["reason"].startswith("Ellie can't simulate swaps in")


def test_a_mining_hardpoint_is_only_matched_to_a_fitted_mining_slot(tmp_path):
    """SmallMiningHardpoint1 fitted: its spaced/zero-padded spellings find it,
    but plain SmallHardpoint1 is a different (empty) slot."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    _insert_module(path, "SmallMiningHardpoint1", "hpt_beamlaser_fixed_small")
    fitted = qf.simulate_module_swap(module_group="beam laser", module_class=1, module_rating="E",
                                     variant="hpt_beamlaser_fixed_small", slot="SmallMiningHardpoint1")
    assert fitted["removed"] == {"item": "hpt_beamlaser_fixed_small", "module": "Beam Laser (fixed)", "class": 1, "rating": "E"}
    for spelling in ("small mining hardpoint 01", "SMALLMININGHARDPOINT1"):
        again = qf.simulate_module_swap(module_group="beam laser", module_class=1, module_rating="E",
                                        variant="hpt_beamlaser_fixed_small", slot=spelling)
        assert again == fitted
    plain = qf.simulate_module_swap(module_group="beam laser", module_class=1, module_rating="E",
                                    variant="hpt_beamlaser_fixed_small", slot="small hardpoint 1")
    assert plain.get("slot") != "SmallMiningHardpoint1" and plain.get("removed", {}).get("item") is None


# --- X8: the summary carries the qualifiers ----------------------------------------

def test_summary_names_the_blueprint_and_what_the_numbers_leave_out(tmp_path):
    """Explorer NX, beam laser into an empty MediumHardpoint1 with a Long range
    g1 blueprint: mass and power are modelled, three effects are not."""
    path = _db(tmp_path)
    _set_ship_state(path, fuel_main=40.0, cargo_count=4)
    result = qf.simulate_module_swap(
        module_group="beam laser", module_class=1, module_rating="E",
        variant="hpt_beamlaser_fixed_small", slot="MediumHardpoint1",
        blueprint_name="long  range", grade=1,
    )
    assert result["added"]["blueprint"] == "Long range"
    assert result["unmodeled_blueprint_effects"] == ["fallofffromrange", "range", "shotspeed"]
    assert "caveat" not in result       # an unfitted HARDPOINT slot is confirmed by the ship's layout
    assert result["summary"].endswith(
        " Blueprint: Long range grade 1. Not reflected in these numbers: fallofffromrange, range, shotspeed.")
    assert result["summary"].startswith(
        "Caspian Explorer: adding hpt_beamlaser_fixed_small in MediumHardpoint1: ")


def test_summary_ends_with_the_caveat_for_an_unfitted_internal_slot(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    result = qf.simulate_module_swap(module_group="shield generator", module_class=4, module_rating="A", slot="Slot06_Size4")
    assert result["summary"] == (
        "Type-9 Heavy: adding int_shieldgenerator_size4_class5 in Slot06_Size4: "
        "jump range now 8.39 -> 8.32 ly (-0.07); max 9.16 -> 9.08 ly (-0.08); "
        "unladen mass 1122.0 -> 1132.0 t (+10.00); power margin 2.71 MW. "
        "Slot06_Size4 isn't fitted in your current loadout, so Ellie can't confirm it's an empty slot on your ship"
        " -- these numbers assume it is."
    )


def test_summary_qualifiers_come_in_a_fixed_order_before_the_unavailable_note_and_the_warning(tmp_path):
    """Note (X1) -> Blueprint -> Not reflected -> caveat -> unavailable note."""
    result = {
        "ship_name": "Type-9 Heavy", "slot": "Slot06_Size4",
        "added": {"symbol": "x", "blueprint": "Lightweight", "blueprint_grade": 2},
        "removed": {"item": None},
        "old_jump_range_now": None, "new_jump_range_now": None, "jump_range_change_now": None,
        "old_jump_range_max": None, "new_jump_range_max": None, "jump_range_change_max": None,
        "old_unladen_mass": 1122.0, "new_unladen_mass": 1132.0, "mass_change": 10.0,
        "new_power_margin": 2.71,
        "capacity_note": "the new hold carries only 8.0 t, so the new range assumes 8.0 t of cargo instead of 9 t",
        "unmodeled_blueprint_effects": ["a", "b"],
        "caveat": "assumes an empty slot",
        "state_note": "current fuel isn't known yet",
        "warning": "WARN.",
    }
    assert qf._swap_summary(result) == (
        "Type-9 Heavy: adding x in Slot06_Size4: unladen mass 1122.0 -> 1132.0 t (+10.00); power margin 2.71 MW."
        " Note: the new hold carries only 8.0 t, so the new range assumes 8.0 t of cargo instead of 9 t."
        " Blueprint: Lightweight grade 2."
        " Not reflected in these numbers: a, b."
        " assumes an empty slot."
        " Current-load jump range unavailable: current fuel isn't known yet."
        " WARN."
    )
    result["caveat"] = "already ends with a period."
    assert " already ends with a period. Current-load" in qf._swap_summary(result)


# ---------------------------------------------------------------------------
# Task 10a X9: behaviours the final review found unpinned
# ---------------------------------------------------------------------------

def _module_stat(path, symbol, column):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {column} FROM module_stats WHERE symbol = ?", (symbol,)).fetchone()[0]
    finally:
        conn.close()


def _update_module(path, slot, **columns):
    conn = sqlite3.connect(path)
    for column, value in columns.items():
        conn.execute(f"UPDATE current_loadout_modules SET {column} = ? WHERE slot = ?", (value, slot))
    conn.commit()
    conn.close()


# --- power margin inputs --------------------------------------------------------

def test_switching_a_module_off_frees_its_power_in_the_margin(tmp_path):
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    baseline = _ls_swap()["new_power_margin"]
    assert baseline == 5.72
    shield_draw = _module_stat(path, "int_shieldgenerator_size6_class1", "power")      # 1.86 MW
    _update_module(path, "Slot03_Size6", is_on=0)
    margin = _ls_swap()["new_power_margin"]
    assert margin > 5.72 + 1.0
    assert margin == pytest.approx(5.72 + shield_draw, abs=0.011)


def test_an_engineered_power_plant_output_is_what_the_margin_uses(tmp_path):
    """The journal's own PowerCapacity (30 MW) replaces the base pgen (16.8)."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    base_output = json.loads(_module_stat(path, "int_powerplant_size6_class1", "extra_json"))["pgen"]
    assert base_output == 16.8
    _update_module(path, "PowerPlant", engineering_modifiers_json=json.dumps([{"Label": "PowerCapacity", "Value": 30.0}]))
    assert _ls_swap()["new_power_margin"] == pytest.approx(5.72 + (30.0 - 16.8), abs=0.011)


def test_an_engineered_power_draw_replaces_the_base_draw_in_the_margin(tmp_path):
    """Slot03_Size6's journal PowerDraw is 0.0, so the margin gains its whole base draw."""
    path = _type9_now(tmp_path, fuel=10.0, cargo=100)
    shield_draw = _module_stat(path, "int_shieldgenerator_size6_class1", "power")
    _update_module(path, "Slot03_Size6", engineering_modifiers_json=json.dumps([{"Label": "PowerDraw", "Value": 0.0}]))
    assert _ls_swap()["new_power_margin"] == pytest.approx(5.72 + shield_draw, abs=0.011)


# --- refusals ---------------------------------------------------------------------

def test_a_module_class_that_does_not_exist_is_refused_at_the_candidate_lookup(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("frame shift drive", 1, "A")
    assert result["found"] is False
    assert "exists" in result["reason"]
    assert result["reason"] == "no frame shift drive of class 1 rating A exists"


def test_a_variant_that_matches_nothing_is_named_in_the_refusal(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("frame shift drive", 1, "A", variant="no such drive")
    assert result["found"] is False
    assert "matching 'no such drive'" in result["reason"]


def test_an_unrecognised_module_already_in_the_target_slot_is_refused_by_name(tmp_path):
    path = _type9_now(tmp_path)
    _insert_module(path, "Slot08_Size4", "notarealmodule")
    result = qf.simulate_module_swap("cargo rack", 4, "E", variant="int_cargorack_size4_class1", slot="Slot08_Size4")
    assert result["found"] is False
    assert "notarealmodule" in result["reason"]
    assert result["reason"] == (
        "the module currently in Slot08_Size4 (notarealmodule) isn't in Ellie's module data, "
        "so the change can't be computed")


# --- guardian booster and optimal mass ---------------------------------------------

def test_swapping_a_guardian_booster_in_adds_its_bonus_to_the_new_max_range(tmp_path):
    """Explorer NX (real engineered drive: optmass 7333.768066, maxfuel 6.8,
    fuelmul 0.011, fuelpower 2.5025) with a class 2 Guardian booster (1.3 t,
    +6 ly) into the unfitted Slot08_Size4. Expected from loadout_calc."""
    path = _db(tmp_path)
    booster_mass = _module_stat(path, "int_guardianfsdbooster_size2", "mass")
    jumpboost = json.loads(_module_stat(path, "int_guardianfsdbooster_size2", "extra_json"))["jumpboost"]
    assert (booster_mass, jumpboost) == (1.3, 6)
    result = qf.simulate_module_swap("guardian fsd booster", 2, "H", slot="Slot08_Size4")
    assert result["found"] is True and result["compatible"] is True
    new_unladen = 1286.300049 + booster_mass
    assert result["new_unladen_mass"] == round(new_unladen, 2)
    expected = compute_jump_range(new_unladen, 7333.768066, 6.8, 0.011, 2.5025, booster_bonus=jumpboost)
    assert result["new_jump_range_max"] == round(expected, 2)
    assert result["new_jump_range_max"] - round(compute_jump_range(new_unladen, 7333.768066, 6.8, 0.011, 2.5025), 2) == pytest.approx(6, abs=0.011)


def _fsd_7e_stats(path):
    extra = json.loads(_module_stat(path, "int_hyperdrive_size7_class1", "extra_json"))
    return _module_stat(path, "int_hyperdrive_size7_class1", "mass"), extra


@pytest.mark.parametrize("grade, optmass_pct", [(1, [0, 0.15]), (5, [0.45, 0.55])])
def test_increased_range_new_max_range_is_the_min_max_of_the_optimal_mass_blueprint(tmp_path, grade, optmass_pct):
    """The stock 7E drive with an Increased range blueprint on the Explorer NX
    (whose real engineered 208 t drive it replaces). Mass is deterministic at
    every grade; optimal mass is a roll range, so the jump range is {min, max}.
    Grade 1's optmass range is [0, 0.15]; the [0.45, 0.55] range is grade 5's."""
    path = _db(tmp_path)
    mass, extra = _fsd_7e_stats(path)
    mass_pct = {1: [0.1, 0.1], 5: [0.3, 0.3]}[grade]
    new_mass = apply_blueprint_delta(mass, mass_pct)[0]
    new_unladen = 1286.300049 - 208.0 + new_mass
    optmass_lo, optmass_hi = apply_blueprint_delta(extra["optmass"], optmass_pct)
    args = (extra["maxfuel"], extra["fuelmul"], extra["fuelpower"])
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name="Increased range", grade=grade, variant="int_hyperdrive_size7_class1",
    )
    assert result["new_jump_range_max"] == {
        "min": round(compute_jump_range(new_unladen, optmass_lo, *args), 2),
        "max": round(compute_jump_range(new_unladen, optmass_hi, *args), 2),
    }
    assert result["new_jump_range_max"]["min"] < result["new_jump_range_max"]["max"]


# --- slot kind and size rules ---------------------------------------------------------

def test_a_shield_generator_does_not_fit_the_life_support_slot(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("shield generator", 4, "A", slot="LifeSupport")
    assert result["found"] is True and result["compatible"] is False
    assert "only fits an internal slot" in result["reason"]


def test_a_fuel_tank_with_no_slot_goes_in_the_standard_fuel_tank_slot(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("fuel tank", 5, "C")
    assert result["found"] is True and result["compatible"] is True
    assert result["slot"] == "FuelTank"


def test_an_oversize_rack_already_fitted_in_a_small_slot_sets_that_slots_size(tmp_path):
    """The Keelback-style exception: Slot05_Size4 really holds a class 5 rack,
    so a class 5 rack fits it again."""
    path = _type9_now(tmp_path)
    _update_module(path, "Slot05_Size4", item="int_cargorack_size5_class1")
    result = qf.simulate_module_swap("cargo rack", 5, "E", variant="int_cargorack_size5_class1", slot="Slot05_Size4")
    assert result["found"] is True and result["compatible"] is True
    assert result["removed"] == {"item": "int_cargorack_size5_class1", "module": "Cargo Rack", "class": 5, "rating": "E"}


def test_an_unfitted_internal_slot_is_accepted_with_a_caveat_if_the_ship_could_have_it(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("cargo rack", 4, "E", variant="int_cargorack_size4_class1", slot="Slot09_Size8")
    assert result["found"] is True and result["compatible"] is True
    assert result["caveat"] == (
        "Slot09_Size8 isn't fitted in your current loadout, so Ellie can't confirm it's an empty slot on your ship "
        "-- these numbers assume it is")


def test_an_internal_slot_bigger_than_any_the_ship_has_is_rejected(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("cargo rack", 4, "E", variant="int_cargorack_size4_class1", slot="Slot09_Size9")
    assert result["found"] is True and result["compatible"] is False
    assert result["reason"] == "your Type-9 Heavy has no size-9 internal slot"


def test_explorer_nx_life_support_class_6_is_too_big_for_its_class_5_slot(tmp_path):
    _db(tmp_path)
    result = qf.simulate_module_swap("life support", 6, "E")
    assert result["found"] is True and result["compatible"] is False
    assert "max class 5" in result["reason"]


def test_a_beam_laser_does_not_fit_a_utility_mount(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("beam laser", 1, "E", variant="hpt_beamlaser_fixed_small", slot="TinyHardpoint2")
    assert result["found"] is True and result["compatible"] is False
    assert "only fits a hardpoint mount" in result["reason"]


# ---------------------------------------------------------------------------
# Task 10b B3 (decoded `removed`), B4 (which slot?), B8 (thruster note), B9b
# ---------------------------------------------------------------------------

def _hardpoint_counts(path, ship_key):
    conn = sqlite3.connect(path)
    try:
        slots = json.loads(conn.execute("SELECT slots_json FROM ship_stats WHERE ship_key = ?", (ship_key,)).fetchone()[0])
    finally:
        conn.close()
    classes = [e if isinstance(e, int) else e["class"] for e in slots["hardpoints"]]
    return {word: classes.count(c) for word, c in (("tiny", 0), ("small", 1), ("medium", 2), ("large", 3), ("huge", 4))}


def _fitted(path):
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT slot FROM current_loadout_modules")}
    finally:
        conn.close()


def _free_names(path, ship_key, words):
    """Hardpoint names of these size words that nothing is fitted in, expected
    straight from the fixture's hardpoints minus its fitted slots."""
    counts = _hardpoint_counts(path, ship_key)
    fitted = _fitted(path)
    return [f"{word.capitalize()}Hardpoint{i}" for word in words for i in range(1, counts[word] + 1)
            if f"{word.capitalize()}Hardpoint{i}" not in fitted]


# --- B3: `removed` is decoded -----------------------------------------------------------

def test_removed_names_the_module_class_and_rating(tmp_path):
    _type9_now(tmp_path)
    removed = _fsd_swap_6a()["removed"]
    assert removed == {"item": "int_hyperdrive_size6_class1", "module": "FSD", "class": 6, "rating": "E"}


def test_removed_decodes_the_engineered_explorer_drive(tmp_path):
    _db(tmp_path)
    result = qf.simulate_module_swap("frame shift drive", 8, "A", variant="int_hyperdrive_overcharge_size8_class5")
    assert result["removed"] == {
        "item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
        "module": "Frame Shift Drive Mk II (SCO)", "class": 8, "rating": "A"}


def test_an_empty_slot_still_removes_nothing(tmp_path):
    _type9_now(tmp_path)
    assert qf.simulate_module_swap("shield generator", 4, "A", slot="Slot06_Size4")["removed"] == {"item": None}


def test_the_loadout_decodes_the_engineered_explorer_drive(tmp_path):
    """Display names come from the Coriolis tables: Increased range, Mass Manager."""
    _db(tmp_path)
    fsd = next(m for m in qf.get_current_loadout()["modules"] if m["slot"] == "FrameShiftDrive")
    assert fsd == {
        "slot": "FrameShiftDrive", "module": "Frame Shift Drive Mk II (SCO)", "class": 8, "rating": "A",
        "on": True, "health": 1.0, "engineering": "Increased range g5, Mass Manager"}


def test_engineering_names_fall_back_one_by_one_to_the_journal_names(tmp_path):
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout_modules SET engineering_blueprint = 'Weird_Thing', engineering_level = 3, "
                 "engineering_experimental = 'special_fsd_heavy' WHERE slot = 'FrameShiftDrive'")
    conn.commit()
    conn.close()
    fsd = next(m for m in qf.get_current_loadout()["modules"] if m["slot"] == "FrameShiftDrive")
    assert fsd["engineering"] == "Weird_Thing g3, Mass Manager"          # unknown blueprint, known experimental
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout_modules SET engineering_blueprint = 'FSD_LongRange', engineering_experimental = 'special_not_real'")
    conn.commit()
    conn.close()
    fsd = next(m for m in qf.get_current_loadout()["modules"] if m["slot"] == "FrameShiftDrive")
    assert fsd["engineering"] == "Increased range g3, special_not_real"   # known blueprint, unknown experimental
    conn = sqlite3.connect(path)
    conn.execute("UPDATE current_loadout_modules SET engineering_experimental = NULL")
    conn.commit()
    conn.close()
    fsd = next(m for m in qf.get_current_loadout()["modules"] if m["slot"] == "FrameShiftDrive")
    assert fsd["engineering"] == "Increased range g3"


# --- B4: "which slot?" replies list real slot names ------------------------------------------

def test_a_utility_module_with_no_slot_lists_the_free_mounts_of_the_type_9(tmp_path):
    path = _type9_now(tmp_path)
    free = _free_names(path, "type_9_heavy", ["tiny"])
    assert free == ["TinyHardpoint1", "TinyHardpoint2", "TinyHardpoint3", "TinyHardpoint4"]
    result = qf.simulate_module_swap("heat sink launcher", 0, "I")
    assert result == {
        "found": False,
        "reason": f"Which utility mount? Your Type-9 Heavy has {len(free)} utility mounts; free right now: "
                  f"{', '.join(free)}. Name one with slot.",
    }
    assert result["reason"] == (
        "Which utility mount? Your Type-9 Heavy has 4 utility mounts; free right now: "
        "TinyHardpoint1, TinyHardpoint2, TinyHardpoint3, TinyHardpoint4. Name one with slot.")


def test_the_utility_reply_leaves_out_mounts_that_are_in_use(tmp_path):
    path = _type9_now(tmp_path)
    _insert_module(path, "TinyHardpoint1", "hpt_shieldbooster_size0_class1")
    _insert_module(path, "TinyHardpoint3", "hpt_chafflauncher_tiny")
    free = _free_names(path, "type_9_heavy", ["tiny"])
    assert free == ["TinyHardpoint2", "TinyHardpoint4"]
    reason = qf.simulate_module_swap("heat sink launcher", 0, "I")["reason"]
    assert reason == "Which utility mount? Your Type-9 Heavy has 4 utility mounts; free right now: TinyHardpoint2, TinyHardpoint4. Name one with slot."


def test_a_utility_reply_on_the_explorer_nx(tmp_path):
    path = _db(tmp_path)
    free = _free_names(path, "explorer_nx", ["tiny"])
    assert len(free) == 6
    assert qf.simulate_module_swap("heat sink launcher", 0, "I")["reason"] == (
        f"Which utility mount? Your Caspian Explorer has 6 utility mounts; free right now: {', '.join(free)}. Name one with slot.")


def test_every_utility_mount_in_use_says_so_and_names_the_fitted_ones(tmp_path):
    path = _type9_now(tmp_path)
    for i in (4, 2, 1, 3):
        _insert_module(path, f"TinyHardpoint{i}", "hpt_chafflauncher_tiny")
    reason = qf.simulate_module_swap("heat sink launcher", 0, "I")["reason"]
    assert reason == (
        "Your Type-9 Heavy has 4 utility mounts and all of them are in use; to replace one, name it with slot: "
        "TinyHardpoint1, TinyHardpoint2, TinyHardpoint3, TinyHardpoint4.")


def test_a_weapon_with_no_slot_lists_free_hardpoints_of_every_size_that_can_hold_it(tmp_path):
    """Type-9: 2 small, 3 medium, none larger; MediumHardpoint2 is fitted. A
    class 2 weapon only fits the medium ones."""
    path = _type9_now(tmp_path)
    free = _free_names(path, "type_9_heavy", ["medium"])
    assert free == ["MediumHardpoint1", "MediumHardpoint3"]
    assert qf.simulate_module_swap("cannon", 2, "D")["reason"] == (
        "Which hardpoint? Your Type-9 Heavy has 3 hardpoints that fit a class 2 weapon; free right now: "
        "MediumHardpoint1, MediumHardpoint3. Name one with slot.")
    # a class 1 weapon also fits the small ones
    small_and_medium = _free_names(path, "type_9_heavy", ["small", "medium"])
    assert qf.simulate_module_swap("beam laser", 1, "E", variant="hpt_beamlaser_fixed_small")["reason"] == (
        f"Which hardpoint? Your Type-9 Heavy has 5 hardpoints that fit a class 1 weapon; free right now: "
        f"{', '.join(small_and_medium)}. Name one with slot.")
    assert small_and_medium == ["SmallHardpoint1", "SmallHardpoint2", "MediumHardpoint1", "MediumHardpoint3"]


def test_a_weapon_with_every_fitting_hardpoint_in_use(tmp_path):
    path = _type9_now(tmp_path)
    for slot in ("MediumHardpoint1", "MediumHardpoint3"):
        _insert_module(path, slot, "hpt_pulselaser_fixed_small")
    assert qf.simulate_module_swap("cannon", 2, "D")["reason"] == (
        "Your Type-9 Heavy has 3 hardpoints that fit a class 2 weapon and all of them are in use; "
        "to replace one, name it with slot: MediumHardpoint1, MediumHardpoint2, MediumHardpoint3.")


def test_a_weapon_too_big_for_every_hardpoint_the_ship_has(tmp_path):
    _type9_now(tmp_path)
    assert qf.simulate_module_swap("cannon", 4, "B")["reason"] == "Your Type-9 Heavy has no hardpoint that fits a class 4 weapon."


def test_an_internal_module_with_nothing_of_its_kind_fitted_lists_fitted_internal_slots(tmp_path):
    _type9_now(tmp_path)
    reason = qf.simulate_module_swap("fuel scoop", 3, "E")["reason"]
    assert reason == (
        "Which internal slot? Name one with slot, for example one of your fitted internal slots: "
        "Slot00_Size8 (Cargo Rack), Slot01_Size8 (Cargo Rack), Slot02_Size7 (Cargo Rack), "
        "Slot03_Size6 (Shield Generator), Slot04_Size5 (Cargo Rack), Slot05_Size4 (Cargo Rack). "
        "Ellie cannot see empty internal slots in the journal.")


def test_the_internal_reply_names_at_most_six_slots(tmp_path):
    _type9_now(tmp_path)
    reason = qf.simulate_module_swap("fuel scoop", 3, "E")["reason"]
    assert reason.count("Slot") == 6 and "Slot07_Size3" not in reason


def test_two_fitted_modules_of_the_same_kind_list_their_slots(tmp_path):
    """Not a new reply, but it used to name no slots either."""
    _type9_now(tmp_path)
    reason = qf.simulate_module_swap("cargo rack", 5, "E", variant="int_cargorack_size5_class1")["reason"]
    assert reason.startswith("ship has 6 slots of that type fitted -- specify which slot: ")
    assert "Slot00_Size8" in reason and "Slot07_Size3" in reason


def test_a_named_slot_still_works_after_the_reply(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("heat sink launcher", 0, "I", slot="TinyHardpoint3")
    assert result["found"] is True and result["slot"] == "TinyHardpoint3"


@pytest.mark.parametrize("junk", ["None", "none", "NONE", "null", "undefined", "", "  ", " None "])
def test_junk_strings_for_the_optional_arguments_mean_not_given(tmp_path, junk):
    """llama3.2 passed slot="None". A real slot, variant or blueprint can't be called that."""
    _type9_now(tmp_path)
    plain = _ls_swap()
    assert qf.simulate_module_swap("life support", 5, "D", slot=junk) == plain
    assert qf.simulate_module_swap("life support", 5, "D", variant=junk) == plain
    assert qf.simulate_module_swap("life support", 5, "D", blueprint_name=junk, grade=3) == plain


def test_junk_slot_on_a_utility_module_gets_the_which_mount_reply(tmp_path):
    _type9_now(tmp_path)
    assert qf.simulate_module_swap("heat sink launcher", 0, "I", slot="None")["reason"].startswith("Which utility mount?")


# --- B8: thrusters can be rated below the loaded ship ------------------------------------------------

def test_a_thruster_rated_below_the_loaded_mass_gets_a_note(tmp_path):
    """Type-9, class 5 thrusters (maxmass 840): the ship loaded is 1122 - 80 + 20
    + 64 t of fuel + 348 t of cargo = 1,474 t."""
    path = _type9_now(tmp_path)
    maxmass = json.loads(_module_stat(path, "int_engine_size5_class1", "extra_json"))["maxmass"]
    new_mass = _module_stat(path, "int_engine_size5_class1", "mass")
    old_mass = _module_stat(path, "int_engine_size7_class1", "mass")
    full = 1122.0 - old_mass + new_mass + 64.0 + 348.0
    assert maxmass < full
    result = qf.simulate_module_swap("thrusters", 5, "E", variant="int_engine_size5_class1")
    note = f"Thrusters is rated for ships up to {maxmass} t; fully loaded, your ship would weigh {round(full, 2)} t."
    assert result["mass_note"] == note == "Thrusters is rated for ships up to 840 t; fully loaded, your ship would weigh 1474.0 t."
    assert result["summary"].endswith(f" Note: {note}")
    assert result["new_fuel_capacity"] == 64.0 and result["new_cargo_capacity"] == 348.0


def test_the_thruster_note_comes_after_the_capacity_note():
    # a swap that cuts capacity can't also be a thruster swap, so the order is pinned on the builder
    built = qf._swap_summary({
        "ship_name": "S", "slot": "X", "added": {"symbol": "y", "blueprint": None}, "removed": {"item": None},
        "old_jump_range_now": None, "new_jump_range_now": None, "jump_range_change_now": None,
        "old_jump_range_max": None, "new_jump_range_max": None, "jump_range_change_max": None,
        "old_unladen_mass": None, "new_unladen_mass": None, "mass_change": None, "new_power_margin": None,
        "capacity_note": "CAP", "mass_note": "Thrusters is rated for 1 t.", "state_note": None,
    })
    assert built == "S: adding y in X. Note: CAP. Note: Thrusters is rated for 1 t."


def test_a_thruster_the_ship_does_not_outweigh_has_no_mass_note(tmp_path):
    _type9_now(tmp_path)
    result = qf.simulate_module_swap("thrusters", 7, "D")          # maxmass 2430 vs about 1,534 t loaded
    assert result["found"] is True and result["compatible"] is True
    assert "mass_note" not in result and "Note:" not in result["summary"]


def test_only_thrusters_get_a_mass_note(tmp_path):
    """Shield generators are exceeded in many real setups, so they carry no note."""
    path = _type9_now(tmp_path)
    maxmass = json.loads(_module_stat(path, "int_shieldgenerator_size3_class1", "extra_json"))["maxmass"]
    assert maxmass < 1534
    result = qf.simulate_module_swap("shield generator", 3, "E", slot="Slot03_Size6")
    assert result["found"] is True and "mass_note" not in result


# --- B9b: the capacities behind capacity_note are in the result -------------------------------------------

def test_every_compatible_swap_reports_the_new_capacities(tmp_path):
    _type9_now(tmp_path, fuel=10.0, cargo=100)
    plain = _ls_swap()
    assert (plain["new_fuel_capacity"], plain["new_cargo_capacity"]) == (64.0, 348.0)
    tank = qf.simulate_module_swap("fuel tank", 3, "C")
    assert tank["new_fuel_capacity"] == 8.0 and tank["new_cargo_capacity"] == 348.0
    assert str(tank["new_fuel_capacity"]) in tank["capacity_note"]
    rack = qf.simulate_module_swap("cargo rack", 3, "E", variant="int_cargorack_size3_class1", slot="Slot00_Size8")
    assert rack["new_cargo_capacity"] == 228.0 and rack["new_fuel_capacity"] == 64.0


def test_the_capacities_are_there_even_when_there_is_no_current_load(tmp_path):
    _type9_now(tmp_path, fuel=None, cargo=None)
    result = qf.simulate_module_swap("fuel tank", 3, "C")
    assert result["new_fuel_capacity"] == 8.0 and result["new_cargo_capacity"] == 348.0
    assert "capacity_note" not in result


def test_incompatible_swaps_have_no_capacities(tmp_path):
    _type9_now(tmp_path)
    assert "new_fuel_capacity" not in qf.simulate_module_swap("life support", 8, "E")
