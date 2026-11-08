# Engineering depth, part 2: plan an upgrade

Date: 2026-11-08. Status: draft for the owner's review.
Parent: roadmap spec `2026-11-03-ellie-roadmap-design.md`, section 8, item 1, second half. Part 1 (`2026-11-06-engineering-blueprint-data-design.md`) made per-roll costs correct and shipped on 2026-11-08. Part 2 has three features (plan an upgrade, "what can I craft now", an engineer unlock tracker). **This spec covers only the first, chosen by the owner on 2026-11-08.** The other two keep their place on the roadmap and get their own specs.

## 1. Purpose and success

A commander asks "what do I need to take my FSD to Increased Range grade 5?" and gets one correct, sourced plan: how many rolls each grade takes, the materials for them, what is held and what is short, and which engineers can do it and where the commander stands with each. The answer never states a number that did not come from a table, and says plainly what it cannot know.

Done when all of these hold, each pinned by a test (section 7):

1. For every blueprint and every target grade in Coriolis, `plan_upgrade` returns a plan whose per-grade materials equal Coriolis's per-roll cost times the rolls, with totals that equal the sum of the steps.
2. The rolls per grade follow the engineer's rank (section 3, F1) and are read from a data file that carries its own evidence; a script regenerates the file from any commander's journal.
3. Engineers come from Coriolis; the commander's status and rank come from the journal; the plan works on a database with none of the spreadsheet tables.
4. The starting grade is the fitted module's current grade when it can be known, and the answer says where it came from.
5. Every limit gets a plain statement instead of a guess: no unlocked engineer offers the grade, the engineer's rank is below the target, a cost is not recorded, the module is not fitted or several are.
6. New benchmark questions on plans pass with the default model at the same bar as the other lookups (at least 2 of 3 trials each).

## 2. Who it is for

Every Ellie user. Engineer locations and unlock requirements live only in the owner's spreadsheet, which is not shipped, so this feature uses only Coriolis and the journal.

## 3. What the investigation found (2026-11-08)

Read-only queries on the owner's journals (574 files, 257 `EngineerCraft` events) and database.

