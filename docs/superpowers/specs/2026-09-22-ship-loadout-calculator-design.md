# Ship loadout calculator — Phase A design

Status: approved by the user 2026-09-22, ready for an implementation plan.

## Context

`plan.md`'s Backlog already flagged "ship-build recommendation" as a real future phase requiring genuine new tools (mass budget, slot/module compatibility, jump range, power budget) — explicitly *not* a model or prompt problem, the same reasoning behind every other tool in this project (`get_blueprint`, `missing_materials`, etc.): a small local model should never compute a number itself, only call a function and phrase the result.

"Ship-build recommendation" turned out to be three layered ideas, not one:

- **Phase A (this spec):** evaluate a loadout — describe a hypothetical change, or read your current one, and get real numbers.
- **Phase B (future, fast follow):** "what fits this slot" — loop Phase A's evaluator over every compatible module for one slot.
- **Phase C (future, separate project):** a full build optimizer searching for the best build toward a stated goal. Out of scope here; it's a genuinely different, much larger problem, and coriolis.io/EDSY already do it well.

## Data already available (verified, not assumed)

`coriolis-data` (already cloned into this project for blueprint ingestion) also carries `ships/*.json` (one file per ship: hull mass, base stats, slot layout, default fit) and `modules/{standard,internal,hardpoints}/*.json` (one file per module family: mass, power, cost, class/rating, and group-specific fields like an FSD's `optmass`/`fuelmul`/`fuelpower`/`maxfuel`). No new download, no new dependency — verified by reading `ships/python.json` and `modules/standard/frame_shift_drive.json` directly.

The journal's `Loadout` event (verified against the commander's own real journal) reports the game's own already-computed `MaxJumpRange` and `UnladenMass` for the current ship, plus every fitted module including real per-module `Engineering.Modifiers` (exact current values, not just a blueprint name) when engineered. `journal_watcher.py` does not currently handle this event at all — today, nothing in Ellie knows the current ship's actual loadout, mass, or jump range.

Coriolis's `modifications/blueprints.json` gives structured, numeric per-grade modifier ranges (`features`: `{stat: [min_pct, max_pct]}`) for graded blueprints — verified against `FSD_LongRange` grade 5 (`optmass: [0.45, 0.55]`). `modifications/specials.json` (experimental effects) does **not** carry equivalent structured numeric data for at least the one entry checked (`special_fsd_heavy`, "Mass Manager" — description text only). This asymmetry is load-bearing for scope below.

## Scope decisions (confirmed with the user)

1. Hypothetical modules **can** have a graded blueprint applied and numerically modeled (ranges, since real engineering rolls are randomized — a range is the honest answer, not an approximation of one).
2. Experimental effects are **deferred to a later phase**; Phase A does not handle them at all. The data to model them numerically isn't there, and `simulate_module_swap` has no argument for naming one: it takes only a graded blueprint (`blueprint_name` + `grade`). (An earlier draft of this spec had a requested experimental effect reported as compatible with its material cost; that code path was never built, by choice.) An experimental effect's material cost remains available through the existing `get_experimental_effect` tool, and no experimental effect is ever assigned a fabricated number.
3. The tool handles **one module swap at a time**, not full from-scratch custom builds. Reasoning: covers the large majority of real questions, and keeps the tool's arguments flat and simple — this project's own Phase 4 testing already found small models get unreliable with complex/nested tool arguments (multiple tools, long descriptions). A "list of changes" argument is exactly that failure shape.
4. Bulkhead/armor swaps are **out of scope** for this tool (bulkheads live per-ship in `ships/*.json`, not in the shared `modules/` files — a structurally different lookup). Candidate for a Phase B-style fast follow, not silently bundled in here.

## Data model

New tables, all owned by the new ingest script (safe to rerun, drops/recreates only its own tables — same convention as `ingest_coriolis.py`):

```sql
CREATE TABLE ship_stats (
    ship_key TEXT PRIMARY KEY,      -- coriolis-data's own filename stem, e.g. "python"
    ed_id INTEGER,
    name TEXT,                      -- display name, e.g. "Python"
    manufacturer TEXT,
    hull_mass REAL,
    properties_json TEXT,           -- the rest of ships/*.json's "properties" block, kept for later use
    slots_json TEXT,                -- {"standard": [...], "hardpoints": [...], "internal": [...]}
    defaults_json TEXT,             -- which module fills each slot by default
    bulkheads_json TEXT             -- captured now; not used by this tool (see scope decision 4)
);

CREATE TABLE module_stats (
    symbol TEXT PRIMARY KEY,        -- lowercased, matched against the journal's own Item field
    coriolis_id TEXT,               -- coriolis-data's short id, e.g. "Ft"
    ed_id INTEGER,
    grp TEXT,                       -- module family, e.g. "fsd", "pp" (power plant)
    class INTEGER,
    rating TEXT,
    mass REAL,
    power REAL,                     -- power draw (or output, for power plants)
    cost INTEGER,
    extra_json TEXT                 -- group-specific fields: optmass/fuelmul/fuelpower/maxfuel for fsd, etc.
);
```

`journal_watcher.py` gains a `Loadout` handler writing two snapshot tables (replaced wholesale each event, the same pattern `Materials` already uses):

```sql
CREATE TABLE current_loadout (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ship_key TEXT, ship_id INTEGER, ship_name TEXT,
    max_jump_range REAL, unladen_mass REAL, cargo_capacity REAL,
    fuel_main REAL, fuel_reserve REAL,
    hull_value INTEGER, modules_value INTEGER, rebuy INTEGER,
    last_updated TEXT
);

CREATE TABLE current_loadout_modules (
    slot TEXT PRIMARY KEY,
    item TEXT, is_on INTEGER, priority INTEGER, health REAL, value INTEGER,
    engineering_blueprint TEXT, engineering_level INTEGER, engineering_quality REAL,
    engineering_experimental TEXT, engineering_modifiers_json TEXT,   -- the real Modifiers array, stored verbatim
    last_updated TEXT
);
```

