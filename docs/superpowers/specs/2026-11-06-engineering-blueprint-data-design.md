# Engineering depth, part 1: trustworthy blueprint data

Date: 2026-11-06. Status: draft for the owner's review.
Parent: roadmap spec `2026-11-03-ellie-roadmap-design.md`, section 8, item 1 ("Engineering depth"), first half. The second half (craft-now, shopping lists, engineer unlock tracker) gets its own spec after this one ships.

## 1. Purpose and success

A commander asks "what does FSD Increased Range grade 5 cost?" or "what am I missing for Dirty Drives grade 3?" and gets a correct, sourced answer for **every engineerable module**, in the words players actually use. Where Ellie does not know a cost she says so. She never states a wrong holding.

Done when all of these hold, each pinned by a test (section 7):

1. Every blueprint and grade that Coriolis lists for any module group can be looked up by a friendly module name and by the blueprint's in-game or Coriolis name, and returns Coriolis's cost.
2. The 48 (blueprint, grade) combinations and the 9 experimental effects seen in the owner's real journals return the costs those crafts actually consumed, or the difference is explained in the plan (two experimental effects differ today, section 3).
3. `missing_materials` and `get_material_count` match held items by the name the game shows. All 34 held material types that the current lookup misses are found.
4. A blueprint with no recorded materials is reported as "cost not recorded", never as free.
5. Benchmark questions G1 (FSD Increased Range grade 5) and G2 (Dirty Drives grade 5) stop being "known gaps" and pass, together with six new questions (section 7).
6. A database built from Coriolis alone, with none of the spreadsheet tables, answers every lookup (F8).
7. A game update reaches Ellie through one documented step: `python setup.py` offers to update the Coriolis clone and rebuilds the tables that depend on it (section 6.9).

## 2. Who it is for

Every Ellie user, not only the owner. `Engineers.xlsx` is git-ignored and is not in the repository, so most users have **only Coriolis data**. Coriolis must therefore be complete and tested by itself. The sheet stays an optional extra that the owner keeps by hand.

## 3. What the investigation found (2026-11-06)

All figures were measured on the owner's machine with read-only queries.

| # | Finding | Evidence |
|---|---|---|
| F1 | The FSD/thrusters gap is a lookup gap, not missing data. | `coriolis_blueprints` holds 387 grade rows (81 blueprint definitions), including FSD, thrusters, power plant, power distributor, life support, sensors, shields and armour. The sheet's Core Internal tab has 2 rows. `blueprint_mapping.py` has keywords only for weapons and optional internals, so the Coriolis fallback never matches a core, shield or armour module. |
| F2 | The structured link already exists. | `coriolis-data/modifications/modules.json` lists, per module group (87 groups, 44 of them with blueprints), its blueprints by Coriolis `fdname`. `ingest_ship_modules.py` already loads that into `module_blueprints (grp, fdname)` for the swap simulator. The journal's `BlueprintName` (for example `FSD_LongRange`) is the same `fdname`. |
| F3 | Sheet and Coriolis agree where both answer. | `cross_check.py`: 789 sheet rows; 617 equal Coriolis, 5 differ (4 Mining Laser Long Range rows with Merc Coin entries; Chaff Launcher Ammo Capacity grade 1), 94 have no Coriolis counterpart (newer blueprints such as Plasma Conversion and Overloaded), 73 have no reference (Guardian and unmapped). |
| F4 | Held-material matching is wrong for tiered names. | The journal stores `disruptedwakeechoes`; blueprints say "Atypical Disrupted Wake Echoes". `missing_materials` and `get_material_count` look up `lower(name without spaces)` against the stored key, so they report 0 while the commander holds 64. 34 of 109 held material types (1,826 of 12,990 units) are affected, all of them Encoded or Manufactured items. |
| F5 | Costs are verified by real crafts. | 257 `EngineerCraft` events in the owner's journals. All 48 (blueprint, grade) combinations equal Coriolis's cost once names are compared by the journal's `Name_Localised`. Of 9 experimental effects, 7 are exact. Two differ: Stripped Down (`special_weapon_lightweight`, Coriolis carbon 3, journal carbon 5 in 2 crafts) and Drag Munitions (Coriolis molybdenum 5, journal molybdenum 2 in 1 craft). |
| F6 | "Cost unknown" reads as "free". | `CargoRack_IncreasedCapacity` grade 5 has empty materials in Coriolis; `get_blueprint` returns `found: true, materials: {}`. |
| F8 | Without the sheet, no blueprint lookup works at all. | `get_blueprint` reads the sheet's table first. Where `graded_blueprints` does not exist, the `@_needs_setup` guard answers "database isn't fully set up" for every call, never reaching Coriolis; `get_experimental_effect` does the same with `ungraded_costs`. Verified on a database with Coriolis fully loaded and no sheet tables: all five calls tried (three `get_blueprint`, one `get_experimental_effect`, one `missing_materials`) returned that reply. Since the sheet is not shipped (section 2), this affects every user but the owner. |
| F7 | The sheet carries stray annotations. | Material names such as `Conductive Polymers (0)`, `Flawed Focus Crystals (100)`, `Precipitated Alloys (87)`, the placeholder `Needs personal research`, the typo `Adaptive Encyptors Capture`, and the non-material entries `Merc Coin` and `Cr.`. |

