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
import difflib
import functools
import json
import math
import re
import sqlite3
from pathlib import Path

from blueprint_mapping import (BLUEPRINT_ALIASES, ENGINEER_ALIASES, GENERIC_EXTRA_WORDS, GROUP_SYNONYMS,
                               clean_material_name, engineer_key, name_tokens, squash)
from loadout_calc import compute_jump_range, compute_current_jump_range, apply_blueprint_delta

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
# journal slot name regardless of ship type -- unlike optional-internal/
# hardpoint slots, there's no size-to-name ambiguity for these. Used only
# as a fallback when nothing of that group is currently fitted (R1) --
# the fitted-module lookup always runs first, so a ship with an internal
# fuel tank alongside its standard tank still gets a real ambiguity error
# instead of always landing on the standard slot. Names verified against
# the journal manual (elite-journal.readthedocs.io) and real Loadout events.
STANDARD_SLOT_NAMES = {
    "pp": "PowerPlant", "t": "MainEngines", "fsd": "FrameShiftDrive",
    "ls": "LifeSupport", "pd": "PowerDistributor", "s": "Radar", "ft": "FuelTank",
}

# Friendly display text for a standard grp, used only in "this slot only
# takes a <X>" messages (R5). Same 7 groups as STANDARD_SLOT_NAMES.
STANDARD_GRP_DISPLAY = {
    "pp": "power plant", "t": "thrusters", "fsd": "frame shift drive",
    "ls": "life support", "pd": "power distributor", "s": "sensors", "ft": "fuel tank",
}

# Order of ship_stats.slots_json["standard"], verified against every real
# ship file in coriolis-data/ships/*.json.
STANDARD_SLOTS_ORDER = ["pp", "t", "fsd", "ls", "pd", "s", "ft"]

# The 6 standard grps that are "core" modules for slot-kind purposes (S1)
# -- fuel tank (ft) is deliberately excluded: it's the one standard group
# that can ALSO be fitted in an ordinary internal slot, so it gets its own
# "fuel tank" kind rather than "core".
_CORE_GRPS = {"pp", "t", "fsd", "ls", "pd", "s"}

# journal current_loadout.ship_key -> ship_stats.ship_key, for the ships
# where the journal's internal name doesn't match Coriolis's directly.
# Each entry verified by reconstructing that ship's real UnladenMass
# exactly from Coriolis data -- except federation_corvette, verified by
# identity only (it's the only Corvette); Coriolis's mass for it is off
# by 12 t, which this tool doesn't depend on.
JOURNAL_SHIP_TO_CORIOLIS = {
    "belugaliner": "beluga", "cobramkiii": "cobra_mk_iii", "cutter": "imperial_cutter",
    "diamondbackxl": "diamondback_explorer", "empire_eagle": "imperial_eagle",
    "federation_corvette": "federal_corvette", "ferdelance": "fer_de_lance",
    "independant_trader": "keelback", "krait_light": "krait_phantom",
    "lakonminer": "type_11_prospector", "smallcombat01_nx": "kestrel",
    "type6": "type_6_transporter", "type7": "type_7_transport", "type8": "type_8_transport",
    "type9": "type_9_heavy",
}

# NN and M both captured -- S3's internal-slot existence checks need the
# slot NUMBER (NN), not just its advertised size (M).
_OPTIONAL_INTERNAL_SLOT_RE = re.compile(r"^Slot(\d+)_Size(\d+)$", re.IGNORECASE)
# Size word and index N both captured -- S1 splits Tiny (utility kind) from
# Small/Medium/Large/Huge (hardpoint kind); S3's existence check needs N.
_HARDPOINT_SLOT_RE = re.compile(r"^(Tiny|Small|Medium|Large|Huge)(?:Mining)?Hardpoint(\d+)$", re.IGNORECASE)
_HARDPOINT_MAX_CLASS = {"tiny": 0, "small": 1, "medium": 2, "large": 3, "huge": 4}

# On-item name patterns that draw no power at all and are never in
# module_stats (cosmetics/cockpits/armour are ship-cosmetic or
# bulkhead-only, not real modules) -- checked before a module_stats
# lookup so they're never mistaken for an unknown/unmodeled module.
_ZERO_DRAW_SUFFIXES = ("_cockpit",)
_ZERO_DRAW_PREFIXES = ("voicepack_", "paintjob_", "decal_", "nameplate_", "bobble_", "string_lights_")
_ZERO_DRAW_SUBSTRINGS = ("_armour_", "_shipkit", "customisation_")


