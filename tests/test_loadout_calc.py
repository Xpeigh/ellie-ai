import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadout_calc import compute_jump_range, compute_current_jump_range, apply_blueprint_delta


def test_compute_jump_range_matches_real_ship():
    # Xpeigh's real Explorer NX, Journal.*.log Loadout event 2026-09-19T16:11:31Z.
    # Real, game-computed MaxJumpRange: 73.964989. unladen_mass/fsd_optmass are
    # the real reported values (UnladenMass, and the FSD's real engineered
    # FSDOptimalMass from Engineering.Modifiers). fsd_maxfuel/fuelmul/fuelpower
    # are the FSD's own base stats (FSD_LongRange doesn't touch these three).
    result = compute_jump_range(
        unladen_mass=1286.300049,
        fsd_optmass=7333.768066,
        fsd_maxfuel=6.8,
        fsd_fuelmul=0.011,
        fsd_fuelpower=2.5025,
    )
    assert abs(result - 73.964989) < 0.001


def test_compute_jump_range_lower_mass_gives_more_range():
    baseline = compute_jump_range(1286.300049, 7333.768066, 6.8, 0.011, 2.5025)
    lighter = compute_jump_range(1000.0, 7333.768066, 6.8, 0.011, 2.5025)
    assert lighter > baseline


def test_apply_blueprint_delta_deterministic_grade():
    # FSD_LongRange grade 1: mass [0.1, 0.1] -- deterministic, not a range.
    # Review Focus #5.
    lo, hi = apply_blueprint_delta(160.0, [0.1, 0.1])
    assert lo == hi == 176.0


def test_apply_blueprint_delta_matches_real_engineered_values():
    # FSD_LongRange grade 5 on Xpeigh's real FSD (also deterministic for
    # these two stats): base mass 160 -> real engineered 208.0;
    # base power 1.05 -> real engineered 1.2075.
    lo, hi = apply_blueprint_delta(160.0, [0.3, 0.3])
    assert lo == hi == 208.0
    lo, hi = apply_blueprint_delta(1.05, [0.15, 0.15])
    assert abs(lo - 1.2075) < 1e-9
    assert lo == hi


def test_apply_blueprint_delta_real_range():
    # FSD_LongRange grade 5, optmass: [0.45, 0.55] -- a genuine roll-dependent
    # range, not deterministic.
    lo, hi = apply_blueprint_delta(4670.0, [0.45, 0.55])
    assert abs(lo - 6771.5) < 0.01
    assert abs(hi - 7238.5) < 0.01
    assert lo < hi


def test_apply_blueprint_delta_sorts_a_larger_first_range():
    # Coriolis stores 155 real feature ranges larger-first when a blueprint
    # LOWERS the stat -- e.g. Lightweight grade 1's mass: [0, -0.45]. The
    # returned pair must still come out (min, max), not the raw input order.
    lo, hi = apply_blueprint_delta(10.0, [0, -0.45])
    assert (lo, hi) == (5.5, 10.0)


def test_compute_jump_range_with_guardian_fsd_booster():
    # Guardian FSD Booster (e.g. class 2) adds a flat ly bonus to jump range.
    # The booster contribution must be additive post-computation.
    without_booster = compute_jump_range(1286.300049, 7333.768066, 6.8, 0.011, 2.5025)
    with_booster = compute_jump_range(
        1286.300049, 7333.768066, 6.8, 0.011, 2.5025, booster_bonus=6
    )
    assert with_booster - without_booster == 6


# The commander's real Type-9 (tests/fixtures/type9_loadout_2026-01-04.json): its
# standard class 6 drive. The journal reported MaxJumpRange 9.164523 for it.
TYPE9_FSD = dict(fsd_optmass=960, fsd_maxfuel=5.3, fsd_fuelmul=0.011, fsd_fuelpower=2.6)
TYPE9_UNLADEN = 1122.0


def test_current_jump_range_full_jump_of_fuel_empty_hold_equals_max():
    # With exactly one jump of fuel on board and an empty hold, "now" and
    # "max" are the same ship: 9.164523 is also what the journal reported.
    now = compute_current_jump_range(TYPE9_UNLADEN, 5.3, 0, **TYPE9_FSD)
    assert now == pytest.approx(9.164523, abs=1e-6)
    assert now == pytest.approx(compute_jump_range(TYPE9_UNLADEN, **TYPE9_FSD), abs=1e-9)


def test_current_jump_range_more_fuel_weighs_more_but_burn_is_capped():
    # 16 t aboard: the jump still burns at most maxfuel (5.3), but the extra
    # 10.7 t of fuel is mass, so the range drops below the 5.3 t figure.
    assert compute_current_jump_range(TYPE9_UNLADEN, 16.0, 0, **TYPE9_FSD) == pytest.approx(9.078354, abs=1e-6)


def test_current_jump_range_counts_cargo_as_mass():
    assert compute_current_jump_range(TYPE9_UNLADEN, 10.0, 100, **TYPE9_FSD) == pytest.approx(8.385687, abs=1e-6)


def test_current_jump_range_less_fuel_than_a_full_jump_uses_the_fuel_on_board():
    # 1.0 t aboard is less than maxfuel, so min(maxfuel, fuel) is 1.0.
    assert compute_current_jump_range(TYPE9_UNLADEN, 1.0, 0, **TYPE9_FSD) == pytest.approx(4.843976, abs=1e-6)


def test_current_jump_range_empty_tank_is_zero():
    assert compute_current_jump_range(TYPE9_UNLADEN, 0.0, 0, **TYPE9_FSD) == 0.0


def test_current_jump_range_adds_booster_bonus():
    plain = compute_current_jump_range(TYPE9_UNLADEN, 10.0, 100, **TYPE9_FSD)
    boosted = compute_current_jump_range(TYPE9_UNLADEN, 10.0, 100, **TYPE9_FSD, booster_bonus=6)
    assert boosted - plain == pytest.approx(6, abs=1e-9)


def test_current_jump_range_of_an_empty_tank_is_zero_even_with_a_booster():
    # nothing to burn means no jump at all: the flat booster bonus must not turn 0 into 6
    assert compute_current_jump_range(TYPE9_UNLADEN, 0.0, 0, **TYPE9_FSD, booster_bonus=6) == 0.0
    assert compute_current_jump_range(TYPE9_UNLADEN, -1.0, 0, **TYPE9_FSD, booster_bonus=6) == 0.0