## 4. Scope

In scope: cost lookup for graded blueprints and experimental effects; matching of held materials; honest unknowns; reply shape for 2-4B models; a journal check and a test fixture built from it; tool descriptions in `ellie_chat.py`; README setup note.

Out of scope (part 2 or later): "what can I craft now", shopping lists across several upgrades, the engineer unlock tracker and the engineers-per-blueprint list (the data is in `modules.json`; part 2 uses it), how many rolls a grade needs, the numeric effect of a blueprint (the swap simulator already reads `feature_ranges_json`), suit and on-foot modifications, changes to the system prompt beyond tool descriptions.

## 5. Approaches considered

- **A. Extend the keyword map** (add FSD, thrusters and the rest to `MODULE_TO_CORIOLIS_KEYWORDS`). Smallest change, but it keeps guessing by substring on Coriolis's coarse `applies_to` text ("Weapon" covers many different blueprints), and the swap simulator already moved away from it for that reason. Rejected.
- **B. Resolve through the `module_blueprints` allow-list.** One exact path from module name to group to `fdname` to cost, shared with the swap simulator. No new tables. **Recommended.**
- **C. Build a flat, denormalised cost table at ingest.** Simple queries, but a second copy of data that must be kept in step, and nothing it adds is needed here. Rejected for now.

## 6. Design (approach B)

### 6.1 Module name to blueprints

A module name from the model is resolved to one or more group codes, then to `fdname`s through `module_blueprints`.

- Group names come from the data, not from a hand-kept list: each group's name is its module file in `coriolis-data/modules/` (`frame_shift_drive.json` gives "frame shift drive"; a trailing plural is also accepted singular). They are stored in a new table `module_group_names (grp, name)`, rebuilt with `module_blueprints`, so a group Coriolis adds later is found after the next update with no code change. Comparison ignores case, spaces, hyphens and punctuation. Measured: 43 of the 44 groups with blueprints get a name this way; the 44th, ship armour (`bh`), has no module file.
- A short hand-kept synonym table in `blueprint_mapping.py` covers what the files cannot: abbreviations (FSD, AFMU, HRP, DSS, ECM, KWS), "armour" and "bulkheads", "engines" and "main engines", "manifest scanner", "distributor". The friendly names the swap simulator already uses (`MODULE_GROUP_TO_GRP`) and the raw group code are accepted too.
- Several groups may match one name (shield generator, bi-weave, prismatic). Their blueprints are merged and de-duplicated by `fdname`.
- A name that matches nothing returns the same kind of reply as the swap tool's misses: it says it does not know the module and lists a few real names.

### 6.2 Blueprint name to one `fdname`

