"""
Pure calculation functions for the ship loadout calculator -- no database,
no I/O, so these are cleanly unit-testable on their own. query_functions.py
is the only caller.

compute_jump_range's formula is EMPIRICALLY VERIFIED (2026-09-22), not
recalled from memory: computed against the commander's own real ship
(Journal.*.log Loadout event) gives 73.964990 ly against his real,
game-computed MaxJumpRange of 73.964989 -- a 0.000001 ly difference from
floating point only. Do not "simplify" this without re-verifying against
a real Loadout event's MaxJumpRange the same way.
"""


def compute_jump_range(unladen_mass, fsd_optmass, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus=0.0):
    """Max single-jump range in ly. total_mass is the ship's unladen mass
    plus exactly one jump's worth of fuel (fsd_maxfuel) -- that combination
    is what "max jump range" means in-game, verified above.

    booster_bonus: flat ly addition from Guardian FSD Booster if fitted (default 0.0).
    The booster contribution is applied as a post-computation flat addition based on
    coriolis-data's jumpboost field and standard community understanding of the module.
    Unlike the base formula, this has NOT been independently cross-verified against a
    real Loadout event from an actual boosted ship."""
    total_mass = unladen_mass + fsd_maxfuel
    return ((fsd_maxfuel / fsd_fuelmul) ** (1 / fsd_fuelpower)) * fsd_optmass / total_mass + booster_bonus


def apply_blueprint_delta(base_value, pct_range):
    """pct_range is Coriolis's own [min_pct, max_pct] (e.g. [0.45, 0.55] for
    a +45% to +55% roll-dependent change). Returns (min_value, max_value) --
    equal when the blueprint grade is deterministic for this stat
    (min_pct == max_pct), never a fabricated spread (Review Focus #5)."""
    min_pct, max_pct = pct_range
    return (base_value * (1 + min_pct), base_value * (1 + max_pct))