| # | Finding | Evidence |
|---|---|---|
| F1 | **Rolls per grade depend on the engineer's rank.** At rank 5, grade N takes N rolls. Below rank 5, grades 1 to 4 took about 4 rolls each. | Finished grades (a higher grade followed), grouped by the engineer's rank at the time (tracked from `EngineerProgress` events). Rank 5: grade 1: 1 roll (6 of 6), grade 2: 2 (6 of 6), grade 3: 3 (4 of 5, once 2), grade 4: 4 (4 of 5, once 2). Below rank 5: grade 1: 4 (5 of 9; also 3 and 5), grade 2: 4 (7 of 7), grade 3: 4 (6 of 6), grade 4: 4 (6 of 6). The owner's own rule ("n+2, grade 1 = 4" below rank 5) fits grades 1 and 2 and over-counts grades 3 and 4 (5 and 6 against 4 observed). Grade 5 appears only as the last step of a run and was 5 rolls in 5 of the 6 runs that reached it (one stopped after 3). **The owner confirms the rank-5 rule for every grade, grade 5 included: an engineer at rank 5 makes every upgrade N rolls for grade N.** The below-rank-5 figures are observation only. |
| F2 | **A grade is only ever crafted at or below the engineer's rank.** | 246 of 246 grade crafts with a known rank had grade <= rank. |
| F3 | **The rank is in the journal.** | `engineer_progress` holds the stage (Known, Invited, Unlocked) and, once unlocked, the rank 1-5. The owner has 6 of 19 known engineers unlocked. |
| F4 | **Coriolis lists the engineers per blueprint per grade.** 26 ship-module engineers. For all 179 blueprints that have both a grade 1 and a grade 5, at least one engineer offers both, so one engineer can carry a whole path. Of the 180 blueprints that reach grade 5, 179 have an engineer at grade 5 (142 have two, 18 three, 10 six and 9 one); `CargoRack_IncreasedCapacity` grade 5 has none. | `modifications/modules.json`. |
| F5 | **Engineer names disagree in two places.** Coriolis writes "Felicty Farseer"; the owner's sheet writes "Coloner Bris Dekker". The journal writes Tod 'The Blaster' McQuinn with single quotes, Coriolis with double quotes (equal once punctuation is ignored). The journal also lists Odyssey suit engineers that Coriolis rightly omits. | Names compared across Coriolis, journal and sheet. |
| F6 | **The loadout already records each fitted module's engineering.** `current_loadout_modules` has `engineering_blueprint` (the Coriolis `fdname`), `engineering_level`, `engineering_experimental`. | Table columns; this is the starting grade. |
| F7 | **Per-roll costs and held-material matching are already correct** (part 1: 48 of 48 grade combinations equal the owner's real crafts). | Part 1. |

## 4. Scope

In scope: one new tool, `plan_upgrade`, with the rolls data file, the script that regenerates it, engineer-name aliases, the tool description for the model, and the tests and benchmark questions that go with them.

Out of scope (later specs): "what can I craft now", the engineer unlock tracker (where an engineer is and how to unlock them), a saved plan list or combined totals over several upgrades (the owner chose one upgrade per question; a saved list belongs with the Memory work), where to find materials (the Library), the engineer rank-up requirements, and Odyssey suit engineers.

## 5. Approaches considered

- **A. One deterministic tool.** The code does all the maths from Coriolis, the journal and the rolls file; the model chooses the tool and phrases the result. **Recommended.**
- **B. The model chains existing tools** (five `get_blueprint` calls, `missing_materials`, then adds). Rejected: it asks a 2-4B model to do arithmetic, the failure the project exists to avoid.
- **C. Learn the rolls from each player's own journal.** More personal, needs history and more machinery. It can sit on top of A later: the script in 6.3 is the first step.

## 6. Design (approach A)

### 6.1 The tool

`plan_upgrade(module_name, upgrade_name, target_grade, from_grade=None, experimental_effect=None) -> dict`

All arguments are flat strings or integers. The module and the blueprint resolve exactly as in part 1 (`_resolve_module_groups`, the allow-list, the same matcher), so the same names work and the same misses list the real options. The target grade must be 1 to 5 and is refused like any other bad grade. `experimental_effect` is optional and, when given, is resolved and checked against the module like `get_experimental_effect`.

### 6.2 Starting grade

In this order:

1. `from_grade`, if the caller gave one (0 to 4; 0 means an unengineered module).
2. Otherwise, the module on the current ship: exactly one fitted module of the resolved group whose `engineering_blueprint` is this blueprint gives its `engineering_level`.
3. A fitted module of the group with a *different* engineering, or an unengineered one: start from 0 (a new modification replaces the old one) and say so.
4. Several fitted modules of the group (for example weapons), or none, or no loadout captured yet: start from 0 and say so, naming the options when there are several so the commander can answer with `from_grade`.

A target at or below the starting grade returns "already there" with nothing to collect.

The reply always says which rule applied (`start_source`).

### 6.3 Rolls

Rolls per step come from `engineering_rolls.json`, a data file in the repository:

```json
// abridged: the shipped file has every grade
{"basis": "one commander's journal, with the date and the number of crafts counted",
 "rank5":  {"1": {"typical": 1, "up_to": 1, "samples": 6, "source": "rule"}, "2": {"typical": 2, "up_to": 2, "samples": 6, "source": "rule"}},
 "below5": {"1": {"typical": 4, "up_to": 5, "samples": 9, "source": "observed"}, "2": {"typical": 4, "up_to": 4, "samples": 7, "source": "observed"}}}
```

`typical` is the number of rolls the plan uses, `up_to` the largest number seen, `samples` how many finished grades support it, and `source` says where `typical` comes from: `"rule"` for the `rank5` entries (N rolls for grade N, confirmed by the owner and matching the journal) or `"observed"` for the `below5` entries (the most common value seen). Only an `observed` entry with fewer than 5 samples, or whose typical value covers fewer than 3 in 4 of its samples, is flagged as thin evidence in the reply (today that is grade 1 below rank 5, seen as 3, 4 or 5 rolls). There is no `below5` entry for grade 5, because the journal shows no grade above the engineer's rank (F2).

The rank used is that of the chosen engineer (6.4), taken as constant over the plan. Players who roll extra for a better result will use more than `typical`; the reply says so.

`rolls_from_journal.py` (like `verify_blueprints.py`) reads a journal folder, tracks each engineer's rank, finds finished grades, and prints or writes this table. The shipped file comes from the owner's journal. The same script lets anyone check it against their own play and report differences.

### 6.4 Engineers

For the target grade, the candidates are the engineers Coriolis lists for that blueprint at that grade (after aliasing, 6.5). The lists are read from the `blueprint_engineers` table, keyed by module group: Coriolis's lists differ by group (Weapon_LongRange is Broo Tarquin and Mel Brandon for a pulse laser but Tod McQuinn and Marsha Hicks for a multi-cannon), so only the engineers of the module's own group(s) are candidates. For each: the commander's status from `engineer_progress` (Unlocked, Invited, Known, or not met when the journal has no row) and the rank. An engineer is *usable now* when unlocked and their rank is at least the target grade. The *chosen* engineer is the usable one with the highest rank (ties by name); if none is usable, the plan still computes materials, sets the rolls basis to rank 5 (the engineer one will eventually need) and says so.

### 6.5 Names

Engineer names are compared with `squash` (case, spaces and punctuation ignored). A small alias table in `blueprint_mapping.py` fixes the two known misspellings: "Felicty Farseer" to "Felicity Farseer" and "Coloner Bris Dekker" to "Colonel Bris Dekker". A test requires every Coriolis ship-module engineer name to resolve to a name the journal uses, so a future misspelling fails loudly.

### 6.6 The maths

For each grade g from start+1 to target: rolls(g) from 6.3, materials(g) = Coriolis per-roll cost of that blueprint at g (`_coriolis_cost`) times rolls. Totals add the steps (typical) and the same with `up_to` rolls. An optional experimental effect adds its single application cost once. Held amounts use `_held_count`, so tiered names match. The reply gives, per material: needed (typical), needed (up to), held, short by.

### 6.7 Reply shape

Found: `{"found": true, "module", "upgrade", "from_grade", "start_source", "target_grade", "rolls_basis", "steps": [{"grade", "rolls", "rolls_up_to", "per_roll": {...}}], "totals": {...}, "totals_up_to": {...}, "short": {name: {"need", "have", "short_by"}}, "engineers": [{"name", "status", "rank", "usable"}], "chosen_engineer", "thin_evidence": bool, "notes": [..], "summary"}`. Misses use the existing shape (`found: false`, `reason`, `available_upgrades`/`available_grades`).

The `summary` is a short paragraph the 2-4B model can read out. Format example using the owner's data (FSD, grade 3 to 5, Felicity Farseer at rank 5, per-roll costs from Coriolis; the bracketed part is filled from the inventory):

> Frame Shift Drive, Increased range, grade 3 to 5 (your drive is at grade 3). Felicity Farseer (unlocked, rank 5) can do it. Rolls at rank 5: grade 4: 4, grade 5: 5. You need 4 Chemical Distillery, 4 Eccentric Hyperspace Trajectories, 4 Manganese, 5 Arsenic, 5 Chemical Manipulators, 5 Datamined Wake Exceptions. You are short by: [the shortfall from the commander's inventory, or "nothing"]. Extra rolls for a better result will cost more.

### 6.8 Limits stated plainly

| Situation | Reply |
|---|---|
| No engineer offers the blueprint at the target grade | "No engineer in Coriolis's data offers it at grade N." |
| Engineers offer it but none is unlocked | Lists them with status; plan computed at rank 5 numbers; "none of them is unlocked yet" |
| The best unlocked engineer's rank is below the target | "Felicity is rank 3; grade 4 needs rank 4 or 5" and the materials for the steps up to their rank are still listed |
| A step has no recorded cost | That step is marked cost unknown and left out of the totals, with a note |
| An `observed` rolls entry is thin | `thin_evidence` true and a sentence saying how few crafts support it or how much they vary |
| No loadout, or several fitted modules | Start from 0 and say what to do (6.2) |

### 6.9 Tool description for the model

Short, no category words (part 1's benchmark showed small models copy them): "Plan taking one specific module from one grade up to a higher grade (1-5): rolls per grade, total materials, what is short, and which engineers can do it. Use this, not a single-roll lookup, when the commander asks what it takes to get a module to a grade. Also use it when the commander asks which engineers can take a module to a grade." Arguments are described with real examples of module names and blueprint names; `module_name` reads "Module name only, without any blueprint word" (on `plan_upgrade`, `get_blueprint` and `missing_materials`).

*Tuned on the local benchmark (`bonsai-2:4b`), 2026-11-08.* The first wording ("Plan an engineering upgrade of one specific module up to a grade") lost "what do I need to take my FSD to Increased Range grade 5?" to `missing_materials` in every trial. That tool answers for one roll only ("you have everything"), which is a wrong answer to a multi-grade question. Three changes fixed it. Leading with the multi-grade scope and the "not a single-roll lookup" sentence routes the planning questions. The engineers sentence routes "which engineers can take my life support to Lightweight grade 2". The "module name only" wording stops the model fusing the blueprint into the module name ("Armoured power plant"), a failure the extra tool text had made more frequent for the existing single-roll lookups (D15 fell from 13 of 15 to 4 of 15 before it). With all three, over 12 trials each, the planning questions pass 12, 11, 10 and 11 times (P1, P3, P5, P6) and D15 passes 10. The older tools' own descriptions are unchanged: a cross-reference sentence added to them did not help once the planner's wording was right, and it cost D15 trials. The benchmark is small and noisy; these are measurements, not guarantees. A question phrased like a single-roll lookup ("what would grade 5 Armoured take?") is ambiguous by design and is not a planning question, so the planner questions in the benchmark say "get to" or "from grade N to grade M".

## 7. Testing and quality gate

- **Maths.** Totals equal the sum of the steps for every blueprint and every start/target pair in Coriolis (the reachability pattern from part 1, run through `plan_upgrade`).
- **Rolls.** Rank 5 versus below 5 (rank 5 is N rolls for grade N, grade 5 included); rolls file shape and `source`; an observed entry is flagged when thin and a rule entry never is; `rolls_from_journal.py` reproduces the shipped table from a committed fixture of runs (`tests/fixtures/observed_rolls.json`, game facts only).
- **Starting grade.** Each rule in 6.2 with a loadout fixture (fitted and engineered, fitted with different engineering, several fitted, none), plus `from_grade`.
- **Engineers.** Status and rank mapping, usable versus not, the chosen engineer, the alias table, every Coriolis engineer resolving to a journal name.
- **Limits.** One test per row of 6.8.
- **No sheet.** The same on a database with none of the spreadsheet tables.
- **Existing tests** stay green, including the connection-closing and one-snapshot tests, which get `plan_upgrade` added.
- **Benchmark** (local, free): about six new questions, for example "what do I need to take my FSD to Increased Range grade 5?" and "plan Dirty Drives up to grade 3 from grade 1", with answer keys computed live from the tool. Each must pass at least 2 of 3 trials with `bonsai-2:4b`.
- **Review sizing.** Native execution as in part 1, one fresh reviewer at the end.

## 8. Risks and open decisions

1. **The below-rank-5 rolls are one commander's observations.** The rank-5 rule is confirmed; the rest is labelled as observed, flagged where it varies (grade 1), regenerable from any journal, and improvable by sharing results. If the community shows a different pattern, the file changes, not the code.
2. **Rank changes during a plan.** Engineers rank up as you craft with them; the plan uses the current rank for every step and says so. Treated as a stated simplification.
3. **Extra rolls.** The plan shows the usual number and "up to"; it cannot know how many extra the player will roll for quality.
4. **Switching engineers mid-path** is allowed by the game but not modelled: one chosen engineer carries the whole path (F4 shows one always exists).
5. **Unlock requirements are not modelled** (part of the unlock tracker). When no engineer is unlocked the plan says who offers it and their status, nothing more.
6. **A module the commander has not fitted.** The tool still plans from grade 0; whether the module fits the ship is the swap simulator's job.

## 9. Files touched (expected)

`query_functions.py` (`plan_upgrade` and helpers), `blueprint_mapping.py` (engineer aliases), new `engineering_rolls.json`, new `rolls_from_journal.py`, `ellie_chat.py` (tool definition and dispatch), new and updated tests, new `tests/fixtures/observed_rolls.json`, `README.md` (tools list), `docs/blueprint-data.md` (a short section on plans and the rolls table). One small new table, `blueprint_engineers (grp, fdname, grade, engineer)`, built from `modules.json` with the allow-lists (the engineer lists Coriolis publishes have to be stored somewhere, and per module group, because they differ by group); no change to the journal watcher or the swap simulator.