The existing forgiving matcher (`_blueprint_name_matches`, used by the swap simulator) does the matching over the allowed blueprints' `upgrade_name`. It gets one more rule, tried only when its four existing rules find nothing, so nothing that matches today changes: a blueprint matches when **all the words of its Coriolis name, or of the part of its `fdname` after the underscore, appear among the words typed**; the match with the most words wins. In-game names such as "Increased FSD Range", "Dirty Drive Tuning", "Faster FSD Boot Sequence" and "Low Emissions Power Plant" resolve by this rule with no table. Tested on the real data: all 558 combinations of group, blueprint and typing style (upgrade name, upgrade name plus module name, `fdname`) resolve to exactly the right blueprint, and 17 of 19 in-game style phrases resolved; of the other two, one ("Drive Strengthening", the in-game name for the thrusters' "Reinforced") needs an alias, and the other was a phrase I made up.

- A short alias table keyed by `fdname` handles names that share no words with Coriolis's. An alias ships only with a source: the owner confirms it in the game, or it is used in the sheet. A wrong alias costs a miss, never a wrong cost, because a miss lists the real names.
- The synonym and alias tables live in `blueprint_mapping.py`, which replaces the keyword map. `cross_check.py` switches to the same resolver so its report covers core modules too.

### 6.3 Cost lookup and source precedence

Unchanged precedence: the sheet's row first when it has the exact module, upgrade and grade, otherwise Coriolis. F3 shows they agree on 99% of overlapping rows, so the order rarely matters, and keeping it respects the owner's earlier decision. Coriolis answers every case the sheet does not cover, which is what other users depend on.

The sheet is optional at every step: if its tables do not exist, the lookup goes straight to Coriolis instead of answering "not set up" (F8). A sheet row is found by the module as typed or by any sheet module name that resolves to the same group, and by the same forgiving blueprint-name matching.

Each reply carries `source` ("excel" or "coriolis"). The five known conflicts (F3) stay with the sheet and are listed in `docs/blueprint-data.md` for the owner to decide, not changed silently.

### 6.4 Honest unknowns

- Empty materials in the chosen record: reply `found: true, cost_known: false`, with a summary that says "no materials recorded for this upgrade". `missing_materials` does not report "nothing missing" for it.
- Grade not offered: "X offers grades 1-4", listing the grades Coriolis has.
- Blueprint that exists but is not allowed for this module: the reply says which module groups it does apply to.

### 6.5 Matching held materials

One helper, `_held_count(conn, name)`, finds a row in `materials_inventory` by either the stored key or the display name (both compared with spaces and hyphens removed, lower-case). `missing_materials` and `get_material_count` both use it. No schema change: `display_name` already holds the game's name for every row (0 rows without one). An item with no row has count 0, which is correct.

### 6.6 Experimental effects

`get_experimental_effect` gains the same module resolution as 6.1 and checks that the effect applies to that module group using the `specials` list in `modules.json`, loaded into a second new table `module_specials (grp, edname)` that is rebuilt with `module_blueprints`. Today "Mass Manager on thrusters" returns a cost although the effect cannot be applied there. Misses list the effects the module does offer.

### 6.7 Reply shape for small models

Following the labelled-result approach used for the loadout tools, each reply keeps its existing fields (so `missing_materials` and current tests keep working) and adds a plain `summary` string and a `materials_per: "roll"` label. Costs are per roll, which matches what one `EngineerCraft` event consumed (F5). Example:

> Frame Shift Drive, Increased range, grade 5, per roll: 1 Arsenic, 1 Chemical Manipulators, 1 Datamined Wake Exceptions.

`missing_materials` summarises as "Have everything" or "Short by: 2 Chemical Manipulators (have 0)". Tool descriptions in `ellie_chat.py` use core-module examples and say the cost is per roll.

### 6.8 Sheet hygiene

`ingest_excel.py` strips a trailing `(n)` annotation from material names and reports the placeholder, the typo and the non-material entries (`Merc Coin`, `Cr.`) in the QA report. It does not delete or reinterpret them: the owner decides what the sheet should say. Query results never contain an annotation suffix.

### 6.9 Updating the data

Reference data (blueprint costs, ship and module stats, the allow-lists, group names) is rebuilt from the local `coriolis-data` clone; live data (materials, ship, fuel) is never touched by a rebuild. After a game update, Coriolis's maintainers update their repository; the user then runs `python setup.py` again.

