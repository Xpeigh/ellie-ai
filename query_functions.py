"""
The query layer. Every function here returns data straight from SQLite --
the chat model's job is to pick the right function and phrase the
result, never to generate a material count or distance from its own
"knowledge." This file is the entire trust boundary for the project.

Precedence rule for blueprint lookups: your Excel sheet (graded_blueprints)
is checked first since it's the most specific and the one you maintain by
hand; Coriolis (coriolis_blueprints) is the fallback for anything the
sheet doesn't have filled in yet. See blueprint_mapping.py for why these
two don't share a join key directly.
"""
import functools
import json
import math
import sqlite3
from pathlib import Path

from blueprint_mapping import MODULE_TO_CORIOLIS_KEYWORDS
from loadout_calc import compute_jump_range, apply_blueprint_delta

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")

# Friendly name -> Coriolis's own grp code (verified against every real
# modules/{standard,internal,hardpoints}/*.json file's grp values).
# A group not listed here can still be addressed by its raw grp code.
MODULE_GROUP_TO_GRP = {
    "frame shift drive": "fsd", "fsd": "fsd",
    "power plant": "pp", "power distributor": "pd",
    "thrusters": "t", "life support": "ls", "sensors": "s",
    "fuel tank": "ft",
    "shield generator": "sg", "bi-weave shield generator": "bsg",
    "prismatic shield generator": "psg", "shield cell bank": "scb",
    "shield booster": "sb",
    "hull reinforcement": "hr", "module reinforcement": "mrp",
    "cargo rack": "cr", "large cargo rack": "crl",
    "fuel scoop": "fs", "docking computer": "dc",
    "guardian fsd booster": "gfsb",
    "auto field-maintenance unit": "am", "afmu": "am",
    "refinery": "rf", "fighter hangar": "fh",
    "beam laser": "bl", "burst laser": "ul", "pulse laser": "pl",
    "cannon": "c", "multi-cannon": "mc", "fragment cannon": "fc",
    "rail gun": "rg", "plasma accelerator": "pa", "mine launcher": "nl",
    "missile rack": "mr", "torpedo pylon": "tp", "chaff launcher": "ch",
    "heat sink launcher": "hs", "point defence": "po",
}