## New query functions (`query_functions.py`)

- **`get_current_loadout()`** — joins `current_loadout` + `current_loadout_modules`, returns ship name, jump range, mass, cargo, fuel, and the module list with real engineering info where present. `@_needs_setup`-decorated like the rest.
- **`get_ship_info(ship_name)`** — base stats and slot layout for a named ship. Small, cheap addition once `ship_stats` exists; also useful standalone ("how many internal slots does a Python have").
- **`simulate_module_swap(module_group, module_class, module_rating, blueprint_name=None, grade=None, slot=None)`** — the core calculator. Resolves the baseline from `current_loadout_modules` (current ship), finds the target slot, looks up the requested module in `module_stats`, applies the blueprint's `features` range if given, and returns mass/jump-range/power deltas plus a compatibility verdict. `module_group` is matched against a small known-name mapping, the same spirit as `blueprint_mapping.py`'s existing module→category table.

  **Slot resolution, made explicit:** when `slot` is omitted, resolve it from `module_group` against the ship's current loadout. If the ship has exactly one fitted slot of that group, use it. If it has more than one (e.g. several hardpoints of the same size) and `slot` wasn't given, return `{"found": False, "reason": "ship has N slots of that type -- specify which (e.g. \"Slot01_Size7\")"}` rather than guessing which one the commander meant. Never silently pick one.

  **Blueprint delta application, made explicit:** for each stat in the chosen grade's `features`, `new_value = base_stat * (1 + pct)` for both the min and max of the range, giving `new_value_min`/`new_value_max`. When a grade's min equals its max (several do, e.g. grade 1 `mass`/`integrity`/`power` above), the "range" collapses to one exact figure -- report it as a single value, not a fake min-max spread.

Result shape (success): `{"found": True, "slot": ..., "removed": {...}, "added": {...}, "mass_change": ..., "new_unladen_mass": ..., "new_jump_range": {"min": ..., "max": ...} | float, "power_change": ..., "new_power_margin": ..., "compatible": true}`. (As built, `new_jump_range` became `new_jump_range_max`, and the result also carries the current-load ranges, the `old_*` baselines, `ship_name`, `summary` and the `capacity_note`/`warning` qualifiers; `query_functions.simulate_module_swap` is the reference for the exact keys.) Result shape (incompatible): `{"found": True, "compatible": false, "reason": "<why>"}` — a real answer, not an error, since "that doesn't fit" is itself useful information. Result shape (genuinely not found, e.g. no such module): `{"found": False, "reason": ...}`, matching every other tool's convention.

## New tools (`ellie_chat.py`)

`get_current_loadout`, `get_ship_info`, and `simulate_module_swap` added to `TOOLS`/`DISPATCH` following the exact existing pattern — no changes to the chat loop itself.

## `setup.py` change

A new `setup_ship_modules()` step, same shape as `setup_coriolis()` (checks existing row counts, asks before re-ingesting, no network needed — it reads the same already-cloned `coriolis-data` directory `setup_coriolis()` ensures exists). Runs immediately after the Coriolis blueprint step in `main()`.

## Error handling

- No `Loadout` event captured yet (fresh session, haven't undocked) → `{"found": False, "reason": "..."}`, same as every other missing-data case — not a crash.
- Requested module/class/rating doesn't exist, or doesn't fit the target slot's size, or the ship has no slot of that group at all → a real, named compatibility reason, never a silently wrong number.
- Requested blueprint doesn't apply to that module category → `{"found": False}`, matching `get_blueprint`'s existing not-found shape.

## Testing plan

- **Formula verification (the load-bearing one):** implement the FSD jump-range formula and verify it two independent ways — against coriolis.io's live calculator for several ship+FSD combinations, and against the commander's own real, already-known-correct number (`MaxJumpRange: 73.964989` from his real `Loadout` event, computed from his real engineered FSD). Reproducing that exact figure from `ship_stats`/`module_stats`/his real `Engineering.Modifiers` is the regression test.
- **Ingestion consistency:** every module referenced in a ship's `defaults` resolves in `module_stats`; every `features` stat key used by a blueprint actually corresponds to a real `module_stats` field for modules in that blueprint's category (assert this at ingest time, fail loudly on mismatch rather than silently drop it).
- **Symbol matching:** Coriolis's `symbol` vs. the journal's `Item` field, tested against the commander's own real `current_loadout_modules` once captured — this project has hit exactly this class of two-systems-name-things-differently bug twice already (the Excel column quirk, the material-name-spacing bug), so this gets a real test, not an assumption.
- **Crash-gap parity:** missing `ship_stats`/`module_stats`/`current_loadout` tables produce the same clean `{"found": False}` pattern as every other tool, verified against an empty database the same way the Phase 5b crash-gap fix was verified.

## Open implementation risks (named honestly, not asserted away)

1. **The exact FSD jump-range formula is not written into this spec**, deliberately — I'm not fully certain of the exact constant/exponent placement from memory, and this project's entire premise is not trusting memory for game data. It gets derived and verified against the two independent oracles above during implementation, not guessed here.
2. **Rating letters (A-E) aren't a simple "best-to-worst" ordering across every module type.** The tool takes the exact class+rating requested; it never infers "the best one."
3. Coriolis's `features` stat key names are assumed to line up with `module_stats`' own field names (both come from the same upstream source) — verified programmatically per the ingestion-consistency test above, not assumed.
