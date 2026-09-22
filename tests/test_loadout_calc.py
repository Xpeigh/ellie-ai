import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadout_calc import compute_jump_range, apply_blueprint_delta


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
