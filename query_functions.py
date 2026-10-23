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

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")


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

# Frontier added "prestige" tiers beyond the original Elite cap (Odyssey
# era) for the four ladders that top out at "Elite" -- Combat, Trade,
# Exploration, CQC. The documented journal manual above doesn't cover
# this (it was written before/without that update), so this table isn't
# sourced the same verified way as the rest -- found via Ben's own real
# commander data (trade_rank: 9, which the base 9-entry table above has
# no slot for) plus Frontier's own forums confirming "Elite I" through
# "Elite V" exist as named prestige tiers. The exact journal INTEGER for
# each tier is inferred (the obvious sequential extension: 8=Elite,
# 9=Elite I, ..., 13=Elite V), not confirmed against an official source --
# worth re-checking if a commander at Elite II+ ever reports a name that
# looks off.
_ELITE_PLUS = {9: "Elite I", 10: "Elite II", 11: "Elite III", 12: "Elite IV", 13: "Elite V"}


def _rank_name(table, value):
    """table[value], with a fallback for the Elite-plus prestige tiers
    (see _ELITE_PLUS above) on the four ladders that actually end in
    "Elite" -- Federation/Empire end in "Admiral"/"King" and never hit
    this path. A value this function doesn't recognize is labeled
    honestly rather than guessed at or silently dropped."""
    if value is None:
        return None
    try:
        value = int(value)
    except (ValueError, TypeError):
        return None
    if 0 <= value < len(table):
        return table[value]
    if table[-1] == "Elite":
        if value in _ELITE_PLUS:
            return _ELITE_PLUS[value]
        if value > max(_ELITE_PLUS):
            return f"Elite (prestige rank {value}, beyond the confirmed Elite I-V range)"
    return None  # genuinely out of range for a ladder that doesn't extend (Federation/Empire)


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