# The 7 standard modules are always exactly one per ship, with a FIXED
# journal slot name regardless of ship type or what's currently fitted --
# unlike optional-internal/hardpoint slots (see _resolve_target_slot's
# docstring and the plan's Review Focus #2 scope note, which is about
# "ordinary (non-reserved) slots" specifically), there's no size-to-name
# ambiguity to resolve for these. Names verified against the journal
# manual (elite-journal.readthedocs.io) and real Loadout events.
STANDARD_SLOT_NAMES = {
    "pp": "PowerPlant", "t": "MainEngines", "fsd": "FrameShiftDrive",
    "ls": "LifeSupport", "pd": "PowerDistributor", "s": "Radar", "ft": "FuelTank",
}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _needs_setup(func):
    """If the table(s) a function needs don't exist yet -- setup.py hasn't
    been run, or only some of its steps have -- SQLite raises
    OperationalError. Without this, that would crash ellie_chat's tool-call
    loop instead of letting the model tell the commander plainly that the
    data isn't there yet. Any OTHER OperationalError (a real bug) still
    raises rather than being swallowed here."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            return {"found": False, "reason": f"Ellie's database isn't fully set up yet ({e}). Run `python setup.py`."}
    return wrapper


def _materials_to_dict(materials_json):
    """Normalizes both storage shapes (Excel's list-of-{qty,item} and
    Coriolis's dict-of-{name:qty}) into one {name: qty} dict."""
    materials = json.loads(materials_json)
    if isinstance(materials, dict):
        return materials
    return {m["item"]: m["qty"] for m in materials if m.get("item")}


@_needs_setup
def get_blueprint(module_name: str, upgrade_name: str, grade: int) -> dict:
    """Materials required for a specific module/upgrade/grade blueprint.
    Checks the Excel sheet first (most specific), falls back to Coriolis's
    broader category data if the sheet doesn't have this one filled in.
    Returns {"found": False} if neither source has it."""
    grade = int(grade)  # models don't always return args matching the declared type (e.g. "3" not 3)
    conn = _connect()

    row = conn.execute(
        "SELECT materials_json, modifiers_text FROM graded_blueprints "
        "WHERE lower(module_name) = lower(?) AND lower(upgrade) = lower(?) AND grade = ?",
        (module_name, upgrade_name, grade),
    ).fetchone()
    if row:
        conn.close()
        return {
            "found": True,
            "source": "excel",
            "module": module_name,
            "upgrade": upgrade_name,
            "grade": grade,
            "materials": _materials_to_dict(row["materials_json"]),
            "effect": row["modifiers_text"],
        }

    keywords = MODULE_TO_CORIOLIS_KEYWORDS.get(module_name)
    if keywords:
        candidates = conn.execute(
            "SELECT applies_to, materials_json, feature_ranges_json FROM coriolis_blueprints "
            "WHERE grade = ? AND lower(upgrade_name) = lower(?)",
            (grade, upgrade_name),
        ).fetchall()
        for c in candidates:
            if any(kw in c["applies_to"].lower() for kw in keywords):
                conn.close()
                return {
                    "found": True,
                    "source": "coriolis",
                    "module": module_name,
                    "upgrade": upgrade_name,
                    "grade": grade,
                    "materials": _materials_to_dict(c["materials_json"]),
                    "effect": None,  # Coriolis gives numeric feature ranges, not descriptive text
                }

    conn.close()
    return {"found": False, "module": module_name, "upgrade": upgrade_name, "grade": grade}


@_needs_setup
def get_experimental_effect(module_name: str, effect_name: str) -> dict:
    """Materials required for an experimental effect (ungraded -- single
    cost, no grade 1-5). Checks Excel's ungraded_costs first, falls back
    to Coriolis's coriolis_experimental_effects."""
    conn = _connect()

    row = conn.execute(
        "SELECT materials_json, modifiers_text FROM ungraded_costs "
        "WHERE lower(module_name) = lower(?) AND lower(effect) = lower(?)",
        (module_name, effect_name),
    ).fetchone()
    if row:
        conn.close()
        return {
            "found": True,
            "source": "excel",
            "module": module_name,
            "effect_name": effect_name,
            "materials": _materials_to_dict(row["materials_json"]),
            "description": row["modifiers_text"],
        }

    row = conn.execute(
        "SELECT name, description, materials_json FROM coriolis_experimental_effects "
        "WHERE lower(name) = lower(?)",
        (effect_name,),
    ).fetchone()
    conn.close()
    if row:
        return {
            "found": True,
            "source": "coriolis",
            "module": module_name,
            "effect_name": row["name"],
            "materials": _materials_to_dict(row["materials_json"]),
            "description": row["description"],
        }

    return {"found": False, "module": module_name, "effect_name": effect_name}


def _material_key(name):
    """Elite's journal stores materials as compact, space-free internal
    names ('exceptionalscrambledemissiondata'), while the Excel sheet and
    Coriolis use the human-readable display name with spaces
    ('Exceptional Scrambled Emission Data'). Lowercasing alone isn't
    enough -- strip spaces/hyphens too, to reach the same canonical key
    the journal watcher already uses when it writes materials_inventory."""
    return name.lower().replace(" ", "").replace("-", "")


@_needs_setup
def missing_materials(module_name: str, upgrade_name: str, grade: int) -> dict:
    """Diffs a blueprint's requirement against what's actually on hand
    right now (materials_inventory, kept current by journal_watcher.py)."""
    grade = int(grade)
    blueprint = get_blueprint(module_name, upgrade_name, grade)
    if not blueprint["found"]:
        result = {"found": False, "module": module_name, "upgrade": upgrade_name, "grade": grade}
        if "reason" in blueprint:  # e.g. the database isn't set up -- don't let that read as "no such blueprint"
            result["reason"] = blueprint["reason"]
        return result

    conn = _connect()
    missing, have_enough = {}, True
    for material, needed_qty in blueprint["materials"].items():
        row = conn.execute(
            "SELECT count FROM materials_inventory WHERE material_name = ?",
            (_material_key(material),),
        ).fetchone()
        on_hand = row["count"] if row else 0
        if on_hand < needed_qty:
            missing[material] = {"need": needed_qty, "have": on_hand, "short_by": needed_qty - on_hand}
            have_enough = False
    conn.close()

    return {
        "found": True,
        "module": module_name,
        "upgrade": upgrade_name,
        "grade": grade,
        "have_everything": have_enough,
        "missing": missing,
        "required": blueprint["materials"],
        "source": blueprint["source"],
    }


@_needs_setup
def get_engineer(name: str) -> dict:
    """An engineer's location, prerequisites, and what they unlock."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM engineers WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if not row:
        conn.close()
        return {"found": False, "name": name}

    result = dict(row)
    if row["engineer_type"] == "ship_module":
        unlocks = conn.execute(
            "SELECT grade, item FROM engineer_unlocks WHERE engineer_name = ? ORDER BY grade",
            (row["name"],),
        ).fetchall()
        result["unlocks_by_grade"] = {u["grade"]: u["item"] for u in unlocks}
    else:
        upgrades = conn.execute(
            "SELECT upgrade FROM personal_upgrades WHERE engineer_name = ?", (row["name"],)
        ).fetchall()
        result["upgrades"] = [u["upgrade"] for u in upgrades]

    conn.close()
    result["found"] = True
    return result


# Rank name tables, sourced from the documented journal manual (elite-journal.readthedocs.io/en/latest/Appendix)
# -- translated here, in code, rather than left for the model to recall from
# its own training data. That's the same "don't trust the model's memory"
# principle the whole project is built on, just applied to rank names too.
COMBAT_RANKS = ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert", "Master", "Dangerous", "Deadly", "Elite"]
TRADE_RANKS = ["Penniless", "Mostly Penniless", "Peddler", "Dealer", "Merchant", "Broker", "Entrepreneur", "Tycoon", "Elite"]
EXPLORE_RANKS = ["Aimless", "Mostly Aimless", "Scout", "Surveyor", "Explorer", "Pathfinder", "Ranger", "Pioneer", "Elite"]
CQC_RANKS = ["Helpless", "Mostly Helpless", "Amateur", "Semi Professional", "Professional", "Champion", "Hero", "Legend", "Elite"]
FEDERATION_RANKS = ["None", "Recruit", "Cadet", "Midshipman", "Petty Officer", "Chief Petty Officer", "Warrant Officer",
                     "Ensign", "Lieutenant", "Lt. Commander", "Post Commander", "Post Captain", "Rear Admiral",
                     "Vice Admiral", "Admiral"]
EMPIRE_RANKS = ["None", "Outsider", "Serf", "Master", "Squire", "Knight", "Lord", "Baron", "Viscount", "Count",
                "Earl", "Marquis", "Duke", "Prince", "King"]


def _rank_name(table, value):
    if value is None:
        return None
    try:
        return table[int(value)]
    except (ValueError, IndexError):
        return None  # out-of-range rather than guessing a name


def player_state() -> dict:
    """Current commander status plus a materials summary, as tracked live
    by journal_watcher.py. Ranks are translated to their real in-game
    names here (not left for the model to recall). Materials are returned
    as per-category COUNTS, not a full itemized list -- Ben's own testing
    showed a model paraphrasing a ~150-item list into "selected examples"
    and quietly getting several exact numbers wrong in the process. Use
    get_material_count() for a specific material's exact figure instead.

    Unlike the other functions here this has no "found" key on success, so
    it can't share the @_needs_setup decorator -- a missing table gets its
    own try/except below, returning the same {status, materials_summary}
    shape (both empty) plus an "error" key, rather than a shape the caller
    isn't expecting."""
    try:
        return _player_state()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return {"status": {}, "materials_summary": {}, "error": f"Ellie's database isn't fully set up yet ({e}). Run `python setup.py`."}


def _player_state() -> dict:
    conn = _connect()
    status_row = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
    status = dict(status_row) if status_row else {}
    status.pop("id", None)

    status["combat_rank_name"] = _rank_name(COMBAT_RANKS, status.get("combat_rank"))
    status["trade_rank_name"] = _rank_name(TRADE_RANKS, status.get("trade_rank"))
    status["explore_rank_name"] = _rank_name(EXPLORE_RANKS, status.get("explore_rank"))
    status["cqc_rank_name"] = _rank_name(CQC_RANKS, status.get("cqc_rank"))
    status["federation_rank_name"] = _rank_name(FEDERATION_RANKS, status.get("federation_rank"))
    status["empire_rank_name"] = _rank_name(EMPIRE_RANKS, status.get("empire_rank"))

    materials = conn.execute(
        "SELECT category, COUNT(*) as distinct_materials, SUM(count) as total_units "
        "FROM materials_inventory WHERE count > 0 GROUP BY category"
    ).fetchall()
    conn.close()

    materials_summary = {
        m["category"] or "Unknown": {"distinct_materials": m["distinct_materials"], "total_units": m["total_units"]}
        for m in materials
    }

    return {"status": status, "materials_summary": materials_summary}


@_needs_setup
def get_material_count(material_name: str) -> dict:
    """Exact on-hand count for ONE specific material, by exact name. Use
    this for "how much X do I have" instead of trying to read it off
    player_state()'s summary."""
    conn = _connect()
    row = conn.execute(
        "SELECT display_name, category, count FROM materials_inventory WHERE material_name = ?",
        (_material_key(material_name),),
    ).fetchone()
    conn.close()
    if not row:
        return {"found": False, "material_name": material_name}
    return {"found": True, "material_name": row["display_name"], "category": row["category"], "count": row["count"]}


@_needs_setup
def distance_to_system(system_name: str) -> dict:
    """Straight-line distance (light years) from the commander's CURRENT
    system to a named system. The origin's coordinates come from the
    journal (StarPos, stored by journal_watcher.py), so it works from any
    system, including unpopulated ones. The destination comes from EDSM,
    and edsm_systems only holds POPULATED systems -- an unpopulated
    destination is reported as not found, with that reason spelled out so
    it isn't mistaken for "doesn't exist"."""
    conn = _connect()

    current = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
    if not current or not current["current_system"]:
        conn.close()
        return {"found": False, "reason": "current system unknown -- journal_watcher may not be running"}

    # journal coordinates first; the EDSM lookup covers a database the
    # watcher hasn't migrated/updated yet (no coordinate columns, or NULL)
    if "current_x" in current.keys() and current["current_x"] is not None:
        origin = {"x": current["current_x"], "y": current["current_y"], "z": current["current_z"]}
    else:
        origin = conn.execute(
            "SELECT x, y, z FROM edsm_systems WHERE lower(name) = lower(?)", (current["current_system"],)
        ).fetchone()
    destination = conn.execute(
        "SELECT name, x, y, z FROM edsm_systems WHERE lower(name) = lower(?)", (system_name,)
    ).fetchone()
    conn.close()

    if not origin:
        return {"found": False, "reason": f"coordinates for current system '{current['current_system']}' unknown"}
    if not destination:
        return {
            "found": False,
            "reason": f"'{system_name}' not found -- the system data only covers populated systems, so it may be unpopulated",
        }

    distance = math.sqrt(
        (origin["x"] - destination["x"]) ** 2
        + (origin["y"] - destination["y"]) ** 2
        + (origin["z"] - destination["z"]) ** 2
    )
    return {
        "found": True,
        "from": current["current_system"],
        "to": destination["name"],
        "distance_ly": round(distance, 2),
    }


@_needs_setup
def get_current_loadout() -> dict:
    """The commander's real, current ship as captured from the journal's
    own Loadout event -- ship, mass, jump range, and every fitted module
    with its real engineering modifiers where present. Unlike a
    hypothetical swap (simulate_module_swap), nothing here is computed;
    it's exactly what the game itself already reported."""
    conn = _connect()
    status = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
    if not status or not status["ship_key"]:
        conn.close()
        return {"found": False, "reason": "no Loadout event captured yet -- journal_watcher may not be running, or you haven't launched/undocked this session"}

    modules = conn.execute(
        "SELECT slot, item, is_on, priority, health, engineering_blueprint, "
        "engineering_level, engineering_quality, engineering_experimental, "
        "engineering_modifiers_json FROM current_loadout_modules ORDER BY slot"
    ).fetchall()
    conn.close()

    module_list = []
    for m in modules:
        entry = {
            "slot": m["slot"], "item": m["item"], "on": bool(m["is_on"]),
            "health": m["health"],
        }
        if m["engineering_blueprint"]:
            entry["engineering"] = {
                "blueprint": m["engineering_blueprint"],
                "level": m["engineering_level"],
                "quality": m["engineering_quality"],
                "experimental_effect": m["engineering_experimental"],
                "modifiers": json.loads(m["engineering_modifiers_json"]) if m["engineering_modifiers_json"] else [],
            }
        module_list.append(entry)

    return {
        "found": True,
        "ship": status["ship_key"],
        "max_jump_range": status["max_jump_range"],
        "unladen_mass": status["unladen_mass"],
        "cargo_capacity": status["cargo_capacity"],
        "fuel_main": status["fuel_main"],
        "fuel_reserve": status["fuel_reserve"],
        "modules": module_list,
    }


@_needs_setup
def get_ship_info(ship_name: str) -> dict:
    """Base (unengineered) stats and slot layout for a named ship, from
    coriolis-data via ingest_ship_modules.py. Matches by display name
    (e.g. "Python") case-insensitively."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM ship_stats WHERE lower(name) = lower(?)", (ship_name,)
    ).fetchone()
    conn.close()
    if not row:
        return {"found": False, "ship_name": ship_name}
    return {
        "found": True,
        "name": row["name"],
        "manufacturer": row["manufacturer"],
        "hull_mass": row["hull_mass"],
        "slots": json.loads(row["slots_json"]),
    }


def _module_group_to_grp(module_group):
    key = module_group.strip().lower()
    if key in MODULE_GROUP_TO_GRP:
        return MODULE_GROUP_TO_GRP[key]
    # accept a raw grp code directly too (e.g. "fsd"), case-insensitively
    known_grps = set(MODULE_GROUP_TO_GRP.values())
    if key in known_grps:
        return key
    return None


def _resolve_target_slot(conn, ship_key, grp, explicit_slot):
    """Which slot a swap targets. A standard module (fsd/pp/t/ls/pd/s/ft)
    always has exactly one fixed journal slot name, so that's resolved
    directly. Anything else (optional internals, hardpoints, utility
    mounts) is resolved from what's currently fitted, matched by module
    group. When NOTHING of that group is fitted, there's no verified way
    to map Coriolis's slot-size layout to the journal's own slot names
    (see the plan's Review Focus #2 scope note) -- rather than guess,
    this asks for an explicit slot, the same honest treatment bulkheads
    already get."""
    if explicit_slot:
        return explicit_slot, None
    if grp in STANDARD_SLOT_NAMES:
        return STANDARD_SLOT_NAMES[grp], None

    fitted = conn.execute(
        "SELECT clm.slot FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) "
        "WHERE ms.grp = ?", (grp,),
    ).fetchall()
    if len(fitted) == 1:
        return fitted[0]["slot"], None
    if len(fitted) > 1:
        return None, f"ship has {len(fitted)} slots of that type fitted -- specify which slot"
    return None, "nothing of that type is currently fitted, so there's no slot to resolve automatically -- specify slot explicitly (e.g. an empty utility slot's name)"


@_needs_setup
def simulate_module_swap(module_group: str, module_class: int, module_rating: str,
                          blueprint_name: str = None, grade: int = None, slot: str = None,
                          variant: str = None) -> dict:
    """'What if I fit X instead' -- swaps one module in the commander's
    CURRENT loadout for a hypothetical one and reports the real resulting
    mass/jump-range/power deltas. Never invents a number: an
    unrecognized module group, an incompatible class/rating, or a
    blueprint that doesn't apply to this module category all return a
    plain not-found/incompatible result instead."""
    grp = _module_group_to_grp(module_group)
    if grp is None:
        return {"found": False, "reason": f"'{module_group}' isn't a recognized module type"}

    conn = _connect()

    baseline = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
    if not baseline or not baseline["ship_key"]:
        conn.close()
        return {"found": False, "reason": "no current loadout captured yet -- journal_watcher may not be running"}

    target_slot, slot_error = _resolve_target_slot(conn, baseline["ship_key"], grp, slot)
    if slot_error:
        conn.close()
        return {"found": False, "reason": slot_error}

    # Several real modules can share one group/class/rating (standard vs SCO
    # FSDs, fixed/gimballed/turret mounts, standard vs Guardian power plants
    # -- 100 such combinations in coriolis-data, verified). Their stats differ
    # materially, so never pick one arbitrarily: narrow by `variant` if given,
    # otherwise list the candidates and ask, the same honest treatment an
    # ambiguous slot gets.
    candidates = conn.execute(
        "SELECT * FROM module_stats WHERE grp = ? AND class = ? AND lower(rating) = lower(?)",
        (grp, module_class, module_rating),
    ).fetchall()
    if variant:
        wanted = variant.strip().lower()
        exact = [c for c in candidates if c["symbol"] == wanted]
        candidates = exact or [c for c in candidates if wanted in _variant_search_text(c)]
    if not candidates:
        conn.close()
        suffix = f" matching '{variant}'" if variant else ""
        return {"found": False, "reason": f"no {module_group} of class {module_class} rating {module_rating}{suffix} exists"}
    if len(candidates) > 1:
        conn.close()
        return {
            "found": False,
            "reason": f"{len(candidates)} different modules are {module_group} class {module_class} rating {module_rating} -- specify which with `variant`",
            "variants": [_describe_variant(c) for c in candidates],
        }
    new_module = candidates[0]

    old_module_row = conn.execute(
        "SELECT * FROM current_loadout_modules WHERE slot = ?", (target_slot,)
    ).fetchone()
    old_stats = conn.execute(
        "SELECT * FROM module_stats WHERE symbol = ?",
        (old_module_row["item"].lower() if old_module_row else None,),
    ).fetchone() if old_module_row else None

    new_mass_min = new_mass_max = new_module["mass"] or 0.0
    new_power_min = new_power_max = new_module["power"] or 0.0
    blueprint_note = None
    unmodeled_keys = []

    if blueprint_name:
        # Match by the FULL friendly module_group phrase against Coriolis's
        # own applies_to text (e.g. "power plant" in "power plant"; "shield
        # booster" in "shield booster") -- verified against real data for
        # every category in MODULE_GROUP_TO_GRP before writing this. The
        # short grp code (e.g. "pp", "s", "t") is deliberately NOT used
        # here: single/double-letter codes are a near-guaranteed false-
        # positive substring match against arbitrary text. Note
        # blueprint_mapping.MODULE_TO_CORIOLIS_KEYWORDS is NOT reused here
        # either -- checked its actual keys while writing this plan, and it
        # only covers hardpoints/optional-internals/utility modules, not
        # core internals like the FSD or power plant.
        candidates = conn.execute(
            "SELECT applies_to, feature_ranges_json FROM coriolis_blueprints "
            "WHERE grade = ? AND lower(upgrade_name) = lower(?)",
            (grade, blueprint_name),
        ).fetchall() if grade else []
        match = None
        for c in candidates:
            if module_group.strip().lower() in c["applies_to"].lower():
                match = c
                break
        if not match:
            conn.close()
            return {"found": False, "reason": f"'{blueprint_name}' grade {grade} doesn't apply to {module_group}"}
        features = json.loads(match["feature_ranges_json"])
        applied_keys = set()
        if "mass" in features:
            new_mass_min, new_mass_max = apply_blueprint_delta(new_module["mass"], features["mass"])
            applied_keys.add("mass")
        if "power" in features:
            new_power_min, new_power_max = apply_blueprint_delta(new_module["power"], features["power"])
            applied_keys.add("power")
        if grp == "fsd" and "optmass" in features:
            applied_keys.add("optmass")  # applied separately below, in the jump-range section
        blueprint_note = blueprint_name
        # honesty check: a features key this code doesn't know how to apply
        # (e.g. a weapon's "damage"/"rof") must be reported, never silently
        # dropped -- the numbers below would otherwise look complete while
        # missing a real part of the blueprint's effect.
        unmodeled_keys = sorted(set(features) - applied_keys)

    old_mass = old_stats["mass"] if old_stats else 0.0
    old_power = old_stats["power"] if old_stats else 0.0
    mass_change_min = new_mass_min - old_mass
    mass_change_max = new_mass_max - old_mass
    new_unladen_min = baseline["unladen_mass"] + mass_change_min
    new_unladen_max = baseline["unladen_mass"] + mass_change_max

    # jump range needs the ship's EFFECTIVE fsd stats -- if the FSD itself
    # is the slot being swapped, that's the new module (+ blueprint, if
    # given); otherwise it's the CURRENT fsd's real reported stats
    # (preferring the journal's own engineering values over recomputed
    # blueprint math -- see Global Constraints).
    if grp == "fsd":
        fsd_optmass = new_module_extra_get(new_module, "optmass")
        fsd_maxfuel = new_module_extra_get(new_module, "maxfuel")
        fsd_fuelmul = new_module_extra_get(new_module, "fuelmul")
        fsd_fuelpower = new_module_extra_get(new_module, "fuelpower")
        if blueprint_name and "optmass" in features:
            optmass_min, optmass_max = apply_blueprint_delta(fsd_optmass, features["optmass"])
        else:
            optmass_min = optmass_max = fsd_optmass
    else:
        fsd_row = conn.execute(
            "SELECT clm.item, clm.engineering_modifiers_json, ms.* FROM current_loadout_modules clm "
            "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE clm.slot = 'FrameShiftDrive'"
        ).fetchone()
        base_optmass = new_module_extra_get(fsd_row, "optmass") if fsd_row else None
        fsd_maxfuel = new_module_extra_get(fsd_row, "maxfuel") if fsd_row else None
        fsd_fuelmul = new_module_extra_get(fsd_row, "fuelmul") if fsd_row else None
        fsd_fuelpower = new_module_extra_get(fsd_row, "fuelpower") if fsd_row else None
        optmass_min = optmass_max = base_optmass
        if fsd_row and fsd_row["engineering_modifiers_json"]:
            for mod in json.loads(fsd_row["engineering_modifiers_json"]):
                if mod["Label"] == "FSDOptimalMass":
                    optmass_min = optmass_max = mod["Value"]

    # Guardian FSD Booster: a flat additive jump-range bonus (loadout_calc.compute_jump_range's
    # booster_bonus parameter -- see that function's own docstring for the honest caveat that
    # this addition, unlike the base formula, isn't cross-verified against a real boosted ship).
    # Resolved the same way as the effective FSD stats above: if the booster ITSELF is the slot
    # being swapped, use the new module's jumpboost; otherwise use whatever booster (if any) is
    # currently fitted. No booster fitted and none being added -> 0.0, unchanged behavior.
    if grp == "gfsb":
        booster_bonus = new_module_extra_get(new_module, "jumpboost") or 0.0
    else:
        booster_row = conn.execute(
            "SELECT ms.extra_json FROM current_loadout_modules clm "
            "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE ms.grp = 'gfsb'"
        ).fetchone()
        booster_bonus = (new_module_extra_get(booster_row, "jumpboost") if booster_row else None) or 0.0

    conn.close()

    if None in (fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, optmass_min):
        new_range_min = new_range_max = None
    else:
        new_range_min = compute_jump_range(new_unladen_max, optmass_min, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)
        new_range_max = compute_jump_range(new_unladen_min, optmass_max, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)

    return {
        "found": True,
        "compatible": True,
        "slot": target_slot,
        "removed": {"item": old_module_row["item"] if old_module_row else None},
        "added": {
            "symbol": new_module["symbol"], "class": module_class, "rating": module_rating,
            "blueprint": blueprint_note,
            "mass": _min_max_or_single(new_mass_min, new_mass_max),
            "power": _min_max_or_single(new_power_min, new_power_max),
        },
        "mass_change": _min_max_or_single(mass_change_min, mass_change_max),
        "new_unladen_mass": _min_max_or_single(new_unladen_min, new_unladen_max),
        "new_jump_range": _min_max_or_single(new_range_min, new_range_max) if new_range_min is not None else None,
        "unmodeled_blueprint_effects": unmodeled_keys,  # honestly-reported, never silently dropped
    }


_MOUNT_WORDS = {"F": "fixed", "G": "gimballed", "T": "turret"}


def _variant_search_text(module_row):
    """What a `variant` string is matched against: the symbol, Coriolis's
    display name, and the mount spelled out -- so "gimballed", "gimbal",
    "sco", "mk ii", "guardian" or an exact symbol all work."""
    extra = json.loads(module_row["extra_json"] or "{}")
    name = extra.get("name") or extra.get("ukName") or ""
    mount = _MOUNT_WORDS.get(extra.get("mount"), "")
    return f"{module_row['symbol']} {name} {mount}".lower()


def _describe_variant(module_row):
    extra = json.loads(module_row["extra_json"] or "{}")
    return {
        "symbol": module_row["symbol"],
        "name": extra.get("name") or extra.get("ukName"),
        "mount": _MOUNT_WORDS.get(extra.get("mount")),
    }


def new_module_extra_get(module_row, key):
    """extra_json[key] for a module_stats row, or None if the row/key is
    absent. Small helper so the fsd-stat lookups above stay readable."""
    if module_row is None or module_row["extra_json"] is None:
        return None
    return json.loads(module_row["extra_json"]).get(key)


def _min_max_or_single(lo, hi, ndigits=2):
    """A genuine roll-dependent range as {"min", "max"}; collapses to one
    plain rounded number when lo == hi (deterministic) -- matches the
    spec's stated result shape and Review Focus #5: never present a
    fabricated spread when the real answer is exact."""
    lo_r, hi_r = round(lo, ndigits), round(hi, ndigits)
    return lo_r if lo_r == hi_r else {"min": lo_r, "max": hi_r}