- Today `setup.py` re-ingests the existing clone but never updates it. It will offer to update the clone first (`git pull --ff-only`), and when the clone changed or was re-ingested it rebuilds the ship, module, allow-list and name tables without asking again, so the tables never disagree with each other.
- The ship-modules step treats the two new tables like the existing three: empty or missing means "re-ingest", so an install from before this change repairs itself on the next `python setup.py`.
- `docs/blueprint-data.md` explains the sources, the precedence, the known sheet-versus-Coriolis conflicts and how to update, in plain words.

### 6.10 Journal check

- `verify_blueprints.py` (next to `cross_check.py`): reads the player's journals, replays every `EngineerCraft`, and compares the ingredients consumed with what Ellie returns for that blueprint, grade or experimental effect. It prints matches, mismatches and unknown blueprints. Anyone can run it to find a wrong cost and report it.
- `tests/fixtures/observed_crafts.json`: the 48 grade combinations and 9 experimental effects, as `fdname`/`edname`, grade and the consumed ingredients by display name. It holds game facts only, no commander data. The tests that use it skip without `coriolis-data`, like the other data-dependent tests.

## 7. Testing and the quality gate

- **Reachability.** Enumerate every blueprint and grade in `modules.json`; each must be found by a friendly module name and by its Coriolis and alias names, with materials equal to Coriolis's. Group codes with no blueprints are listed, not silently skipped.
- **Journal fixture.** All 48 grade combinations and the experimental effects equal the fixture, except entries recorded in the plan as explained (the two in F5).
- **Held materials.** The 34 affected names resolve; an unheld item returns 0; a display name and a stored key both work.
- **Coriolis only.** On a database that has Coriolis, ship and module tables but none of the sheet tables, every lookup above works.
- **Honest unknowns.** The Cargo Rack case returns `cost_known: false`; an out-of-range grade lists the offered grades; a wrong-module blueprint names the right groups.
- **Experimental applicability.** Mass Manager on thrusters is refused with the modules it applies to.
- **Existing tests** stay green, including the connection-closing and one-snapshot tests that already cover these tools.
- **Benchmark** (run locally, no Claude usage): G1 and G2 become category D lookups with answer keys computed live from the tool. Six new questions: lookups for power plant, power distributor, life support and sensors, and two "what am I missing" questions (FSD Increased Range grade 3, Dirty Drives grade 2) whose keys are computed from the live inventory. With `bonsai-2:4b`, each new question must pass in at least 2 of 3 trials, the same bar as the other lookups.
- **Review sizing.** One implementer and one reviewer per task. No multi-agent review without the owner's agreement on cost.

## 8. Risks and open decisions

1. **Setup must be re-run.** Lookups now depend on `module_blueprints`, `module_group_names` and `module_specials`, built by the ship-modules step of `python setup.py`. A user who has only the older tables gets the existing "run `python setup.py`" reply, and that step now detects missing tables and rebuilds them. The README says so.
2. **The two differing experimental effects (F5).** The plan's first task re-reads those journal events (including whether the module type matters). If a difference is confirmed, the choice is between a small committed corrections file with its evidence, or reporting it upstream to Coriolis. Recommendation: a corrections file now, upstream report in parallel.
3. **The five sheet-vs-Coriolis conflicts (F3).** Left as they are, listed for the owner.
4. **In-game names.** Aliases are the least certain part. The plan's first task lists every Coriolis blueprint and its candidate in-game name, and only sourced ones ship.
5. **Per-roll cost.** Part 1 states costs per roll and says so. How many rolls a grade needs is a game-mechanics question that part 2 must settle before it can total a shopping list.

## 9. Files touched (expected)

`query_functions.py` (resolver, `_held_count`, replies), `blueprint_mapping.py` (synonyms, aliases, name cleaning; the keyword map is removed), `ingest_ship_modules.py` (the two new tables), `setup.py` (update step, new tables in the completeness check), `ellie_chat.py` (tool descriptions), `ingest_excel.py` (annotation stripping), `cross_check.py` (same resolver), new `verify_blueprints.py`, new `docs/blueprint-data.md`, new `tests/fixtures/observed_crafts.json`, new and updated tests, `README.md` (setup note, data sources). Two small new tables (`module_group_names`, `module_specials`); no change to the swap simulator or the journal watcher.