def _connect():
    """A connection for ONE tool call, already inside a read transaction
    (isolation_level=None + an explicit BEGIN), so every SELECT of the call
    sees one snapshot even while journal_watcher commits. The database is in
    WAL mode, where a reader never blocks the writer. Callers close it in a
    try/finally."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
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


def _resolve_module_groups(conn, typed):
    """The group code(s) a typed module name means, sorted; [] when blank or unknown.
    Looked up, in order, in: the group names built from the module files, the synonym
    table, the friendly names the swap simulator uses, then a raw group code.
    Case, spaces and hyphens do not matter."""
    text = str(typed or "").strip().lower()
    key = squash(text)
    if not key:
        return []
    names = conn.execute("SELECT grp, name FROM module_group_names").fetchall()
    hits = sorted({r["grp"] for r in names if squash(r["name"]) == key})
    if hits:
        return hits
    synonyms = sorted({grp for name, grp in GROUP_SYNONYMS.items() if squash(name) == key})
    if synonyms:
        return synonyms
    if text in MODULE_GROUP_TO_GRP:
        return [MODULE_GROUP_TO_GRP[text]]
    known = {r["grp"] for r in names} | set(MODULE_GROUP_TO_GRP.values())
    # a group with blueprints or effects but no module file (ship armour, 'bh') is still a known code
    known |= {r["grp"] for r in conn.execute("SELECT grp FROM module_blueprints UNION SELECT grp FROM module_specials")}
    if key in known:
        return [key]
    if len(key) > 3 and key.endswith("s"):          # a typed plural: 'beam lasers', 'shield boosters'
        singular = key[:-1]
        hits = sorted({r["grp"] for r in names if squash(r["name"]) == singular})
        return hits or sorted({grp for name, grp in GROUP_SYNONYMS.items() if squash(name) == singular})
    return []


def _module_words(conn, groups, typed_module):
    """The words that belong to a module: what was typed for it, its group names and its synonyms.
    A typed blueprint name may carry these besides its own words ('Low Emissions Power Plant')."""
    words = set(name_tokens(typed_module))
    if groups:
        for r in conn.execute(f"SELECT name FROM module_group_names WHERE grp IN ({','.join('?' * len(groups))})", groups):
            words |= name_tokens(r["name"])
        for name, grp in GROUP_SYNONYMS.items():
            if grp in groups:
                words |= name_tokens(name)
    return words


_MODULE_HINT = ("Core modules she knows: frame shift drive, thrusters, power plant, power distributor, "
                "life support, sensors, shield generator, armour. Weapons and the rest are named one by one, "
                "for example beam laser, burst laser, pulse laser, cannon, multi-cannon, rail gun, missile rack, "
                "fuel scoop, shield booster.")


def _grade_number(grade):
    try:
        return int(grade)
    except (TypeError, ValueError):
        return None


def _in_marks(values):
    """'?,?,?' for an IN (...) list."""
    return ",".join("?" * len(values))


def _split_materials(materials_json):
    """({name: qty}, [extras]) from either storage shape (the sheet's list of
    {qty, item}, Coriolis's {name: qty}). Names lose any stray '(n)' annotation.
    A line without a numeric quantity (the sheet's 'Needs personal research') goes in
    `extras`, never into the counts."""
    raw = json.loads(materials_json)
    items = list(raw.items()) if isinstance(raw, dict) else [(m.get("item"), m.get("qty")) for m in raw if m.get("item")]
    counted, extras = {}, []
    for name, qty in items:
        name = clean_material_name(name)
        if isinstance(qty, int) and not isinstance(qty, bool):
            counted[name] = counted.get(name, 0) + qty
        else:
            extras.append(name)
    return counted, extras


def _materials_text(materials, extras):
    text = ", ".join(f"{qty} {name}" for name, qty in materials.items())
    if extras:
        text += ("; also listed: " if text else "listed: ") + ", ".join(extras)
    return text


def _group_label(conn, grp):
    row = conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC LIMIT 1", (grp,)).fetchone()
    return row["name"].title() if row else grp


def _grade_span(grades):
    grades = sorted(grades)
    if len(grades) > 1 and grades == list(range(grades[0], grades[-1] + 1)):
        return f"{grades[0]}-{grades[-1]}"
    return ", ".join(str(g) for g in grades)


def _coriolis_cost(conn, fdname, grade):
    """Coriolis's recorded cost of one roll: {"upgrade_name", "materials", "extras"}, or None."""
    row = conn.execute("SELECT upgrade_name, materials_json FROM coriolis_blueprints WHERE fdname = ? AND grade = ?", (fdname, grade)).fetchone()
    if row is None:
        return None
    materials, extras = _split_materials(row["materials_json"])
    return {"upgrade_name": row["upgrade_name"], "materials": materials, "extras": extras}


def _sheet_rows(conn, sql, params):
    """Rows from one of the OPTIONAL spreadsheet tables; [] when that table was never
    created (no Engineers.xlsx). Without this, a missing sheet table made every lookup
    answer 'not fully set up' and never reach Coriolis."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise


def _sheet_module_names(conn, table, module_name, groups):
    """The module names (lower case) to look for in a sheet table: the name as typed,
    plus every sheet module name that resolves to one of the same groups."""
    wanted = {module_name.strip().lower()}
    if groups:
        for row in _sheet_rows(conn, f"SELECT DISTINCT module_name FROM {table} WHERE module_name IS NOT NULL", ()):
            if set(_resolve_module_groups(conn, row["module_name"])) & set(groups):
                wanted.add(row["module_name"].strip().lower())
    return sorted(wanted)


def _sheet_blueprint(conn, module_name, groups, upgrade_name, grade):
    names = _sheet_module_names(conn, "graded_blueprints", module_name, groups)
    rows = _sheet_rows(
        conn,
        "SELECT module_name, upgrade, materials_json, modifiers_text FROM graded_blueprints "
        f"WHERE lower(module_name) IN ({_in_marks(names)}) AND grade = ?", (*names, grade))
    candidates = [{"fdname": "", "upgrade_name": r["upgrade"], "row": r} for r in rows]
    hits = _blueprint_name_matches(candidates, upgrade_name, module_words=_module_words(conn, groups, module_name))
    return hits[0]["row"] if len(hits) == 1 else None


def _blueprint_reply(base, source, upgrade, materials, extras, effect):
    label = f"{base['module']}, {upgrade}, grade {base['grade']}"
    reply = {"found": True, "source": source, **base}
    reply.update(upgrade=upgrade, materials=materials, materials_per="roll",
                 cost_known=bool(materials or extras), effect=effect)
    if extras:
        reply["other_requirements"] = extras
    if materials or extras:
        reply["summary"] = f"{label}, per roll: {_materials_text(materials, extras)}."
    else:
        reply["summary"] = f"{label}: no materials are recorded for this upgrade, so its cost is not known."
    return reply


def _unknown_upgrade_reason(conn, module_name, upgrade_name, offered):
    listing = f"Blueprints available for {module_name}: {', '.join(offered)}."
    everything = conn.execute("SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints").fetchall()
    elsewhere = _blueprint_name_matches(everything, upgrade_name)
    if not elsewhere:
        return f"Ellie doesn't know a blueprint called '{upgrade_name}'. {listing}"
    fdnames = sorted({h["fdname"] for h in elsewhere})
    grps = [r["grp"] for r in conn.execute(
        f"SELECT DISTINCT grp FROM module_blueprints WHERE fdname IN ({_in_marks(fdnames)}) ORDER BY grp", fdnames)]
    labels = [_group_label(conn, g) for g in grps]
    shown = ", ".join(labels[:8]) + (f" and {len(labels) - 8} more" if len(labels) > 8 else "")
    return f"'{upgrade_name}' doesn't apply to {module_name}; it applies to {shown}. {listing}"


def _resolve_blueprint(conn, module_name, upgrade_name, groups):
    """(resolved, miss) for a typed module and blueprint name. `resolved` is {"fdname", "upgrade_name",
    "grades"} for the one Coriolis blueprint they mean; `miss` is {"reason", ...} (with "available_upgrades"
    when it helps) when they mean none, or more than one. Exactly one of the two is None."""
    if not groups:
        return None, {"reason": f"Ellie doesn't know a module called '{module_name}'. {_MODULE_HINT}"}
    allowed = [r["fdname"] for r in conn.execute(
        f"SELECT DISTINCT fdname FROM module_blueprints WHERE grp IN ({_in_marks(groups)})", groups)]
    known = conn.execute(
        f"SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints WHERE fdname IN ({_in_marks(allowed)})", allowed).fetchall() if allowed else []
    if not known:
        return None, {"reason": f"{module_name} has no engineering blueprints in Ellie's data."}
    offered = sorted({r["upgrade_name"] for r in known})
    hits = _blueprint_name_matches(known, upgrade_name, module_words=_module_words(conn, groups, module_name))
    if len(hits) > 1:
        names = sorted({h["upgrade_name"] for h in hits})
        return None, {"available_upgrades": offered,
                      "reason": f"'{upgrade_name}' could mean more than one blueprint for {module_name}: {', '.join(names)}. Ask which one."}
    if not hits:
        return None, {"available_upgrades": offered, "reason": _unknown_upgrade_reason(conn, module_name, upgrade_name, offered)}
    fdname = hits[0]["fdname"]
    grades = sorted(r["grade"] for r in conn.execute("SELECT grade FROM coriolis_blueprints WHERE fdname = ?", (fdname,)))
    return {"fdname": fdname, "upgrade_name": hits[0]["upgrade_name"], "grades": grades}, None


@_needs_setup
def get_blueprint(module_name: str, upgrade_name: str, grade: int) -> dict:
    """Materials ONE roll of a blueprint costs, for any module Coriolis knows. The
    spreadsheet's row is used first when it has this module, upgrade and grade (the
    owner's hand-kept data); otherwise Coriolis's. A module name resolves to its group(s);
    the group's allow-list (module_blueprints) says which blueprints it can take; the
    blueprint is matched by name. A miss says why and lists what does exist."""
    module_name = str(module_name or "").strip()
    upgrade_name = str(upgrade_name or "").strip()
    number = _grade_number(grade)
    base = {"module": module_name, "upgrade": upgrade_name, "grade": number if number is not None else grade}
    if number is None:
        return {"found": False, **base, "reason": "The grade must be a number from 1 to 5."}
    grade = number
    conn = _connect()
    try:
        groups = _resolve_module_groups(conn, module_name)
        sheet = _sheet_blueprint(conn, module_name, groups, upgrade_name, grade)
        if sheet is not None:
            materials, extras = _split_materials(sheet["materials_json"])
            return _blueprint_reply(base, "excel", sheet["upgrade"], materials, extras, sheet["modifiers_text"])
        resolved, miss = _resolve_blueprint(conn, module_name, upgrade_name, groups)
        if miss:
            return {"found": False, **base, **miss}
        grades = resolved["grades"]
        if grade not in grades:
            plural = "s" if len(grades) > 1 else ""
            return {"found": False, **base, "available_grades": grades,
                    "reason": f"{resolved['upgrade_name']} for {module_name} is offered at grade{plural} {_grade_span(grades)}, not grade {grade}."}
        cost = _coriolis_cost(conn, resolved["fdname"], grade)
        return _blueprint_reply(base, "coriolis", cost["upgrade_name"], cost["materials"], cost["extras"], None)
    finally:
        conn.close()


def _special_cost(conn, edname):
    row = conn.execute(
        "SELECT name, description, materials_json FROM coriolis_experimental_effects WHERE lower(edname) = lower(?)", (edname,)).fetchone()
    if row is None:
        return None
    materials, extras = _split_materials(row["materials_json"])
    return {"name": row["name"], "description": row["description"], "materials": materials, "extras": extras}


def _sheet_effect(conn, module_name, groups, effect_name):
    names = _sheet_module_names(conn, "ungraded_costs", module_name, groups)
    rows = _sheet_rows(
        conn,
        "SELECT module_name, effect, materials_json, modifiers_text FROM ungraded_costs "
        f"WHERE lower(module_name) IN ({_in_marks(names)})", tuple(names))
    candidates = [{"fdname": "", "upgrade_name": r["effect"], "row": r} for r in rows]
    hits = _blueprint_name_matches(candidates, effect_name, module_words=_module_words(conn, groups, module_name))
    return hits[0]["row"] if len(hits) == 1 else None


def _effect_reply(base, source, effect_name, materials, extras, description):
    label = f"{base['module']}, {effect_name}"
    reply = {"found": True, "source": source, **base}
    reply.update(effect_name=effect_name, materials=materials, materials_per="application",
                 cost_known=bool(materials or extras), description=description)
    if extras:
        reply["other_requirements"] = extras
    if materials or extras:
        reply["summary"] = f"{label}, each time it is applied: {_materials_text(materials, extras)}."
    else:
        reply["summary"] = f"{label}: no materials are recorded for this effect, so its cost is not known."
    return reply


def _unknown_effect_reason(conn, module_name, effect_name, offered):
    listing = f"Experimental effects available for {module_name}: {', '.join(offered)}."
    everything = [{"fdname": r["edname"], "upgrade_name": r["name"]}
                  for r in conn.execute("SELECT edname, name FROM coriolis_experimental_effects")]
    elsewhere = _blueprint_name_matches(everything, effect_name)
    if not elsewhere:
        return f"Ellie doesn't know an experimental effect called '{effect_name}'. {listing}"
    eds = sorted({h["fdname"].lower() for h in elsewhere})
    grps = [r["grp"] for r in conn.execute(
        f"SELECT DISTINCT grp FROM module_specials WHERE lower(edname) IN ({_in_marks(eds)}) ORDER BY grp", eds)]
    labels = [_group_label(conn, g) for g in grps]
    shown = ", ".join(labels[:8]) + (f" and {len(labels) - 8} more" if len(labels) > 8 else "")
    return f"'{effect_name}' can't be applied to {module_name}; it applies to {shown}. {listing}"


@_needs_setup
def get_experimental_effect(module_name: str, effect_name: str) -> dict:
    """Materials an experimental effect costs each time it is applied (ungraded -- a
    single cost). The spreadsheet's row first, then Coriolis. The effect must be one the
    module's group can take (module_specials): 'Mass Manager' on thrusters is refused,
    and the reply says where it does fit."""
    module_name = str(module_name or "").strip()
    effect_name = str(effect_name or "").strip()
    base = {"module": module_name, "effect_name": effect_name}
    conn = _connect()
    try:
        groups = _resolve_module_groups(conn, module_name)
        sheet = _sheet_effect(conn, module_name, groups, effect_name)
        if sheet is not None:
            materials, extras = _split_materials(sheet["materials_json"])
            return _effect_reply(base, "excel", sheet["effect"], materials, extras, sheet["modifiers_text"])
        if not groups:
            return {"found": False, **base, "reason": f"Ellie doesn't know a module called '{module_name}'. {_MODULE_HINT}"}
        allowed = [r["edname"].lower() for r in conn.execute(
            f"SELECT DISTINCT edname FROM module_specials WHERE grp IN ({_in_marks(groups)})", groups)]
        rows = conn.execute(
            "SELECT edname, name FROM coriolis_experimental_effects "
            f"WHERE lower(edname) IN ({_in_marks(allowed)})", allowed).fetchall() if allowed else []
        if not rows:
            return {"found": False, **base, "reason": f"{module_name} has no experimental effects in Ellie's data."}
        offered = sorted({r["name"] for r in rows})
        hits = _blueprint_name_matches([{"fdname": r["edname"], "upgrade_name": r["name"]} for r in rows], effect_name,
                                       module_words=_module_words(conn, groups, module_name), use_tail=False)
        if len(hits) > 1:
            costs = [_special_cost(conn, h["fdname"])["materials"] for h in hits]
            if all(c == costs[0] for c in costs):      # Coriolis lists some effects twice (Mass Lock Munition on torpedo pylons)
                hits = hits[:1]
        if len(hits) > 1:
            names = sorted({h["upgrade_name"] for h in hits})
            return {"found": False, **base, "available_effects": offered,
                    "reason": f"'{effect_name}' could mean more than one effect for {module_name}: {', '.join(names)}. Ask which one."}
        if not hits:
            return {"found": False, **base, "available_effects": offered,
                    "reason": _unknown_effect_reason(conn, module_name, effect_name, offered)}
        cost = _special_cost(conn, hits[0]["fdname"])
        return _effect_reply(base, "coriolis", cost["name"], cost["materials"], cost["extras"], cost["description"])
    finally:
        conn.close()


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
    """Diffs ONE roll of a blueprint's requirement against what's actually on hand
    right now (materials_inventory, kept current by journal_watcher.py)."""
    blueprint = get_blueprint(module_name, upgrade_name, grade)
    if not blueprint["found"]:
        result = {"found": False, "module": blueprint.get("module", module_name),
                  "upgrade": blueprint.get("upgrade", upgrade_name), "grade": blueprint.get("grade", grade)}
        for key in ("reason", "available_upgrades", "available_grades"):
            if key in blueprint:
                result[key] = blueprint[key]
        return result

    required = blueprint["materials"]
    shared = {"found": True, "module": blueprint["module"], "upgrade": blueprint["upgrade"], "grade": blueprint["grade"],
              "cost_known": blueprint["cost_known"], "required": required, "source": blueprint["source"], "materials_per": "roll"}
    if "other_requirements" in blueprint:
        shared["other_requirements"] = blueprint["other_requirements"]
    if not required:
        return {**shared, "have_everything": None, "missing": {}, "summary": blueprint["summary"]}

    conn = _connect()
    missing, have_enough = {}, True
    try:
        for material, needed_qty in required.items():
            row = _held_count(conn, material)
            on_hand = row["count"] if row else 0
            if on_hand < needed_qty:
                missing[material] = {"need": needed_qty, "have": on_hand, "short_by": needed_qty - on_hand}
                have_enough = False
    finally:
        conn.close()

    label = f"{blueprint['module']}, {blueprint['upgrade']}, grade {blueprint['grade']}"
    if have_enough:
        summary = f"You have everything for {label} (per roll): " + ", ".join(f"{qty} {name}" for name, qty in required.items()) + "."
    else:
        summary = (f"For {label} (per roll) you need " + ", ".join(f"{qty} {name}" for name, qty in required.items())
                   + ". You are short by: "
                   + ", ".join(f"{m['short_by']} {name} (have {m['have']})" for name, m in missing.items()) + ".")
    if shared.get("other_requirements"):
        summary += f" Also listed: {', '.join(shared['other_requirements'])}."
    return {**shared, "have_everything": have_enough, "missing": missing, "summary": summary}


@_needs_setup
def get_engineer(name: str) -> dict:
    """An engineer's location, prerequisites, and what they unlock."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM engineers WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if not row:
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
    finally:
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
    try:
        status_row = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
        materials = conn.execute(
            "SELECT category, COUNT(*) as distinct_materials, SUM(count) as total_units "
            "FROM materials_inventory WHERE count > 0 GROUP BY category"
        ).fetchall()
    finally:
        conn.close()
    status = dict(status_row) if status_row else {}
    status.pop("id", None)

    status["combat_rank_name"] = _rank_name(COMBAT_RANKS, status.get("combat_rank"))
    status["trade_rank_name"] = _rank_name(TRADE_RANKS, status.get("trade_rank"))
    status["explore_rank_name"] = _rank_name(EXPLORE_RANKS, status.get("explore_rank"))
    status["cqc_rank_name"] = _rank_name(CQC_RANKS, status.get("cqc_rank"))
    status["federation_rank_name"] = _rank_name(FEDERATION_RANKS, status.get("federation_rank"))
    status["empire_rank_name"] = _rank_name(EMPIRE_RANKS, status.get("empire_rank"))

    materials_summary = {
        m["category"] or "Unknown": {"distinct_materials": m["distinct_materials"], "total_units": m["total_units"]}
        for m in materials
    }

    return {"status": status, "materials_summary": materials_summary}


def _held_count(conn, name):
    """The materials_inventory row for a material, found by the key the journal stored
    ('disruptedwakeechoes') OR by the name the game shows ('Atypical Disrupted Wake
    Echoes'); the stored key wins if both match. None when the commander has never held it."""
    key = _material_key(name)
    if not key:
        return None
    return conn.execute(
        "SELECT material_name, display_name, category, count FROM materials_inventory "
        "WHERE material_name = ? OR replace(replace(lower(display_name), ' ', ''), '-', '') = ? "
        "ORDER BY (material_name = ?) DESC LIMIT 1",
        (key, key, key),
    ).fetchone()


def _commander_engineers(conn):
    """{engineer_key: {"name", "stage", "rank"}} from the journal-fed engineer_progress table ({} when empty)."""
    return {engineer_key(r["engineer_name"]): {"name": r["engineer_name"], "stage": r["rank_stage"], "rank": r["rank_value"]}
            for r in conn.execute("SELECT engineer_name, rank_stage, rank_value FROM engineer_progress")}


def _engineers_offering(conn, fdname, grade):
    """The engineers Coriolis lists for this blueprint at this grade, each with the commander's status and
    rank from the journal. One entry per engineer (Coriolis's misspellings merged), sorted by name.
    `usable` = unlocked with a rank of at least this grade."""
    mine = _commander_engineers(conn)
    seen, offers = set(), []
    for row in conn.execute("SELECT DISTINCT engineer FROM blueprint_engineers WHERE fdname = ? AND grade = ?", (fdname, grade)):
        key = engineer_key(row["engineer"])
        if key in seen:
            continue
        seen.add(key)
        known = mine.get(key)
        status = (known["stage"] or "").lower() if known else "not met"
        rank = known["rank"] if known else None
        name = known["name"] if known else ENGINEER_ALIASES.get(squash(row["engineer"]), row["engineer"])
        offers.append({"name": name, "status": status, "rank": rank, "usable": status == "unlocked" and (rank or 0) >= grade})
    return sorted(offers, key=lambda o: o["name"])


def _choose_engineer(offers, target):
    """(engineer, mode): the usable engineer with the highest rank (ties by name) -> 'usable'; else the unlocked
    one with the highest known rank, who cannot reach the target -> 'low_rank'; else an unlocked one whose rank
    the journal has not given -> 'rank_unknown'; else None with 'none_unlocked' (offers exist, nobody is
    unlocked) or 'no_engineer' (nobody offers it)."""
    usable = sorted((o for o in offers if o["usable"]), key=lambda o: (-o["rank"], o["name"]))
    if usable:
        return usable[0], "usable"
    unlocked = [o for o in offers if o["status"] == "unlocked"]
    ranked = sorted((o for o in unlocked if o["rank"] is not None), key=lambda o: (-o["rank"], o["name"]))
    if ranked:
        return ranked[0], "low_rank"
    if unlocked:
        return sorted(unlocked, key=lambda o: o["name"])[0], "rank_unknown"
    return None, ("none_unlocked" if offers else "no_engineer")


def _start_grade(conn, groups, fdname, stated):
    """(grade, source, note): the grade an upgrade starts from. A stated grade wins. Otherwise: exactly one
    fitted module of the group that carries this blueprint gives its level; every other case starts from
    an unengineered module (grade 0) and the note says why and what to say instead."""
    if stated is not None:
        return stated, "stated", None
    fitted = []
    if groups:
        try:
            fitted = conn.execute(
                "SELECT m.slot, m.engineering_blueprint, m.engineering_level FROM current_loadout_modules m "
                f"JOIN module_stats s ON s.symbol = lower(m.item) WHERE s.grp IN ({_in_marks(groups)}) ORDER BY m.slot", groups).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
    if not fitted:
        return 0, "from scratch", ("Nothing of this kind is fitted on your current ship, or no loadout has been captured yet, "
                                   "so this starts from an unengineered module. Say 'from grade N' if yours is already engineered.")
    if len(fitted) > 1:
        slots = ", ".join(r["slot"] for r in fitted)
        return 0, "from scratch", (f"You have {len(fitted)} of these fitted ({slots}), so this starts from an unengineered module. "
                                   "Say 'from grade N' for the one you mean.")
    row = fitted[0]
    if row["engineering_blueprint"] and row["engineering_blueprint"].lower() == fdname.lower():
        return int(row["engineering_level"] or 0), "fitted module", None
    if row["engineering_blueprint"]:
        return 0, "from scratch", "Your fitted one carries a different modification; a new one replaces it, so this starts from grade 1."
    return 0, "fitted module", "Your fitted one is unengineered, so this starts from grade 1."


@_needs_setup
def get_material_count(material_name: str) -> dict:
    """Exact on-hand count for ONE specific material, by exact name. Use
    this for "how much X do I have" instead of trying to read it off
    player_state()'s summary."""
    conn = _connect()
    try:
        row = _held_count(conn, material_name)
    finally:
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
    try:
        current = conn.execute("SELECT * FROM commander_status WHERE id = 1").fetchone()
        if not current or not current["current_system"]:
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
    finally:
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


def _round2(value):
    return None if value is None else round(value, 2)


def _fitted_fsd_params(conn):
    """The fitted frame shift drive's jump-range inputs -- {"item", "optmass",
    "base_optmass", "maxfuel", "fuelmul", "fuelpower", "booster_bonus"} -- or
    None when the FSD isn't in module_stats at all. optmass prefers the
    journal's own engineered FSDOptimalMass over the base value (ground truth
    for a module the commander really has fitted); base_optmass is the base
    value alone. booster_bonus is the flat ly bonus of a fitted Guardian FSD
    Booster, 0.0 when there is none.

    Any individual stat can still be None (a module_stats row missing a
    field): callers must treat that as "unknown jump range", never as 0."""
    fsd_row = conn.execute(
        "SELECT clm.item, clm.engineering_modifiers_json, ms.* FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE clm.slot = ?",
        (STANDARD_SLOT_NAMES["fsd"],),
    ).fetchone()
    if fsd_row is None:
        return None
    # N6: a missing base optmass must stay None (-> an honest "unknown jump
    # range"), never get routed through the NULL->0.0 rule that's correct for
    # mass/power but wrong here -- a ~0 optmass would give a jump range of
    # ~0, not "unknown".
    engineered_optmass = _engineered_modifier(fsd_row["engineering_modifiers_json"], "FSDOptimalMass")
    base_optmass = new_module_extra_get(fsd_row, "optmass")
    optmass = engineered_optmass if engineered_optmass is not None else base_optmass
    booster_row = conn.execute(
        "SELECT ms.extra_json FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE ms.grp = 'gfsb'"
    ).fetchone()
    return {
        "item": fsd_row["item"],
        "optmass": optmass,
        "base_optmass": base_optmass,  # the un-engineered module_stats value; the jump check needs it
        "maxfuel": new_module_extra_get(fsd_row, "maxfuel"),
        "fuelmul": new_module_extra_get(fsd_row, "fuelmul"),
        "fuelpower": new_module_extra_get(fsd_row, "fuelpower"),
        "booster_bonus": (new_module_extra_get(booster_row, "jumpboost") if booster_row else None) or 0.0,
    }


def _fsd_params_complete(params):
    return params is not None and None not in (
        params["optmass"], params["maxfuel"], params["fuelmul"], params["fuelpower"],
    )


def _read_ship_state(conn):
    """The ship_state row (fuel/cargo/last-jump snapshot), or None if the
    table doesn't exist yet -- a database from before fuel tracking, where
    journal_watcher hasn't been restarted since the upgrade. That reads as
    "nothing tracked yet", not as the whole database being unset-up."""
    try:
        return conn.execute("SELECT * FROM ship_state WHERE id = 1").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return None


def _current_ship_state(conn, params):
    """(fuel_now, cargo_now, usable, state_note) for a current-load jump range.
    When the state is not usable, fuel_now and cargo_now are None too, so
    nothing downstream can relay a number we don't trust (e.g. a stale fuel
    level bigger than the tank), and state_note says why in words the
    commander can be told."""
    state = _read_ship_state(conn)
    fuel = state["fuel_main"] if state else None
    cargo = state["cargo_count"] if state else None
    loadout_row = conn.execute("SELECT fuel_main, unladen_mass FROM current_loadout WHERE id = 1").fetchone()
    capacity = loadout_row["fuel_main"] if loadout_row else None
    unladen = loadout_row["unladen_mass"] if loadout_row else None

    if fuel is None:
        note = "current fuel isn't known yet (no refuel, jump or load event seen since the ship changed)"
    elif cargo is None:
        note = "current cargo isn't known yet (the game hasn't reported the cargo hold since the ship changed)"
    elif fuel < 0 or (capacity is not None and fuel > capacity + 1e-6):
        # Fuel outside [0, tank capacity] means the tracked level came from a
        # ship we've since left without seeing the change.
        note = "the tracked fuel level doesn't fit this ship's tank, so it's probably stale"
    elif unladen is None:
        # every mass figure is unladen + something; checked before any arithmetic
        note = "the ship's empty mass isn't known yet"
    elif not _fsd_params_complete(params):
        note = "the fitted frame shift drive isn't in Ellie's module data"
    else:
        return fuel, cargo, True, None
    return None, None, False, note


def _jump_check_warning(conn, params):
    """Honesty guard for the mass model behind the current-load range. The
    game burns FuelUsed = fuelmul * (JumpDist * M / optmass) ** fuelpower, so
    the last real jump tells us the ship's total mass M; if that disagrees
    with unladen + fuel before the jump + cargo by more than 1 t, Ellie's
    picture of the ship was wrong for that jump (normal jumps agree to about
    0.01 t, 97% within 0.5 t) and the commander is told so. Returns the
    warning sentence, or None -- also None, silently, when the last jump is
    missing a number the check needs, was boosted (injection changes the
    jump), was made with a different drive than the one fitted now, or a
    Guardian FSD Booster is fitted (see the comment below).

    The optimal mass used is the one in force AT the jump (the snapshot's
    last_jump_fsd_optmass if the drive was engineered then, else the base
    value), not the drive's current one: engineering a drive changes its
    optimal mass without changing its item symbol, and judging an old jump
    by the new mass would blame Ellie's model for a drive that never made it."""
    state = _read_ship_state(conn)
    if state is None or not _fsd_params_complete(params):
        return None
    if params["booster_bonus"] > 0:
        # The implied mass below has no booster term while the range model adds
        # the booster's jumpboost; how a fitted booster changes JumpDist is not
        # verified (no booster jumps in the real history), so the check stays
        # out of it, the same honest skip a boosted jump gets.
        return None
    if "last_jump_fsd_optmass" not in state.keys():
        # the watcher hasn't migrated the table since the upgrade: a snapshot
        # without its jump-time optimal mass can't be judged
        return None
    jump_optmass = state["last_jump_fsd_optmass"]
    if jump_optmass is None:
        jump_optmass = params["base_optmass"]
    if jump_optmass is None:
        return None
    dist, used = state["last_jump_dist"], state["last_jump_fuel_used"]
    if dist is None or used is None or dist <= 0 or used <= 0:
        return None
    if state["last_jump_boosted"] != 0:
        return None
    if None in (state["last_jump_cargo"], state["last_jump_unladen"], state["last_jump_fuel_before"]):
        return None
    if state["last_jump_fsd_item"] != params["item"]:
        return None
    implied = jump_optmass * (used / params["fuelmul"]) ** (1 / params["fuelpower"]) / dist
    modeled = state["last_jump_unladen"] + state["last_jump_fuel_before"] + state["last_jump_cargo"]
    if abs(implied - modeled) <= 1.0:
        return None
    return f"Ellie's mass estimate for your last jump was off by {abs(implied - modeled):.1f} t, so these jump ranges may be inaccurate."


def _module_display_name(module_row):
    """The human name of a module_stats row: Coriolis's own `name` (e.g.
    "Frame Shift Drive (SCO)"), else its `ukName` ("FSD", "Cargo Rack"). A few
    families (the weapons) carry neither, so those are named from their group
    and mount ("Pulse Laser (fixed)"); a group Ellie has no name for falls back
    to the raw symbol."""
    extra = json.loads(module_row["extra_json"] or "{}")
    name = extra.get("name") or extra.get("ukName")
    if name:
        return name
    group = next((g for g, code in MODULE_GROUP_TO_GRP.items() if code == module_row["grp"]), None)
    if group is None:
        return module_row["symbol"]
    mount = _MOUNT_WORDS.get(extra.get("mount"))
    return f"{group.title()} ({mount})" if mount else group.title()


def _module_fields(item, stats_row):
    """{"module", "class", "rating"} for a fitted symbol. A symbol that isn't
    in module_stats is listed by its raw name, with no class or rating."""
    if stats_row is None:
        return {"module": item}
    fields = {"module": _module_display_name(stats_row)}
    if stats_row["class"] is not None:
        fields["class"] = stats_row["class"]
    if stats_row["rating"] is not None:
        fields["rating"] = stats_row["rating"]
    return fields


def _lookup_display_name(conn, sql, key):
    """A display name from one of the Coriolis tables, or None -- also when the
    table isn't there (a database set up without that step)."""
    try:
        row = conn.execute(sql, (key,)).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return None
    return row[0] if row and row[0] else None


def _engineering_label(conn, blueprint, level, experimental):
    """'Increased range g5, Mass Manager': the blueprint's display name and grade,
    plus the experimental effect's name when one is fitted. Each name falls back
    to the journal's own spelling when Coriolis doesn't know it."""
    name = _lookup_display_name(
        conn, "SELECT upgrade_name FROM coriolis_blueprints WHERE fdname = ? LIMIT 1", blueprint) or blueprint
    label = f"{name} g{level}" if level is not None else name
    if experimental:
        label += ", " + (_lookup_display_name(
            conn, "SELECT name FROM coriolis_experimental_effects WHERE edname = ? LIMIT 1", experimental) or experimental)
    return label


def _readable_modules(conn):
    """The fitted modules as a compact, readable list: {"slot", "module",
    "class", "rating", "on", "health"} plus "engineering" (one string) when
    engineered. Cosmetic and zero-draw slots (armour, cockpit, paint, decals,
    ship kits, voice pack, nameplates, bobbleheads, lights) are not modules and
    are left out. The raw symbol and the numeric modifiers are not repeated:
    a small model reading them misdescribed modules and the list ran to ~5,000
    characters on a real ship."""
    entries = []
    rows = conn.execute(
        "SELECT slot, item, is_on, health, engineering_blueprint, engineering_level, "
        "engineering_experimental FROM current_loadout_modules ORDER BY slot"
    ).fetchall()
    for m in rows:
        if _draws_nothing(m["item"].lower()):
            continue
        stats = conn.execute("SELECT * FROM module_stats WHERE symbol = ?", (m["item"].lower(),)).fetchone()
        entry = {"slot": m["slot"], **_module_fields(m["item"], stats), "on": bool(m["is_on"]), "health": m["health"]}
        if m["engineering_blueprint"]:
            entry["engineering"] = _engineering_label(
                conn, m["engineering_blueprint"], m["engineering_level"], m["engineering_experimental"])
        entries.append(entry)
    return entries


@_needs_setup
def get_current_loadout() -> dict:
    """The commander's real, current ship as captured from the journal's
    own Loadout event -- ship, mass, jump range, and every fitted real module
    (named, with its class and rating, and its engineering as one short
    string; cosmetic slots are left out).

    Two jump ranges, never to be confused: jump_range_max is the game's
    own MaxJumpRange (empty hold, one jump of fuel); jump_range_now is
    computed here from the fuel and cargo journal_watcher has tracked, and
    is None (with state_note saying why) whenever those can't be trusted.
    summary is the same facts pre-written as one sentence, so a small model
    can relay it instead of re-deriving the numbers."""
    conn = _connect()
    try:
        status = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
        if not status or not status["ship_key"]:
            return {"found": False, "reason": "no Loadout event captured yet -- journal_watcher may not be running, or you haven't launched/undocked this session"}

        module_list = _readable_modules(conn)
        params = _fitted_fsd_params(conn)
        fuel_now, cargo_now, usable, state_note = _current_ship_state(conn, params)
        # the warning qualifies the current-load range, so it goes with it
        warning = _jump_check_warning(conn, params) if usable else None
        ship_row = _resolve_ship_stats(conn, status["ship_key"])
    finally:
        conn.close()

    ship_name = ship_row["name"] if ship_row else status["ship_key"]
    unladen = status["unladen_mass"]
    jump_range_max = _round2(status["max_jump_range"])
    jump_range_now = mass_now = None
    if usable:
        jump_range_now = round(compute_current_jump_range(
            unladen, fuel_now, cargo_now, params["optmass"], params["maxfuel"],
            params["fuelmul"], params["fuelpower"], params["booster_bonus"],
        ), 2)
        mass_now = round(unladen + fuel_now + cargo_now, 2)
    # computed from the raw tracked values above; only what's reported is rounded
    fuel_now, cargo_now = _round2(fuel_now), _round2(cargo_now)

    # a missing journal MaxJumpRange leaves the max clause out rather than print "None"
    if usable:
        summary = f"{ship_name}: jump range now {jump_range_now} ly (fuel {fuel_now} t, cargo {cargo_now} t, mass {mass_now} t)"
        summary += f"; max {jump_range_max} ly with an empty hold and one jump of fuel." if jump_range_max is not None else "."
    else:
        summary = f"{ship_name}: "
        if jump_range_max is not None:
            summary += f"max jump range {jump_range_max} ly (empty hold, one jump of fuel). "
        summary += f"Current-load jump range unavailable: {state_note}."
    if warning:
        summary += " " + warning

    result = {
        "found": True,
        "ship": status["ship_key"],
        "ship_name": ship_name,
        "jump_range_now": jump_range_now,
        "jump_range_max": jump_range_max,
        "fuel_now": fuel_now,
        "cargo_now": cargo_now,
        "mass_now": mass_now,
        "unladen_mass": _round2(unladen),
        "cargo_capacity": status["cargo_capacity"],
        "fuel_tank_capacity": status["fuel_main"],
        "fuel_reservoir_capacity": status["fuel_reserve"],
        "state_note": state_note,
    }
    if warning:
        result["warning"] = warning
    result["summary"] = summary
    result["modules"] = module_list
    return result


def _normalised_name(text):
    """Lower-case letters and digits only: 'Type-9 Heavy' -> 'type9heavy'."""
    return re.sub(r"[\W_]+", "", text.lower())


def _match_ship(rows, typed):
    """The one ship_stats row `typed` means, or None. In order: the exact name
    (case-insensitive); the same name ignoring everything but letters and
    digits; a UNIQUE prefix of that normalised form ('Type-9' -> 'Type-9
    Heavy'). More than one prefix match is not a match: Ellie would be guessing."""
    wanted = typed.strip().lower()
    for row in rows:
        if (row["name"] or "").lower() == wanted:
            return row
    key = _normalised_name(typed)
    if not key:
        return None
    for row in rows:
        if _normalised_name(row["name"] or "") == key:
            return row
    prefixed = [row for row in rows if _normalised_name(row["name"] or "").startswith(key)]
    return prefixed[0] if len(prefixed) == 1 else None


_CORE_SLOT_LABELS = ("power_plant", "thrusters", "frame_shift_drive", "life_support",
                     "power_distributor", "sensors", "fuel_tank")


@_needs_setup
def get_ship_info(ship_name: str) -> dict:
    """A named ship's hull mass and slot layout from coriolis-data (via
    ingest_ship_modules.py), as LABELLED facts -- core slot sizes by name,
    hardpoints by size, utility mounts, internal slots -- plus a pre-written
    summary. The raw positional arrays of ship_stats (['standard', 'hardpoints',
    'internal'] as bare numbers) were being misread by small models."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM ship_stats").fetchall()
    finally:
        conn.close()
    row = _match_ship(rows, ship_name)
    if row is None:
        result = {"found": False, "reason": f"Ellie has no ship called '{ship_name}' in her ship data."}
        by_normalised = {}
        for r in rows:
            by_normalised.setdefault(_normalised_name(r["name"] or ""), r["name"])
        close = difflib.get_close_matches(_normalised_name(ship_name), list(by_normalised), n=3)
        if close:
            result["did_you_mean"] = [by_normalised[c] for c in close]
        return result

    slots = json.loads(row["slots_json"])
    standard = slots.get("standard") or []
    hardpoint_classes = [_entry_class(e) for e in slots.get("hardpoints") or []]
    internal = slots.get("internal") or []
    plain_sizes = [e for e in internal if isinstance(e, int)]
    hardpoints = {word: hardpoint_classes.count(c) for word, c in
                  (("small", 1), ("medium", 2), ("large", 3), ("huge", 4))}
    utility_mounts = hardpoint_classes.count(0)
    core = dict(zip(_CORE_SLOT_LABELS, standard)) if len(standard) == len(_CORE_SLOT_LABELS) else {}
    summary = (
        f"{row['name']} ({row['manufacturer']}): hull mass {row['hull_mass']} t; "
        f"hardpoints: {hardpoints['small']} small, {hardpoints['medium']} medium, "
        f"{hardpoints['large']} large, {hardpoints['huge']} huge; "
        f"{utility_mounts} utility mounts; {len(internal)} internal slots"
    )
    if core:
        summary += (
            f"; core slots: power plant {core['power_plant']}, thrusters {core['thrusters']}, "
            f"frame shift drive {core['frame_shift_drive']}, life support {core['life_support']}, "
            f"power distributor {core['power_distributor']}, sensors {core['sensors']}, fuel tank {core['fuel_tank']}"
        )
    return {
        "found": True,
        "name": row["name"],
        "manufacturer": row["manufacturer"],
        "hull_mass": row["hull_mass"],
        "core_slots": core,
        "hardpoints": hardpoints,
        "utility_mounts": utility_mounts,
        "internal_slots": {
            "total": len(internal),
            "sizes": {str(c): plain_sizes.count(c) for c in sorted(set(plain_sizes), reverse=True)},
            "reserved": [{"name": e.get("name"), "class": e.get("class")} for e in internal if isinstance(e, dict)],
        },
        "summary": summary + ".",
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


def _resolve_explicit_slot_casing(conn, explicit_slot):
    """An explicit `slot` is matched case-insensitively (R5) -- against
    what's actually fitted first (the ground truth for real slot names),
    then against the 7 standard names, returning the REAL casing either
    way, or None when it matches neither."""
    row = conn.execute(
        "SELECT slot FROM current_loadout_modules WHERE lower(slot) = lower(?)", (explicit_slot,)
    ).fetchone()
    if row:
        return row["slot"]
    for name in STANDARD_SLOT_NAMES.values():
        if name.lower() == explicit_slot.lower():
            return name
    return None


# Any spelling of a hardpoint/utility slot name, once spaces and underscores
# are removed: size word, optional "mining", "hardpoint", a zero-padded index.
_HARDPOINT_NAME_RE = re.compile(r"(tiny|small|medium|large|huge)(mining)?hardpoint0*(\d+)", re.IGNORECASE)


def _parse_hardpoint_name(text):
    """(size_word, is_mining, index) for 'TinyHardpoint1', 'Tiny Hardpoint 1',
    'tinyhardpoint01', 'SMALLMININGHARDPOINT2' ...; None for anything else
    (trailing or leading text included: this is a fullmatch)."""
    m = _HARDPOINT_NAME_RE.fullmatch(re.sub(r"[ _]", "", text.strip()))
    if not m:
        return None
    return m.group(1).lower(), bool(m.group(2)), int(m.group(3))


def _resolve_explicit_slot(conn, explicit_slot):
    """(slot, error) for an explicit `slot` argument: stripped; matched
    case-insensitively against what's fitted and the 7 standard names (real
    casing returned); failing that, a hardpoint/utility spelling such as
    'Tiny Hardpoint 1' or 'tinyhardpoint01' is matched to the FITTED slot with
    the same size, mining flag and index (otherwise the module actually
    fitted there would be treated as an empty slot and never subtracted), or
    canonicalised to 'TinyHardpoint1' style when nothing is fitted there.
    Hardpoint numbering starts at 1. Anything else is returned as given -- a
    genuinely empty slot nothing has ever been fitted to has nothing to be
    corrected against."""
    slot = explicit_slot.strip()
    real = _resolve_explicit_slot_casing(conn, slot)
    if real:
        return real, None
    parsed = _parse_hardpoint_name(slot)
    if parsed is None:
        return slot, None
    size_word, mining, index = parsed
    if index == 0:
        return None, f"'{slot}' isn't a valid hardpoint slot -- numbering starts at 1"
    for row in conn.execute("SELECT slot FROM current_loadout_modules").fetchall():
        if _parse_hardpoint_name(row["slot"]) == parsed:
            return row["slot"], None
    return f"{size_word.capitalize()}{'Mining' if mining else ''}Hardpoint{index}", None


def _which_slot_reason(conn, ship_key, module_kind, module_class):
    """The reply for "a utility module / weapon / internal module, and no slot
    named": which real slot names to choose from, taken from the ship's layout
    and what is fitted, instead of an instruction with no names in it. None when
    it can't say (unknown ship or module kind)."""
    ship_row = _resolve_ship_stats(conn, ship_key)
    if ship_row is None or module_kind not in ("utility", "weapon", "internal"):
        return None
    ship_name = ship_row["name"] or ship_key
    fitted = conn.execute("SELECT slot, item FROM current_loadout_modules ORDER BY slot").fetchall()

    if module_kind == "internal":
        shown = []
        for r in fitted:
            if _OPTIONAL_INTERNAL_SLOT_RE.fullmatch(r["slot"]) and len(shown) < 6:
                stats = conn.execute("SELECT * FROM module_stats WHERE symbol = ?", (r["item"].lower(),)).fetchone()
                shown.append(f"{r['slot']} ({_module_fields(r['item'], stats)['module']})")
        example = f", for example one of your fitted internal slots: {', '.join(shown)}" if shown else ""
        return f"Which internal slot? Name one with slot{example}. Ellie cannot see empty internal slots in the journal."

    classes = [_entry_class(e) for e in json.loads(ship_row["slots_json"]).get("hardpoints") or []]
    fitted_hardpoints = {}
    for r in fitted:
        parsed = _parse_hardpoint_name(r["slot"])
        if parsed:
            fitted_hardpoints[parsed] = r["slot"]
    if module_kind == "utility":
        words, noun = ("tiny",), "utility mounts"
    else:
        words = tuple(w for w in ("small", "medium", "large", "huge") if _HARDPOINT_MAX_CLASS[w] >= module_class)
        noun = f"hardpoints that fit a class {module_class} weapon"
    free, in_use = [], []
    for word in words:
        for i in range(1, classes.count(_HARDPOINT_MAX_CLASS[word]) + 1):
            real = fitted_hardpoints.get((word, False, i))
            if real:
                in_use.append(real)
            else:
                free.append(f"{word.capitalize()}Hardpoint{i}")
    n = len(free) + len(in_use)
    question = "Which utility mount?" if module_kind == "utility" else "Which hardpoint?"
    if n == 0:
        if module_kind == "utility":
            return f"Your {ship_name} has no utility mounts."
        return f"Your {ship_name} has no hardpoint that fits a class {module_class} weapon."
    if free:
        return f"{question} Your {ship_name} has {n} {noun}; free right now: {', '.join(free)}. Name one with slot."
    return (f"Your {ship_name} has {n} {noun} and all of them are in use; "
            f"to replace one, name it with slot: {', '.join(in_use)}.")


def _resolve_target_slot(conn, ship_key, grp, explicit_slot, module_kind=None, module_class=None):
    """Which slot a swap targets. An explicit slot always wins (casing
    corrected against reality, R5). Otherwise: resolve from what's
    CURRENTLY FITTED of that grp first (R1) -- this must run before the
    standard-slot fallback below, because a grp like `ft` covers both the
    one standard FuelTank slot AND any optional-internal fuel tanks, and
    a ship with both fitted is genuinely ambiguous, not automatically the
    standard slot. Only when NOTHING of that grp is fitted does a
    standard module (fsd/pp/t/ls/pd/s/ft) fall back to its one fixed
    journal slot name -- every ship has exactly one, so that's not a
    guess. Anything else with nothing fitted (optional internals,
    hardpoints, utility mounts) has no verified way to map Coriolis's
    slot-size layout to the journal's own slot names (see the plan's
    Review Focus #2 scope note) -- rather than guess, this asks for an
    explicit slot, the same honest treatment bulkheads already get."""
    explicit_slot = (explicit_slot or "").strip()
    if explicit_slot:
        return _resolve_explicit_slot(conn, explicit_slot)

    fitted = conn.execute(
        "SELECT clm.slot FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) "
        "WHERE ms.grp = ?", (grp,),
    ).fetchall()
    if len(fitted) == 1:
        return fitted[0]["slot"], None
    if len(fitted) > 1:
        names = ", ".join(sorted(r["slot"] for r in fitted))
        return None, f"ship has {len(fitted)} slots of that type fitted -- specify which slot: {names}"
    if grp in STANDARD_SLOT_NAMES:
        return STANDARD_SLOT_NAMES[grp], None
    return None, _which_slot_reason(conn, ship_key, module_kind, module_class) or (
        "nothing of that type is currently fitted, so there's no slot to resolve automatically -- "
        "specify slot explicitly (e.g. an empty utility slot's name)")


def _resolve_ship_stats(conn, ship_key):
    """current_loadout.ship_key -> its ship_stats row: a direct match
    first, then through JOURNAL_SHIP_TO_CORIOLIS for the ships where the
    journal's internal name doesn't match Coriolis's own key."""
    if not ship_key:
        return None
    row = conn.execute("SELECT * FROM ship_stats WHERE lower(ship_key) = lower(?)", (ship_key,)).fetchone()
    if row:
        return row
    mapped = JOURNAL_SHIP_TO_CORIOLIS.get(ship_key.lower())
    if not mapped:
        return None
    return conn.execute("SELECT * FROM ship_stats WHERE lower(ship_key) = lower(?)", (mapped,)).fetchone()


def _classify_slot_kind(target_slot):
    """Which of the 4 recognized slot kinds target_slot is, or None for
    "anything else" (Military01, Cargo01, LimpetController01, FighterBay01,
    PlanetaryApproachSuite, Armour, ShipCockpit, CargoHatch, cosmetics, ...)
    -- those are a later extension (S1), never guessed at here. Names are
    already case-corrected by the time this is called (R5)."""
    if target_slot in STANDARD_SLOT_NAMES.values():
        return "standard"
    m = _HARDPOINT_SLOT_RE.fullmatch(target_slot)
    if m:
        return "utility" if m.group(1).lower() == "tiny" else "hardpoint"
    if _OPTIONAL_INTERNAL_SLOT_RE.fullmatch(target_slot):
        return "internal"
    return None


def _module_kind(symbol, grp, module_class):
    """Which of the 5 recognized module kinds a candidate module is,
    from its symbol prefix and class alone (S1) -- verified across all of
    module_stats: every hpt_ grp is consistently class-0-only (utility) or
    class->=1-only (weapon), so every candidate for one grp shares a kind,
    and this can be taken before any variant disambiguation."""
    if symbol.lower().startswith("hpt_"):
        return "utility" if module_class == 0 else "weapon"
    if grp in _CORE_GRPS:
        return "core"
    if grp == "ft":
        return "fuel tank"
    return "internal"


def _readable_module_name(module_group, grp):
    """A human-readable name for the module being fitted, for use in a
    rejection reason (T1) -- never the raw grp code (e.g. "sb", "t"),
    which reads as gibberish to the commander. STANDARD_GRP_DISPLAY for a
    core grp; else the caller's own module_group when that's already a
    recognized friendly name; else the first MODULE_GROUP_TO_GRP key that
    maps to this grp (covers a caller that passed the raw code)."""
    if grp in STANDARD_GRP_DISPLAY:
        return STANDARD_GRP_DISPLAY[grp]
    if module_group.strip().lower() in MODULE_GROUP_TO_GRP:
        return module_group
    for name, g in MODULE_GROUP_TO_GRP.items():
        if g == grp:
            return name
    return module_group


def _kind_mismatch_reason(module_group, grp, module_kind, slot_kind, target_slot):
    """None if module_kind is allowed in slot_kind/target_slot (S1's
    "Allowed pairs"), else a plain reason. Runs before variant
    disambiguation and before the size check -- an impossible pairing is
    impossible regardless of which specific variant or how big the slot
    turns out to be.

    NB1/T1: a core-module mismatch has TWO separate cases, kept in
    separate branches so neither can borrow the other's group -- naming
    the module being swapped IN when the real problem is which SLOT it's
    going into (or vice versa) tells the commander something false about
    his ship:
    - target_slot IS one of the 7 standard slots, just the WRONG one:
      name that slot's real owner (round 1's R5 case).
    - target_slot is any other kind entirely (internal/hardpoint/
      utility): name the module being swapped in -- there's no "wrong
      standard slot" to blame, it just doesn't belong outside its own."""
    if module_kind == "core":
        if target_slot == STANDARD_SLOT_NAMES[grp]:
            return None
        if slot_kind == "standard":
            owner_grp = next(g for g, name in STANDARD_SLOT_NAMES.items() if name == target_slot)
            return f"{target_slot} only takes a {STANDARD_GRP_DISPLAY[owner_grp]}"
        return f"a {STANDARD_GRP_DISPLAY[grp]} only fits the {STANDARD_SLOT_NAMES[grp]} slot"

    display = _readable_module_name(module_group, grp)
    if module_kind == "fuel tank":
        if slot_kind == "internal" or target_slot == STANDARD_SLOT_NAMES["ft"]:
            return None
        return f"a {display} only fits the standard fuel tank slot or an internal slot"
    if module_kind == "internal":
        if slot_kind == "internal":
            return None
        return f"a {display} only fits an internal slot"
    if module_kind == "weapon":
        if slot_kind == "hardpoint":
            return None
        return f"a {display} only fits a hardpoint mount"
    if module_kind == "utility":
        if slot_kind == "utility":
            return None
        return f"a {display} only fits a utility mount"
    return None


def _blueprint_name_matches(rows, name, module_words=None, use_tail=True):
    """The rows (anything with fdname and upgrade_name) matching a typed
    blueprint name, by the FIRST of these rules that hits anything, all
    case-insensitive:
      1. the display name, stripped and with inner whitespace collapsed
         ('  Increased  range ' -> 'Increased range');
      2. the journal's own name ('FSD_LongRange');
      3. that name without its first 'Module_' segment, underscores ignored
         ('LongRange', 'long_range');
      4. the display name with spaces, hyphens, underscores and punctuation
         removed ('increased-range', 'IncreasedRange');
      5. an in-game alias from blueprint_mapping.BLUEPRINT_ALIASES;
      6. every word of the display name, or of the part of the fdname after the
         underscore, appears among the typed words ('Increased FSD Range' for
         'Increased range'); the matches with the MOST words win. Rule 6 refuses a name with other
         words besides those, unless `module_words` is given and holds them (or they are in
         GENERIC_EXTRA_WORDS): 'Engine Focused' is not 'Focused' for a laser. `module_words=None` skips
         that check (used only to explain a miss). `use_tail=False` ignores the fdname's words (effects).
    Returns every row the winning rule matched (the caller refuses an
    ambiguous result rather than picking one). A blank name matches nothing."""
    raw = name.strip().lower()
    if not raw:
        return []
    squashed = re.sub(r"[\W_]+", "", raw)

    def tail(fdname):
        return fdname.split("_", 1)[1] if "_" in fdname else None

    rules = (
        lambda r: " ".join(r["upgrade_name"].split()).lower() == " ".join(raw.split()),
        lambda r: r["fdname"].lower() == raw,
        lambda r: tail(r["fdname"]) is not None and tail(r["fdname"]).lower().replace("_", "") == raw.replace("_", ""),
        lambda r: bool(squashed) and re.sub(r"[\W_]+", "", r["upgrade_name"].lower()) == squashed,
        lambda r: any(squash(alias) == squashed for alias in BLUEPRINT_ALIASES.get(r["fdname"], ())),
    )
    for rule in rules:
        hits = [r for r in rows if rule(r)]
        if hits:
            return hits

    typed = name_tokens(name)
    allowed = None if module_words is None else set(module_words) | GENERIC_EXTRA_WORDS
    scored = []
    for r in rows:
        candidates = [name_tokens(r["upgrade_name"])]
        if use_tail:
            candidates.append(name_tokens(tail(r["fdname"]) or ""))
        sizes = [len(words) for words in candidates
                 if words and words <= typed and (allowed is None or (typed - words) <= allowed)]
        if sizes:
            scored.append((max(sizes), r))
    if not scored:
        return []
    best = max(size for size, _ in scored)
    return [r for size, r in scored if size == best]


def _find_blueprint(conn, grp, grade, blueprint_name, module_group):
    """(blueprint_row, None) for the one allow-listed blueprint of this grp and
    grade that `blueprint_name` means, else (None, result_dict) with a reason
    that says WHY it wasn't found: it exists but not for this group, or isn't
    a blueprint Ellie knows at all -- and what this group does offer."""
    candidates = conn.execute(
        "SELECT cb.fdname, cb.upgrade_name, cb.feature_ranges_json FROM coriolis_blueprints cb "
        "JOIN module_blueprints mb ON mb.fdname = cb.fdname WHERE mb.grp = ? AND cb.grade = ?",
        (grp, grade),
    ).fetchall()
    hits = _blueprint_name_matches(candidates, blueprint_name, module_words=_module_words(conn, [grp], module_group))
    if len(hits) > 1:
        # Never take the first row. Not reachable with today's data
        # (verified: 0 upgrade_name collisions within any grp's allow-list at
        # any grade) -- this guards future data.
        return None, {"found": False, "reason": f"multiple blueprint definitions match '{blueprint_name}' grade {grade} for {module_group} -- Ellie's data needs a closer look before this can be applied safely"}
    if hits:
        return hits[0], None

    available = sorted({r[0] for r in conn.execute(
        "SELECT cb.upgrade_name FROM coriolis_blueprints cb "
        "JOIN module_blueprints mb ON mb.fdname = cb.fdname WHERE mb.grp = ?", (grp,))})
    everything = conn.execute("SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints").fetchall()
    offered = f"Blueprints available for {module_group}: {', '.join(available)}."
    if _blueprint_name_matches(everything, blueprint_name):
        return None, {"found": False, "reason": f"'{blueprint_name}' grade {grade} doesn't apply to {module_group}. {offered}"}
    return None, {"found": False, "reason": f"Ellie doesn't know a blueprint called '{blueprint_name}'. {offered}"}


def _entry_class(entry):
    """A ship_stats slots_json hardpoints/internal entry's class -- a
    plain int, or a dict with a "class" key (a reserved/named slot)."""
    return entry if isinstance(entry, int) else entry.get("class")


def _hardpoint_slot_exists(ship_row, size_word, index):
    """S3: does the ship really have an Nth hardpoint of this size? Counts
    EVERY entry of that class in slots_json["hardpoints"], named or plain
    -- a reserved/named hardpoint still occupies a real slot of that size
    (verified: 3,021 real hardpoint slots, 0 violations)."""
    hardpoints = json.loads(ship_row["slots_json"]).get("hardpoints") or []
    target_class = _HARDPOINT_MAX_CLASS[size_word]
    count = sum(1 for e in hardpoints if _entry_class(e) == target_class)
    return index <= count


def _largest_plain_internal_class(ship_row):
    """S3(b): the largest ordinary (non-reserved) internal slot on this
    ship. Plain entries are ints, or dicts WITHOUT a "name" key -- named
    entries (Military, Cargo, Limpets, Fighter, PlanetaryApproachSuite)
    are reserved slots addressed by their own journal slot name, not by
    SlotNN_SizeM. Verified: 0 violations in 12,764 real internal slots."""
    internal = json.loads(ship_row["slots_json"]).get("internal") or []
    plain = [_entry_class(e) for e in internal if isinstance(e, int) or "name" not in e]
    return max(plain) if plain else None


def _internal_slot_collision(conn, nn):
    """S3(a): if a FITTED internal slot shares this NN under a different
    name, that fitted name is the real one -- the requested SlotNN_SizeM
    is wrong, not just unfitted. Verified: 0 repeated NN within one real
    loadout, across 12,764 internal slots."""
    for row in conn.execute("SELECT slot FROM current_loadout_modules").fetchall():
        m = _OPTIONAL_INTERNAL_SLOT_RE.fullmatch(row["slot"])
        if m and int(m.group(1)) == nn:
            return row["slot"]
    return None


def _slot_size_and_existence(conn, ship_row, ship_key, target_slot, slot_kind, old_module_row, old_stats):
    """The largest module class target_slot can hold, PLUS (for an
    explicit slot that isn't currently fitted) whether it plausibly
    exists at all (S3). Returns (max_class, caveat, early_return):
    early_return is a result dict to return immediately when set;
    otherwise max_class/caveat are valid and the caller proceeds.
    caveat is True only on the one path where existence was inferred,
    not confirmed (an unfitted internal slot that passed both checks).

    - Standard slots: sized from the slot's OWN owner grp (reverse
      STANDARD_SLOT_NAMES lookup), never from the swap's `grp` -- S2
      requires this to hold even if S1's kind check were absent. A
      standard slot always exists by definition, no S3 check needed.
      (Verified: 12,068 real-loadout checks, 0 violations.)
    - Hardpoint/utility, fitted: sized by the slot's own size word.
    - Hardpoint/utility, NOT fitted: S3 checks the ship really has an
      Nth hardpoint of that size before sizing it.
    - Internal, fitted: M, or the fitted module's class if that's larger
      (the one real exception -- the Keelback's Slot03_Size3 holding a
      class-4 cargo rack -- falls out of this generic rule).
    - Internal, NOT fitted: S3's number-collision and plain-max-size
      checks (Review Focus #2 -- empty internal slots aren't otherwise
      verifiable at all); passing both adds the caveat.
    """
    if slot_kind == "standard":
        owner_grp = next(g for g, name in STANDARD_SLOT_NAMES.items() if name == target_slot)
        if not ship_row:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s slot layout, so the fit can't be checked"}
        standard_sizes = json.loads(ship_row["slots_json"]).get("standard") or []
        if len(standard_sizes) != len(STANDARD_SLOTS_ORDER):
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s standard slot sizes, so the fit can't be checked"}
        return standard_sizes[STANDARD_SLOTS_ORDER.index(owner_grp)], False, None

    if slot_kind in ("hardpoint", "utility"):
        m = _HARDPOINT_SLOT_RE.fullmatch(target_slot)
        size_word, index = m.group(1).lower(), int(m.group(2))
        max_class = _HARDPOINT_MAX_CLASS[size_word]
        if not old_module_row:
            if not ship_row:
                return None, False, {"found": False, "reason": f"don't know {ship_key}'s hardpoint layout, so the fit can't be checked"}
            if not _hardpoint_slot_exists(ship_row, size_word, index):
                ship_name = ship_row["name"] or ship_key
                return None, False, {
                    "found": True, "compatible": False, "slot": target_slot,
                    "reason": f"your {ship_name} has no {target_slot}",
                }
        return max_class, False, None

    if slot_kind == "internal":
        m = _OPTIONAL_INTERNAL_SLOT_RE.fullmatch(target_slot)
        nn, size_m = int(m.group(1)), int(m.group(2))
        if old_module_row:
            max_class = size_m
            if old_stats and old_stats["class"] and old_stats["class"] > max_class:
                max_class = old_stats["class"]
            return max_class, False, None

        collision = _internal_slot_collision(conn, nn)
        if collision:
            return None, False, {"found": False, "reason": f"your ship's slot {nn} is {collision}"}
        if not ship_row:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s internal slot layout, so the fit can't be checked"}
        largest_plain = _largest_plain_internal_class(ship_row)
        if largest_plain is None:
            return None, False, {"found": False, "reason": f"don't know {ship_key}'s internal slot layout, so the fit can't be checked"}
        if size_m > largest_plain:
            ship_name = ship_row["name"] or ship_key
            return None, False, {
                "found": True, "compatible": False, "slot": target_slot,
                "reason": f"your {ship_name} has no size-{size_m} internal slot",
            }
        return size_m, True, None  # caveat: existence inferred, not confirmed

    return None, False, {"found": False, "reason": f"don't recognize '{target_slot}''s size, so the fit can't be checked"}


def _coerce_int_in_range(value, lo, hi, label):
    """Accepts an int, a whole-number float (5.0 -> 5, T2/NB2), or a
    whole-number string (N4/S5 -- a small model can easily pass
    module_class="5"); anything else -- a genuinely fractional value like
    5.5, or unparseable garbage -- or an out-of-range value, is a plain
    reason, never a TypeError from a later comparison."""
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None, f"'{value}' isn't a valid {label}"
            n = int(value)
        else:
            n = int(str(value).strip())
    except (ValueError, TypeError):
        return None, f"'{value}' isn't a valid {label}"
    if not (lo <= n <= hi):
        return None, f"{label} {n} is out of range ({lo}-{hi})"
    return n, None


def _modifier_list(modifiers_json):
    """A stored Engineering.Modifiers JSON as a list of dicts. The text comes
    from external data ('null' when a journal's Modifiers is null, in
    principle anything); whatever isn't a list of dicts is treated as having no
    modifiers, so one bad row can't take a tool down."""
    try:
        modifiers = json.loads(modifiers_json) if modifiers_json else []
    except (ValueError, TypeError):
        return []
    if not isinstance(modifiers, list) or not all(isinstance(m, dict) for m in modifiers):
        return []
    return modifiers


def _engineered_modifier(modifiers_json, label):
    """One journal Engineering.Modifiers entry's real Value by Label, or
    None if there's no Engineering block, no modifier with that label, or the
    Value isn't a number (every modifier read here feeds arithmetic)."""
    for mod in _modifier_list(modifiers_json):
        if mod.get("Label") == label:
            value = mod.get("Value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            return None
    return None


def _engineered_or_base(modifiers_json, label, base_value):
    """The journal's real engineered value for `label` if present (ground
    truth for a module the commander actually has fitted), else the base
    module_stats value -- NULL there means 0.0 (R2): Coriolis omits
    zero-valued fields, never treat a known module as massless/powerless
    just because that field is absent."""
    value = _engineered_modifier(modifiers_json, label)
    return value if value is not None else (base_value or 0.0)


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


def _draws_nothing(item_lower):
    """Cockpits/armour/cosmetics are ship-cosmetic or bulkhead-only slots,
    never present in module_stats -- checked by name so they're never
    mistaken for an unknown module in the power-margin sum (R7)."""
    if item_lower.endswith(_ZERO_DRAW_SUFFIXES):
        return True
    if item_lower.startswith(_ZERO_DRAW_PREFIXES):
        return True
    return any(s in item_lower for s in _ZERO_DRAW_SUBSTRINGS)


def _power_draw_total(conn, target_slot):
    """Sum of power draw for every currently-On module except the one in
    target_slot (that one's being removed -- R5's "a removed module
    contributes nothing" applies to power too, not just the FSD
    booster's jump bonus). Hardpoints included; the cargo hatch counts as
    a flat 0.6 MW when On (R7); a module's own real engineered PowerDraw
    is preferred over its base module_stats value, consistent with this
    project's global "prefer the journal's ground truth" rule. Returns
    (total, unknown_items): unknown_items is non-empty when an On module
    outside the zero-draw/cargo-hatch categories isn't in module_stats --
    the caller must treat the margin as unknowable, never silently
    omit it (R7)."""
    rows = conn.execute(
        "SELECT slot, item, engineering_modifiers_json FROM current_loadout_modules "
        "WHERE is_on = 1 AND slot != ?", (target_slot,),
    ).fetchall()
    total = 0.0
    unknown = []
    for row in rows:
        item_lower = row["item"].lower()
        if item_lower.startswith("modularcargobaydoor"):
            total += 0.6
            continue
        if _draws_nothing(item_lower):
            continue
        engineered = _engineered_modifier(row["engineering_modifiers_json"], "PowerDraw")
        if engineered is not None:
            total += engineered
            continue
        stats = conn.execute("SELECT power FROM module_stats WHERE symbol = ?", (item_lower,)).fetchone()
        if stats is None:
            unknown.append(row["item"])
            continue
        total += stats["power"] or 0.0
    return total, unknown


def _effective_plant_output(conn, target_slot, new_module, features):
    """(min, max) power plant output in MW. If the plant itself is being
    swapped (the TARGET SLOT is the standard PowerPlant slot -- S2: keyed
    on the slot, not on `grp`, so this stays correct even if a kind-check
    elsewhere had a gap), that's the new module's pgen (+ the blueprint's
    pgen delta, if one applies); otherwise it's the CURRENTLY fitted
    plant's real reported output -- the journal's own PowerCapacity if
    it's engineered (ground truth), else its base module_stats pgen.
    Returns (None, None) when the currently-fitted plant can't be
    resolved at all, so the caller reports an honestly-unknown margin
    instead of a guess."""
    if target_slot == STANDARD_SLOT_NAMES["pp"]:
        base = new_module_extra_get(new_module, "pgen") or 0.0
        if "pgen" in features:
            return apply_blueprint_delta(base, features["pgen"])
        return base, base

    plant_row = conn.execute(
        "SELECT clm.engineering_modifiers_json, ms.extra_json FROM current_loadout_modules clm "
        "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE clm.slot = ?",
        (STANDARD_SLOT_NAMES["pp"],),
    ).fetchone()
    if not plant_row:
        return None, None
    capacity = _engineered_modifier(plant_row["engineering_modifiers_json"], "PowerCapacity")
    if capacity is not None:
        return capacity, capacity
    pgen = new_module_extra_get(plant_row, "pgen") or 0.0
    return pgen, pgen


def _module_tank_fuel(module_row):
    """Tonnes of fuel a fuel-tank module holds (module_stats `fuel`), else 0."""
    if module_row is None or module_row["grp"] != "ft":
        return 0.0
    return new_module_extra_get(module_row, "fuel") or 0.0


def _module_rack_cargo(module_row, modifiers_json=None):
    """Tonnes a cargo rack holds, else 0: the journal's engineered
    CargoCapacity modifier when this is a fitted rack that has one (the
    Loadout's CargoCapacity is the sum of exactly these, verified on every real
    Loadout), else module_stats `cargo`. A hypothetical rack has no modifiers,
    so it is its base capacity."""
    if module_row is None or module_row["grp"] not in ("cr", "crl"):
        return 0.0
    engineered = _engineered_modifier(modifiers_json, "CargoCapacity")
    return engineered if engineered is not None else (new_module_extra_get(module_row, "cargo") or 0.0)


def _capped(now, capacity):
    """What of `now` fits in `capacity` (None = unknown capacity: no cap)."""
    return now if capacity is None else max(0.0, min(now, capacity))


def _capacity_note(fuel_now, fuel_capacity, cargo_now, cargo_capacity):
    """Why the new range assumes less fuel or cargo than is aboard now, or None
    when nothing is cut. A change below 1e-6 t is float noise, not a cap."""
    notes = []
    if fuel_capacity is not None and fuel_now - max(0.0, fuel_capacity) > 1e-6:
        cap = round(max(0.0, fuel_capacity), 2)
        notes.append(f"the new tank holds only {cap} t, so the new range assumes {cap} t of fuel instead of {round(fuel_now, 2)} t")
    if cargo_capacity is not None and cargo_now - max(0.0, cargo_capacity) > 1e-6:
        cap = round(max(0.0, cargo_capacity), 2)
        notes.append(f"the new hold carries only {cap} t, so the new range assumes {cap} t of cargo instead of {round(cargo_now, 2)} t")
    return "; ".join(notes) or None


def _swap_summary(result):
    """One sentence with the whole before/after, built FROM the structured
    result so every number in it is, by construction, one the result also
    carries (rounded to 2 dp). Clauses whose figures are unknown are left
    out; a roll-dependent {"min", "max"} value reads "between X and Y" and
    gets no change in parentheses, since there is no single change."""
    def text(value):
        return f"between {value['min']} and {value['max']}" if isinstance(value, dict) else str(value)

    def change_text(change, new):
        if isinstance(change, dict) or isinstance(new, dict):
            return ""
        return f" ({change + 0.0:+.2f})"  # + 0.0 so a rounded -0.0 prints as +0.00

    def before_after(label, old, new, change, unit):
        return f"{label} {old} -> {text(new)} {unit}{change_text(change, new)}"

    added = result["added"]["symbol"]
    removed = result["removed"]["item"]
    action = f"swapping {removed} for {added}" if removed else f"adding {added}"
    clauses = []
    if result["old_jump_range_now"] is not None and result["new_jump_range_now"] is not None:
        clauses.append(before_after(
            "jump range now", result["old_jump_range_now"], result["new_jump_range_now"],
            result["jump_range_change_now"], "ly"))
    if result["old_jump_range_max"] is not None and result["new_jump_range_max"] is not None:
        clauses.append(before_after(
            "max", result["old_jump_range_max"], result["new_jump_range_max"],
            result["jump_range_change_max"], "ly"))
    if result["old_unladen_mass"] is not None and result["new_unladen_mass"] is not None:
        clauses.append(before_after(
            "unladen mass", result["old_unladen_mass"], result["new_unladen_mass"], result["mass_change"], "t"))
    if result["new_power_margin"] is not None:
        clauses.append(f"power margin {text(result['new_power_margin'])} MW")

    summary = f"{result['ship_name']}: {action} in {result['slot']}"
    summary += (": " + "; ".join(clauses) + ".") if clauses else "."
    # qualifiers, in a fixed order: what the new capacities did to the range,
    # which blueprint was applied, what it does that the numbers leave out,
    # and the caveat about an empty slot that couldn't be confirmed
    if result.get("capacity_note"):
        summary += f" Note: {result['capacity_note']}."
    if result.get("mass_note"):
        summary += f" Note: {result['mass_note']}"
    if result["added"].get("blueprint"):
        summary += f" Blueprint: {result['added']['blueprint']} grade {result['added']['blueprint_grade']}."
    if result.get("unmodeled_blueprint_effects"):
        summary += f" Not reflected in these numbers: {', '.join(result['unmodeled_blueprint_effects'])}."
    if result.get("caveat"):
        summary += " " + result["caveat"] + ("" if result["caveat"].endswith(".") else ".")
    if result["state_note"]:
        summary += f" Current-load jump range unavailable: {result['state_note']}."
    if result.get("warning"):
        summary += " " + result["warning"]
    return summary


def _unset(value):
    """A small model's way of saying "not given": the strings 'None', 'null',
    'undefined' or an empty one (llama3.2 passed slot="None"). A real slot,
    variant or blueprint is never called that."""
    if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "undefined"):
        return None
    return value


@_needs_setup
def simulate_module_swap(module_group: str, module_class: int, module_rating: str,
                          blueprint_name: str = None, grade: int = None, slot: str = None,
                          variant: str = None) -> dict:
    """'What if I fit X instead' -- swaps one module in the commander's
    CURRENT loadout for a hypothetical one and reports the real resulting
    mass/jump-range/power deltas. Never invents a number: an
    unrecognized module group, an incompatible class/rating, an
    impossible slot/module pairing, or a blueprint that doesn't apply to
    this module category all return a plain not-found/incompatible
    result instead."""
    grp = _module_group_to_grp(module_group)
    if grp is None:
        return {"found": False, "reason": f"'{module_group}' isn't a recognized module type"}
    slot, variant, blueprint_name = _unset(slot), _unset(variant), _unset(blueprint_name)

    # N4/S5: a small model can easily pass "5" instead of 5 -- accept
    # either (or a whole-number float, T2/NB2), reject anything else (or
    # out-of-range) with a plain reason rather than a TypeError from a
    # later numeric comparison. `grade` is deliberately NOT coerced/
    # validated here -- it's only meaningful when a blueprint is actually
    # requested (T2/NB2: a plain swap with grade=0, a 3B model's plausible
    # way of saying "no grade", must be ignored exactly as it was before
    # this validation existed, not rejected as "out of range").
    module_class, class_error = _coerce_int_in_range(module_class, 0, 8, "module class")
    if class_error:
        return {"found": False, "reason": class_error}

    conn = _connect()
    try:
        baseline = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
        if not baseline or not baseline["ship_key"]:
            return {"found": False, "reason": "no current loadout captured yet -- journal_watcher may not be running"}

        # The candidate modules are looked up first only so that "which slot?"
        # can say what KIND of module this is (a utility module, a weapon, an
        # internal one); nothing here returns before the slot is resolved.
        raw_candidates = conn.execute(
            "SELECT * FROM module_stats WHERE grp = ? AND class = ? AND lower(rating) = lower(?)",
            (grp, module_class, module_rating),
        ).fetchall()
        module_kind = _module_kind(raw_candidates[0]["symbol"], grp, module_class) if raw_candidates else None

        target_slot, slot_error = _resolve_target_slot(
            conn, baseline["ship_key"], grp, slot, module_kind=module_kind, module_class=module_class)
        if slot_error:
            return {"found": False, "reason": slot_error}

        # S1: classify the target slot by name. "Anything else" (Military01,
        # Cargo01, LimpetController01, FighterBay01, PlanetaryApproachSuite,
        # Armour, ShipCockpit, CargoHatch, cosmetics, ...) is a later
        # extension -- fail closed exactly as an unrecognized slot size did
        # before this, never guess.
        slot_kind = _classify_slot_kind(target_slot)
        if slot_kind is None:
            return {"found": False, "reason": f"Ellie can't simulate swaps in {target_slot} yet"}

        # Several real modules can share one group/class/rating (standard vs SCO
        # FSDs, fixed/gimballed/turret mounts, standard vs Guardian power plants
        # -- 100 such combinations in coriolis-data, verified). Their stats differ
        # materially, so never pick one arbitrarily: narrow by `variant` if given,
        # otherwise list the candidates and ask, the same honest treatment an
        # ambiguous slot gets. raw_candidates (before the variant filter) is
        # also what S1's kind check uses -- every candidate for one grp shares
        # a kind, so this is valid before any variant disambiguation.
        # S1: does this module kind even fit this slot kind? Runs before the
        # variants ambiguity response and before the size check -- "a beam
        # laser in an internal slot" is wrong whichever mount is meant, so
        # don't make the user pick a mount first.
        if raw_candidates:
            mismatch = _kind_mismatch_reason(module_group, grp, module_kind, slot_kind, target_slot)
            if mismatch:
                return {"found": True, "compatible": False, "slot": target_slot, "reason": mismatch}

        candidates = raw_candidates
        if variant:
            wanted = variant.strip().lower()
            exact = [c for c in candidates if c["symbol"] == wanted]
            candidates = exact or [c for c in candidates if wanted in _variant_search_text(c)]
        if not candidates:
            suffix = f" matching '{variant}'" if variant else ""
            return {"found": False, "reason": f"no {module_group} of class {module_class} rating {module_rating}{suffix} exists"}
        if len(candidates) > 1:
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
        if old_module_row and not old_stats:
            # R2: never treat an unrecognized fitted module as massless --
            # refuse honestly instead of fabricating a "0" for it.
            return {
                "found": False,
                "reason": f"the module currently in {target_slot} ({old_module_row['item']}) isn't in Ellie's module data, so the change can't be computed",
            }

        # S2/S3: size the slot from the SLOT, not from `grp` -- and, for an
        # explicit slot that isn't currently fitted, check it plausibly
        # exists at all (Review Focus #2's scope note: empty slots are
        # otherwise unverifiable). caveat is True only when existence was
        # inferred rather than confirmed (an unfitted internal slot).
        ship_row = _resolve_ship_stats(conn, baseline["ship_key"])
        max_class, caveat, early_return = _slot_size_and_existence(
            conn, ship_row, baseline["ship_key"], target_slot, slot_kind, old_module_row, old_stats
        )
        if early_return:
            return early_return
        if module_class > max_class:
            return {
                "found": True, "compatible": False, "slot": target_slot,
                "reason": f"a class {module_class} {module_group} doesn't fit {target_slot} (max class {max_class})",
            }

        new_mass_min = new_mass_max = new_module["mass"] or 0.0
        new_power_min = new_power_max = new_module["power"] or 0.0
        blueprint_note = None
        features = {}
        unmodeled_keys = []

        if blueprint_name:
            # grade is only validated HERE, not up front (T2/NB2) -- it's
            # meaningless without a blueprint, so an unvalidated grade=0
            # from a caller that never asked for a blueprint must be
            # ignored, not rejected.
            if grade is not None:
                grade, grade_error = _coerce_int_in_range(grade, 1, 5, "grade")
                if grade_error:
                    return {"found": False, "reason": grade_error}
            if not grade:
                return {"found": False, "reason": "a blueprint grade (1-5) is required"}

            # S4: eligibility comes from Coriolis's own per-group allow-list
            # (modifications/modules.json, ingested into module_blueprints by
            # ingest_ship_modules.py), NOT a category-text match -- Coriolis's
            # applies_to text is far coarser than which blueprints actually
            # exist for a group (e.g. "Weapon" covers every weapon grp, but a
            # torpedo pylon only really has LightWeight/Sturdy; a multi-cannon
            # and a beam laser can both match "Overcharged" under two DIFFERENT,
            # differently-featured blueprint definitions).
            allow_list_count = conn.execute(
                "SELECT COUNT(*) FROM module_blueprints WHERE grp = ?", (grp,)
            ).fetchone()[0]
            if allow_list_count == 0:
                return {"found": False, "reason": f"no engineering blueprints exist for {module_group} in Ellie's data"}

            blueprint_row, blueprint_miss = _find_blueprint(conn, grp, grade, blueprint_name, module_group)
            if blueprint_miss:
                return blueprint_miss

            features = json.loads(blueprint_row["feature_ranges_json"])
            applied_keys = set()
            if "mass" in features:
                new_mass_min, new_mass_max = apply_blueprint_delta(new_module["mass"] or 0.0, features["mass"])
                applied_keys.add("mass")
            if "power" in features:
                new_power_min, new_power_max = apply_blueprint_delta(new_module["power"] or 0.0, features["power"])
                applied_keys.add("power")
            if target_slot == STANDARD_SLOT_NAMES["fsd"] and "optmass" in features:
                applied_keys.add("optmass")  # applied separately below, in the jump-range section
            if target_slot == STANDARD_SLOT_NAMES["pp"] and "pgen" in features:
                applied_keys.add("pgen")  # applied separately below, in the power-margin section
            blueprint_note = blueprint_row["upgrade_name"]   # the display name, however the caller spelled it
            # honesty check: a features key this code doesn't know how to apply
            # (e.g. a weapon's "damage"/"rof") must be reported, never silently
            # dropped -- the numbers below would otherwise look complete while
            # missing a real part of the blueprint's effect.
            unmodeled_keys = sorted(set(features) - applied_keys)

        # R2: the removed module's REAL mass/power -- the journal's own
        # engineered Mass/PowerDraw if present (ground truth), else the base
        # module_stats value (NULL there means 0.0, never "unknown").
        if old_module_row:
            old_mass = _engineered_or_base(old_module_row["engineering_modifiers_json"], "Mass", old_stats["mass"])
            old_power = _engineered_or_base(old_module_row["engineering_modifiers_json"], "PowerDraw", old_stats["power"])
        else:
            old_mass = old_power = 0.0

        mass_change_min = new_mass_min - old_mass
        mass_change_max = new_mass_max - old_mass
        # a journal Loadout without UnladenMass leaves every mass figure unknown
        if baseline["unladen_mass"] is None:
            new_unladen_min = new_unladen_max = None
        else:
            new_unladen_min = baseline["unladen_mass"] + mass_change_min
            new_unladen_max = baseline["unladen_mass"] + mass_change_max
        power_change_min = new_power_min - old_power
        power_change_max = new_power_max - old_power

        # jump range needs the ship's EFFECTIVE fsd stats -- if the FSD itself
        # is the slot being swapped (S2: keyed on the TARGET SLOT, not `grp`
        # -- after S1 the two agree, but the slot is what's actually true,
        # and this doesn't depend on S1 being in place), that's the new
        # module (+ blueprint, if given); otherwise it's the CURRENT fsd's
        # real reported stats (preferring the journal's own engineering
        # values over recomputed blueprint math -- see Global Constraints).
        fitted_fsd = _fitted_fsd_params(conn)
        if target_slot == STANDARD_SLOT_NAMES["fsd"]:
            fsd_optmass = new_module_extra_get(new_module, "optmass")
            fsd_maxfuel = new_module_extra_get(new_module, "maxfuel")
            fsd_fuelmul = new_module_extra_get(new_module, "fuelmul")
            fsd_fuelpower = new_module_extra_get(new_module, "fuelpower")
            if "optmass" in features:
                optmass_min, optmass_max = apply_blueprint_delta(fsd_optmass, features["optmass"])
            else:
                optmass_min = optmass_max = fsd_optmass
        else:
            fsd_maxfuel = fitted_fsd["maxfuel"] if fitted_fsd else None
            fsd_fuelmul = fitted_fsd["fuelmul"] if fitted_fsd else None
            fsd_fuelpower = fitted_fsd["fuelpower"] if fitted_fsd else None
            optmass_min = optmass_max = fitted_fsd["optmass"] if fitted_fsd else None

        # Guardian FSD Booster: a flat additive jump-range bonus (loadout_calc.compute_jump_range's
        # booster_bonus parameter -- see that function's own docstring for the honest caveat that
        # this addition, unlike the base formula, isn't cross-verified against a real boosted ship).
        # Stays keyed on `grp` (S2): a booster works from any internal slot, so there's no separate
        # "target slot" to prefer here. Resolved the same way as the effective FSD stats above: if
        # the booster ITSELF is the slot being swapped, use the new module's jumpboost; otherwise use
        # whatever booster (if any) is currently fitted EXCLUDING the target slot (R5) -- a booster
        # being removed contributes nothing, even though its row is still in current_loadout_modules
        # until this swap happens.
        if grp == "gfsb":
            booster_bonus = new_module_extra_get(new_module, "jumpboost") or 0.0
        else:
            booster_row = conn.execute(
                "SELECT ms.extra_json FROM current_loadout_modules clm "
                "JOIN module_stats ms ON ms.symbol = lower(clm.item) WHERE ms.grp = 'gfsb' AND clm.slot != ?",
                (target_slot,),
            ).fetchone()
            booster_bonus = (new_module_extra_get(booster_row, "jumpboost") if booster_row else None) or 0.0

        if None in (fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, optmass_min, new_unladen_min):
            new_range_min = new_range_max = None
        else:
            new_range_min = compute_jump_range(new_unladen_max, optmass_min, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)
            new_range_max = compute_jump_range(new_unladen_min, optmass_max, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus)

        # The same swap with the fuel and cargo the ship has NOW (unchanged by
        # the swap). "old" is the CURRENT ship, with the FSD and booster it
        # really has fitted; "new" reuses the unladen mass, FSD and booster
        # resolved above for the maximum figure.
        fuel_now, cargo_now, state_usable, state_note = _current_ship_state(conn, fitted_fsd)
        old_range_now = new_range_now_min = new_range_now_max = None
        capacity_note = None
        # The swap can change what the ship can hold. Capacities are the
        # Loadout's own totals less the removed module's share plus the added
        # module's; they are reported on every compatible result so the figures
        # in capacity_note (and the thruster mass note) appear in it too.
        new_fuel_capacity = new_cargo_capacity = None
        if baseline["fuel_main"] is not None:
            new_fuel_capacity = baseline["fuel_main"] - _module_tank_fuel(old_stats) + _module_tank_fuel(new_module)
        if baseline["cargo_capacity"] is not None:
            removed_cargo = _module_rack_cargo(old_stats, old_module_row["engineering_modifiers_json"] if old_module_row else None)
            new_cargo_capacity = baseline["cargo_capacity"] - removed_cargo + _module_rack_cargo(new_module)
        if state_usable:
            # The new figures use the fuel and cargo that still fit (old_* and
            # the reported fuel_now/cargo_now stay the real current values).
            fuel_for_new = _capped(fuel_now, new_fuel_capacity)
            cargo_for_new = _capped(cargo_now, new_cargo_capacity)
            capacity_note = _capacity_note(fuel_now, new_fuel_capacity, cargo_now, new_cargo_capacity)
            old_range_now = compute_current_jump_range(
                baseline["unladen_mass"], fuel_now, cargo_now, fitted_fsd["optmass"], fitted_fsd["maxfuel"],
                fitted_fsd["fuelmul"], fitted_fsd["fuelpower"], fitted_fsd["booster_bonus"],
            )
            if new_range_min is not None:
                new_range_now_min = compute_current_jump_range(
                    new_unladen_max, fuel_for_new, cargo_for_new, optmass_min, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus,
                )
                new_range_now_max = compute_current_jump_range(
                    new_unladen_min, fuel_for_new, cargo_for_new, optmass_max, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower, booster_bonus,
                )
        old_range_max = baseline["max_jump_range"]  # the game's own MaxJumpRange
        range_change_max = range_change_now = None
        if new_range_min is not None and old_range_max is not None:
            range_change_max = _min_max_or_single(new_range_min - old_range_max, new_range_max - old_range_max)
        if new_range_now_min is not None:
            range_change_now = _min_max_or_single(new_range_now_min - old_range_now, new_range_now_max - old_range_now)

        # R7: power margin on a DEPLOYED basis -- effective plant output minus
        # every currently-On module's draw (target slot's old module excluded,
        # the new module's draw added instead), hardpoints and the cargo
        # hatch's flat 0.6 MW included. An On module this code can't identify
        # makes the margin honestly unknown, never a guess.
        plant_output_min, plant_output_max = _effective_plant_output(conn, target_slot, new_module, features)
        draw_total, unknown_power_items = _power_draw_total(conn, target_slot)
        if plant_output_min is None or unknown_power_items:
            new_power_margin = None
        else:
            margin_min = plant_output_min - (draw_total + new_power_max)
            margin_max = plant_output_max - (draw_total + new_power_min)
            new_power_margin = _min_max_or_single(margin_min, margin_max)

        # B8: a thruster is rated for ships up to a maximum mass. Measured on every
        # real thruster fitting (1,854) the loaded ship was below it, so only a
        # hypothetical thruster that falls short is worth a note. (Shield
        # generators are rated the same way but real setups exceed it often, so
        # they get nothing.)
        mass_note = None
        if new_module["grp"] == "t":
            maxmass = new_module_extra_get(new_module, "maxmass")
            if maxmass is not None and None not in (new_unladen_max, new_fuel_capacity, new_cargo_capacity):
                fully_loaded = new_unladen_max + new_fuel_capacity + new_cargo_capacity
                if fully_loaded > maxmass:
                    mass_note = (
                        f"These thrusters are rated for ships up to {round(maxmass, 2)} t; "
                        f"fully loaded, your ship would weigh {round(fully_loaded, 2)} t."
                    )

        result = {
            "found": True,
            "compatible": True,
            "ship_name": ship_row["name"] if ship_row else baseline["ship_key"],
            "slot": target_slot,
            "removed": (
                {"item": old_module_row["item"], **_module_fields(old_module_row["item"], old_stats)}
                if old_module_row else {"item": None}
            ),
            "added": {
                "symbol": new_module["symbol"], "class": module_class, "rating": module_rating,
                "blueprint": blueprint_note,
                "blueprint_grade": grade if blueprint_note else None,
                "mass": _min_max_or_single(new_mass_min, new_mass_max),
                "power": _min_max_or_single(new_power_min, new_power_max),
            },
            "mass_change": _min_max_or_single(mass_change_min, mass_change_max),
            "old_unladen_mass": _round2(baseline["unladen_mass"]),
            "new_unladen_mass": _min_max_or_single(new_unladen_min, new_unladen_max) if new_unladen_min is not None else None,
            "fuel_now": _round2(fuel_now),
            "cargo_now": _round2(cargo_now),
            "state_note": state_note,  # why the *_jump_range_now figures are None, when they are
            "old_jump_range_now": _round2(old_range_now),
            "new_jump_range_now": _min_max_or_single(new_range_now_min, new_range_now_max) if new_range_now_min is not None else None,
            "jump_range_change_now": range_change_now,
            "old_jump_range_max": _round2(old_range_max),
            "new_jump_range_max": _min_max_or_single(new_range_min, new_range_max) if new_range_min is not None else None,
            "jump_range_change_max": range_change_max,
            "new_fuel_capacity": _round2(new_fuel_capacity),
            "new_cargo_capacity": _round2(new_cargo_capacity),
            "power_change": _min_max_or_single(power_change_min, power_change_max),
            "new_power_margin": new_power_margin,
            "power_unknown_modules": unknown_power_items,  # honestly-reported, never silently dropped
            "unmodeled_blueprint_effects": unmodeled_keys,  # honestly-reported, never silently dropped
        }
        if capacity_note:
            result["capacity_note"] = capacity_note
        if mass_note:
            result["mass_note"] = mass_note
        if caveat:
            # S3(c): only on the "unfitted internal slot, existence inferred
            # not confirmed" path -- Task 6 relays this to the model.
            result["caveat"] = (
                f"{target_slot} isn't fitted in your current loadout, so Ellie can't confirm "
                "it's an empty slot on your ship -- these numbers assume it is"
            )
        # same honesty guard as get_current_loadout, on the same terms: only
        # alongside a current-load figure it qualifies
        warning = _jump_check_warning(conn, fitted_fsd) if old_range_now is not None else None
        if warning:
            result["warning"] = warning
        result["summary"] = _swap_summary(result)
        return result
    finally:
        conn.close()
