# Engineering depth part 2: plan an upgrade. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new tool, `plan_upgrade`, answers "what do I need to take this module to grade N?" with rolls per grade, total materials, what is short, and which engineers can do it, from Coriolis, the journal and a small rolls data file.

**Architecture:** One deterministic function in `query_functions.py` does all the maths. It reuses part 1's module and blueprint resolution (refactored into one shared helper), adds a small `blueprint_engineers` table built from Coriolis, reads each engineer's status and rank from the journal-fed `engineer_progress` table, and takes rolls per grade from `engineering_rolls.json`, which a script regenerates from any journal.

**Tech Stack:** Python 3, stdlib `sqlite3`, `pytest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-08-engineering-upgrade-plan-design.md` (read it first; this plan implements it). One amendment, made in Task 1: the spec's section 9 said "no new tables", but the engineer lists Coriolis publishes must be stored somewhere, so there is one small new table, `blueprint_engineers`.

## Global Constraints

Copied from the spec; every task's requirements include these.

- Rolls: an engineer at **rank 5** takes **N rolls for grade N** (grade 5 included, a confirmed rule); below rank 5, grades 1 to 4 take about 4 rolls each (observed, grade 1 varies 3 to 5). A grade is only ever crafted at or below the engineer's rank. The plan uses the chosen engineer's **current** rank for every step.
- Costs are **per roll** (part 1); an experimental effect is added **once**. The reply never states a number that did not come from a table, and says plainly what it cannot know (spec 6.8).
- Engineers come from Coriolis; status and rank from the journal's `engineer_progress` (stages `Unlocked`, `Invited`, `Known`; rank only once unlocked). Engineer names are compared with `engineer_key` (case, spaces and punctuation ignored, known misspellings corrected). The plan works on a database with **none** of the spreadsheet tables; the sheet's per-grade cost, when it has one, wins as in part 1.
- `plan_upgrade` takes flat arguments only (strings and integers), returns a dict, never raises on bad model input, keeps `@_needs_setup` directly above the `def`, and opens one connection through `_connect()` closed in `try/finally` (the optional experimental effect is looked up first by `get_experimental_effect`, which uses its own).
- No new dependencies; `requirements.txt` is unchanged. No change to the journal watcher or the swap simulator. The system prompt is not changed.
- Tests that read the cloned Coriolis data are listed in `NEEDS_CORIOLIS_DATA` in `tests/conftest.py` so they skip, with a reason, on a fresh clone. **A test file that needs no data must not contain the literal text of that clone's folder name or `ism.ingest_all`**: `tests/test_conftest_skips.py` greps for them.
- Run tests from the repository root with `python -m pytest`. The baseline before this work is **668 passed**.
- Every commit message ends with a blank line and `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. **Do not push** and do not change Git credentials.
- Windows: when a script prints text that may be non-ASCII, set `PYTHONIOENCODING=utf-8`.

## Setup before Task 1

Work on a branch in the main checkout: `git checkout -b engineering-upgrade-plan`. Confirm the baseline: `python -m pytest tests -q` prints `668 passed`. The Coriolis clone must exist (run `python setup.py` once if it does not).

**Data facts the tests rely on** (checked against the clone on 2026-11-08; if one has moved after a Coriolis refresh, the test names it, update the number, not the code):
- `modifications/modules.json` has 1,694 distinct (blueprint, grade, engineer) rows and 26 distinct engineer strings: the 25 ship engineers plus Coriolis's own misspelling "Felicty Farseer".
- `coriolis_blueprints` has 387 rows, and 908 (group, blueprint, grade) combinations are reachable through `module_blueprints`.
- The only blueprint-grade with no engineer is `CargoRack_IncreasedCapacity` grade 5, and it is also the only grade that blueprint has, with an empty cost (`{}`).
- Mass Manager (`special_fsd_heavy`, group `fsd` only) costs 5 Atypical Disrupted Wake Echoes, 3 Galvanising Alloys and 1 Eccentric Hyperspace Trajectories per application.
- `FSD_LongRange` is offered at grade 5 by Elvira Martuuk, Felicity Farseer and Mel Brandon; `Engine_Dirty` at grade 3 by Chloe Sedesi, "Felicty Farseer", Mel Brandon and Professor Palin.

## Review Focus

Failure modes the spec implies that a person using Ellie will hit. Each has a test in the task that owns the code.

1. **A grade typed as text or out of range** ("five", `None`, 0, 6; a starting grade of "x", 7, -1): a plain "must be a number from 1 to 5" (or 0 to 4) reply, never an exception. Tests: Task 6.
2. **An engineer table with no rows, or with rows that have no rank** (a fresh install; engineers only Known or Invited; an engineer Unlocked whose rank the journal has not reported, which the watcher stores as NULL): the plan is still computed, assumes a rank-5 engineer, and says so; no crash. Tests: Tasks 2 and 6.
3. **Several fitted modules of the group, a fitted module with a different modification, no loadout yet, or old installs without the loadout tables**: the plan starts from an unengineered module and says what to do. Tests: Task 5.
4. **A blueprint Coriolis lists with no engineer or no recorded cost** (Cargo Rack Expanded Capacity grade 5): the plan says "no engineer offers it" and "no recorded materials", leaves the unknown step out of the totals, never invents. Test: Task 6.
5. **Engineer name spelling** (Coriolis writes "Felicty Farseer"; the journal writes quotes differently): matched by `engineer_key`, so a misspelling cannot hide an engineer or double-list one. Tests: Task 2.

---

### Task 1: Engineer lists in the database

**Files:**
- Modify: `ingest_ship_modules.py` (`build_schema`, new `build_blueprint_engineers`, `ingest_all`)
- Modify: `setup.py` (`STEP_TABLES`, `setup_ship_modules`)
- Modify: `tests/test_ingest_ship_modules.py`, `tests/test_setup_ship_modules.py`
- Modify: `docs/superpowers/specs/2026-11-08-engineering-upgrade-plan-design.md` (the amendment)

**Interfaces:**
- Produces: table `blueprint_engineers (fdname TEXT, grade INTEGER, engineer TEXT, PRIMARY KEY (fdname, grade, engineer))`, names stored exactly as Coriolis spells them; `ism.build_blueprint_engineers(conn, repo_path, commit=True) -> int`. `ism.ingest_all` still returns its 3-tuple and also fills the table.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest_ship_modules.py`:

```python
def test_blueprint_engineers_list_who_offers_what_at_each_grade(tmp_path):
    conn = _fresh_db(tmp_path)
    count = ism.build_blueprint_engineers(conn, str(REPO))
    conn.commit()
    assert count > 1500                                              # 1,694 on the data of 2026-11-08

    def engineers(fdname, grade):
        return {r[0] for r in conn.execute("SELECT engineer FROM blueprint_engineers WHERE fdname = ? AND grade = ?", (fdname, grade))}

    assert engineers("FSD_LongRange", 5) == {"Elvira Martuuk", "Felicity Farseer", "Mel Brandon"}
    assert "Colonel Bris Dekker" in engineers("FSD_LongRange", 1) and "Colonel Bris Dekker" not in engineers("FSD_LongRange", 4)
    assert "Felicty Farseer" in engineers("Engine_Dirty", 3)          # Coriolis's own misspelling is stored as published


def test_ingest_all_fills_the_engineer_table_and_a_rerun_does_not_duplicate(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "engineers.db"))
    ism.ingest_all(conn, str(REPO))
    first = conn.execute("SELECT COUNT(*) FROM blueprint_engineers").fetchone()[0]
    assert first > 1500
    ism.ingest_all(conn, str(REPO))
    assert conn.execute("SELECT COUNT(*) FROM blueprint_engineers").fetchone()[0] == first
    conn.close()
```

In `tests/test_setup_ship_modules.py` change the `TABLES` line to
`TABLES = ("ship_stats", "module_stats", "module_blueprints", "module_group_names", "module_specials", "blueprint_engineers")`
and the first docstring line's word "five" to "six". Then append:

```python
def test_an_install_from_before_the_engineer_table_is_repaired_without_asking(setup_db, monkeypatch, capsys):
    conn = sqlite3.connect(str(setup_db))
    conn.execute("DROP TABLE blueprint_engineers")
    conn.commit()
    conn.close()
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "blueprint_engineers is empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert _count(setup_db, "blueprint_engineers") > 0
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py -q`
Expected: FAIL (`AttributeError: ... build_blueprint_engineers`; the setup tests fail on the missing table).

- [ ] **Step 3: Implement the ingest**

In `ingest_ship_modules.py`, in `build_schema`, directly before the final `if commit: conn.commit()`, add:

```python
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blueprint_engineers (
            fdname TEXT, grade INTEGER, engineer TEXT, PRIMARY KEY (fdname, grade, engineer)
        )
        """
    )
```

After `build_module_specials` add:

```python
def build_blueprint_engineers(conn, repo_path, commit=True):
    """Which engineers offer each blueprint at each grade, from modifications/modules.json (the same
    file as the allow-lists). Names are stored exactly as Coriolis spells them -- including its own
    misspellings, which the lookups correct -- so the table mirrors the source. Clears and rebuilds."""
    data = json.load(open(Path(repo_path) / "modifications" / "modules.json", encoding="utf-8"))
    conn.execute("DELETE FROM blueprint_engineers")
    count = 0
    for entry in data.values():
        for fdname, blueprint in (entry.get("blueprints") or {}).items():
            for grade, detail in blueprint.get("grades", {}).items():
                for engineer in detail.get("engineers", []):
                    count += conn.execute("INSERT OR IGNORE INTO blueprint_engineers VALUES (?,?,?)",
                                          (fdname, int(grade), engineer)).rowcount
    if commit:
        conn.commit()
    return count
```

In `ingest_all`, after the `build_module_specials(conn, repo_path, commit=False)` line add `build_blueprint_engineers(conn, repo_path, commit=False)`, and extend its docstring's table list with `blueprint_engineers`.

In `setup.py`: in `STEP_TABLES` add `"blueprint_engineers"` to the ship/module list, and in `setup_ship_modules` change the `tables = (...)` tuple to end with `"module_specials", "blueprint_engineers")`.

- [ ] **Step 4: Amend the spec**

In `docs/superpowers/specs/2026-11-08-engineering-upgrade-plan-design.md`, section 9: replace `No new tables; no change to the journal watcher or the swap simulator.` with `One small new table, \`blueprint_engineers (fdname, grade, engineer)\`, built from \`modules.json\` with the allow-lists (the engineer lists Coriolis publishes have to be stored somewhere); no change to the journal watcher or the swap simulator.` In section 6.4, after the first sentence, add: `The lists are read from that table.`

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py -q` then `python -m pytest tests -q`. Expected: all pass (668 plus the new tests).

- [ ] **Step 6: Commit**

```bash
git add ingest_ship_modules.py setup.py tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py docs/superpowers/specs/2026-11-08-engineering-upgrade-plan-design.md
git commit -m "Store which engineers offer each blueprint at each grade" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Engineer names, status and choice

**Files:**
- Modify: `blueprint_mapping.py` (`ENGINEER_ALIASES`, `engineer_key`)
- Modify: `query_functions.py` (`_commander_engineers`, `_engineers_offering`, `_choose_engineer`)
- Modify: `tests/test_blueprint_mapping.py`, `tests/conftest.py`
- Create: `tests/test_plan_upgrade.py`

**Interfaces:**
- Consumes: Task 1's `blueprint_engineers`; the `coriolis_db` fixture (it now also builds `blueprint_engineers` through `ism.ingest_all`).
- Produces:
  - `engineer_key(name) -> str`; `ENGINEER_ALIASES: dict[str, str]` (squashed misspelling -> correct name).
  - `qf._engineers_offering(conn, fdname, grade) -> list[dict]`, each `{"name": str, "status": "unlocked"|"invited"|"known"|"not met", "rank": int|None, "usable": bool}`, sorted by name, one entry per engineer (misspellings merged). `usable` means unlocked with rank >= `grade`.
  - `qf._choose_engineer(offers, target) -> (dict|None, str)` with mode `"usable"` (the usable engineer with the highest rank, ties by name), `"low_rank"` (no one usable; the unlocked engineer with the highest known rank), `"rank_unknown"` (no one usable or ranked, but someone is unlocked and the journal has not given their rank: `rank_value` is NULL until the game reports it), `"none_unlocked"` (offers exist, none unlocked), `"no_engineer"` (empty list).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_blueprint_mapping.py`:

```python
def test_engineer_key_ignores_spelling_noise_and_corrects_the_known_misspellings():
    assert bm.engineer_key("Felicty Farseer") == bm.engineer_key("Felicity Farseer") == "felicityfarseer"
    assert bm.engineer_key("Coloner Bris Dekker") == bm.engineer_key("Colonel Bris Dekker")
    assert bm.engineer_key("Tod 'The Blaster' McQuinn") == bm.engineer_key('Tod "The Blaster" McQuinn')
    assert bm.engineer_key("  elvira   MARTUUK ") == "elviramartuuk"
    assert bm.engineer_key(None) == "" and bm.engineer_key("") == ""
```

Add `"test_plan_upgrade.py"` to `NEEDS_CORIOLIS_DATA` in `tests/conftest.py`. Create `tests/test_plan_upgrade.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm
import query_functions as qf

# The 25 ship-module engineers, spelled the way the game's journal spells them.
JOURNAL_SHIP_ENGINEERS = [
    "Bill Turner", "Broo Tarquin", "Chloe Sedesi", "Colonel Bris Dekker", "Didi Vatermann", "Elvira Martuuk",
    "Etienne Dorn", "Felicity Farseer", "Hera Tani", "Juri Ishmaak", "Lei Cheung", "Liz Ryder", "Lori Jameson",
    "Marco Qwent", "Marsha Hicks", "Mel Brandon", "Petra Olmanova", "Professor Palin", "Ram Tah", "Selene Jean",
    "The Dweller", "The Sarge", "Tiana Fortune", "Tod 'The Blaster' McQuinn", "Zacariah Nemo",
]


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _engineer(path, name, stage, rank=None):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO engineer_progress VALUES (?,?,?,?)", (name, stage, rank, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


def _offers(path, fdname, grade):
    conn = _conn(path)
    try:
        return qf._engineers_offering(conn, fdname, grade)
    finally:
        conn.close()


def test_every_coriolis_engineer_name_resolves_to_a_name_the_journal_uses(coriolis_db):
    conn = _conn(coriolis_db)
    try:
        coriolis = {bm.engineer_key(r[0]) for r in conn.execute("SELECT DISTINCT engineer FROM blueprint_engineers")}
    finally:
        conn.close()
    assert coriolis == {bm.engineer_key(n) for n in JOURNAL_SHIP_ENGINEERS}      # a new misspelling fails here


def test_felicity_is_listed_for_dirty_drives_despite_coriolis_misspelling_her(coriolis_db):
    names = [o["name"] for o in _offers(coriolis_db, "Engine_Dirty", 3)]
    assert "Felicity Farseer" in names and "Felicty Farseer" not in names
    assert names == sorted(names) and len(names) == len(set(names)) == 4


def test_status_and_rank_come_from_the_journal_table(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _engineer(coriolis_db, "Elvira Martuuk", "Invited")
    _engineer(coriolis_db, "Tod 'The Blaster' McQuinn", "Unlocked", 5)           # not an FSD engineer: must not appear
    offers = {o["name"]: o for o in _offers(coriolis_db, "FSD_LongRange", 5)}
    assert offers["Felicity Farseer"] == {"name": "Felicity Farseer", "status": "unlocked", "rank": 5, "usable": True}
    assert offers["Elvira Martuuk"] == {"name": "Elvira Martuuk", "status": "invited", "rank": None, "usable": False}
    assert offers["Mel Brandon"] == {"name": "Mel Brandon", "status": "not met", "rank": None, "usable": False}
    assert "Tod 'The Blaster' McQuinn" not in offers


def test_an_unlocked_engineer_below_the_target_rank_is_not_usable(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 3)
    assert {o["name"]: o["usable"] for o in _offers(coriolis_db, "FSD_LongRange", 3)}["Felicity Farseer"] is True
    assert {o["name"]: o["usable"] for o in _offers(coriolis_db, "FSD_LongRange", 4)}["Felicity Farseer"] is False


def test_an_empty_engineer_table_gives_everyone_as_not_met(coriolis_db):
    offers = _offers(coriolis_db, "FSD_LongRange", 5)
    assert [o["status"] for o in offers] == ["not met"] * 3 and not any(o["usable"] for o in offers)


def test_the_chosen_engineer():
    def offer(name, status, rank, usable):
        return {"name": name, "status": status, "rank": rank, "usable": usable}

    best, mode = qf._choose_engineer([offer("B", "unlocked", 5, True), offer("A", "unlocked", 5, True), offer("C", "unlocked", 3, False)], 5)
    assert (best["name"], mode) == ("A", "usable")                                  # equal rank: first by name
    best, mode = qf._choose_engineer([offer("C", "unlocked", 3, False), offer("D", "unlocked", 2, False), offer("E", "known", None, False)], 5)
    assert (best["name"], mode) == ("C", "low_rank")                                # the best unlocked one, below the target
    best, mode = qf._choose_engineer([offer("F", "unlocked", None, False), offer("C", "unlocked", 3, False)], 5)
    assert (best["name"], mode) == ("C", "low_rank")                                # a known rank beats an unknown one
    best, mode = qf._choose_engineer([offer("F", "unlocked", None, False), offer("E", "known", None, False)], 5)
    assert (best["name"], mode) == ("F", "rank_unknown")                            # unlocked, but the journal gave no rank
    assert qf._choose_engineer([offer("E", "known", None, False)], 5) == (None, "none_unlocked")
    assert qf._choose_engineer([], 5) == (None, "no_engineer")
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest tests/test_blueprint_mapping.py tests/test_plan_upgrade.py -q` — Expected: FAIL (`AttributeError: ... engineer_key`, `_engineers_offering`).

- [ ] **Step 3: Implement**

Append to `blueprint_mapping.py`:

```python


# Engineer names that Coriolis (or the owner's sheet) misspells, keyed by squash(): the journal's spelling wins.
ENGINEER_ALIASES = {
    "felictyfarseer": "Felicity Farseer",
    "colonerbrisdekker": "Colonel Bris Dekker",
}


def engineer_key(name):
    """A comparison key for an engineer's name: case, spaces and punctuation ignored, known misspellings
    corrected ('Felicty Farseer' and 'Felicity Farseer' give the same key)."""
    key = squash(name)
    return squash(ENGINEER_ALIASES.get(key, name))
```

In `query_functions.py` add `ENGINEER_ALIASES, engineer_key` to the `from blueprint_mapping import (...)` list, and add (above `plan_upgrade`'s future home; any spot after `_held_count`):

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_blueprint_mapping.py tests/test_plan_upgrade.py -q` then `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add blueprint_mapping.py query_functions.py tests/conftest.py tests/test_blueprint_mapping.py tests/test_plan_upgrade.py
git commit -m "Engineers per blueprint: names, journal status and rank, and which one to choose" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The rolls table and the script that regenerates it

**Files:**
- Create: `engineering_rolls.py`, `rolls_from_journal.py`, `engineering_rolls.json` (generated), `tests/test_engineering_rolls.py`, `tests/fixtures/observed_rolls.json` (generated)

**Interfaces:**
- Produces:
  - `engineering_rolls.load_rolls(path=None) -> dict` (`{"basis", "rank5", "below5"}`); `engineering_rolls.rolls_for(table, rank5, grade) -> dict|None` returning `{"typical", "up_to", "samples", "source", "thin"}` (plus `"share"` for observed entries), or `None` when the table has no entry (grade 5 below rank 5); `engineering_rolls.is_thin(entry) -> bool`.
  - `rolls_from_journal.read_craft_events(journal_dir)`, `finished_segments(events) -> list[{"rank5": bool, "grade": int, "rolls": int}]`, `build_table(segments, basis) -> dict`, `main(argv=None)`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_engineering_rolls.py`:

```python
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engineering_rolls as er
import rolls_from_journal as rj

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "observed_rolls.json"


def _craft(engineer, module, blueprint, grade, effect=None):
    event = {"event": "EngineerCraft", "Engineer": engineer, "Module": module, "BlueprintName": blueprint, "Level": grade}
    if effect:
        event["ApplyExperimentalEffect"] = effect
    return json.dumps(event)


def _progress(engineer, rank):
    return json.dumps({"event": "EngineerProgress", "Engineer": engineer, "Rank": rank})


def _segments(tmp_path, *lines):
    (tmp_path / "Journal.2026-11-08T000000.01.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rj.finished_segments(rj.read_craft_events(tmp_path))


def test_a_rank_5_engineer_run_gives_n_rolls_per_finished_grade(tmp_path):
    lines = [_progress("Felicity Farseer", 5)] + [_craft("Felicity Farseer", "m1", "FSD_LongRange", g) for g in (1, 2, 2, 3, 3, 3)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 1, "rolls": 1}, {"rank5": True, "grade": 2, "rolls": 2}]
    # grade 3 is the last step and below grade 5, so it may be unfinished: not counted


def test_a_lower_rank_engineer_is_recorded_as_not_rank_5(tmp_path):
    lines = [_progress("Mel Brandon", 3)] + [_craft("Mel Brandon", "m1", "Engine_Dirty", g) for g in (1, 1, 1, 1, 2, 2, 2, 2, 3)]
    assert _segments(tmp_path, *lines) == [{"rank5": False, "grade": 1, "rolls": 4}, {"rank5": False, "grade": 2, "rolls": 4}]


def test_the_last_grade_5_step_counts_because_nothing_can_follow_it(tmp_path):
    lines = [_progress("Felicity Farseer", 5)] + [_craft("Felicity Farseer", "m1", "FSD_LongRange", g) for g in (4, 4, 4, 4, 5, 5, 5, 5, 5)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 4, "rolls": 4}, {"rank5": True, "grade": 5, "rolls": 5}]


def test_experimental_crafts_other_events_and_bad_lines_are_ignored(tmp_path):
    lines = [_progress("Felicity Farseer", 5), "not json", '{"event":"Music"}',
             _craft("Felicity Farseer", "m1", "FSD_LongRange", 1), _craft("Felicity Farseer", "m1", "FSD_LongRange", 1, effect="special_fsd_heavy"),
             _craft("Felicity Farseer", "m1", "FSD_LongRange", 2), _craft("Felicity Farseer", "m1", "FSD_LongRange", 2)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 1, "rolls": 1}]


def test_a_new_module_or_engineer_starts_a_new_run(tmp_path):
    lines = [_progress("Felicity Farseer", 5), _craft("Felicity Farseer", "m1", "FSD_LongRange", 1), _craft("Felicity Farseer", "m2", "FSD_LongRange", 2)]
    assert _segments(tmp_path, *lines) == []                  # grade 1 on m1 was never followed by grade 2 on m1


def test_a_craft_with_no_known_rank_is_skipped(tmp_path):
    lines = [_craft("Nobody", "m1", "X_Y", 1), _craft("Nobody", "m1", "X_Y", 2)]
    assert _segments(tmp_path, *lines) == []


def test_build_table_makes_rank_5_a_rule_and_below_5_the_most_common_value():
    segments = ([{"rank5": True, "grade": 1, "rolls": 1}] * 3 + [{"rank5": True, "grade": 3, "rolls": 2}]
                + [{"rank5": False, "grade": 1, "rolls": r} for r in (3, 4, 4, 4, 5)]
                + [{"rank5": False, "grade": 2, "rolls": 4}] * 6)
    table = rj.build_table(segments, "test basis")
    assert table["basis"] == "test basis"
    assert table["rank5"]["1"] == {"typical": 1, "up_to": 1, "samples": 3, "source": "rule"}
    assert table["rank5"]["3"] == {"typical": 3, "up_to": 3, "samples": 1, "source": "rule"}      # an early finish never lowers the rule
    assert table["rank5"]["5"] == {"typical": 5, "up_to": 5, "samples": 0, "source": "rule"}
    assert table["below5"]["1"] == {"typical": 4, "up_to": 5, "samples": 5, "source": "observed", "share": 0.6}
    assert table["below5"]["2"] == {"typical": 4, "up_to": 4, "samples": 6, "source": "observed", "share": 1.0}
    assert "3" not in table["below5"] and "5" not in table["below5"]


def test_a_tie_between_two_values_takes_the_smaller():
    table = rj.build_table([{"rank5": False, "grade": 1, "rolls": r} for r in (3, 3, 5, 5)], "b")
    assert table["below5"]["1"]["typical"] == 3


def test_only_observed_entries_can_be_thin():
    assert er.is_thin({"source": "rule", "samples": 0}) is False
    assert er.is_thin({"source": "observed", "samples": 4, "share": 1.0}) is True        # too few
    assert er.is_thin({"source": "observed", "samples": 9, "share": 0.56}) is True       # varies too much
    assert er.is_thin({"source": "observed", "samples": 7, "share": 1.0}) is False


def test_rolls_for_picks_the_rank_table_and_has_no_grade_5_below_rank_5():
    table = rj.build_table([{"rank5": False, "grade": 1, "rolls": 4}] * 6, "b")
    assert er.rolls_for(table, True, 5)["typical"] == 5 and er.rolls_for(table, True, 5)["thin"] is False
    assert er.rolls_for(table, False, 1)["typical"] == 4 and er.rolls_for(table, False, 1)["thin"] is False
    assert er.rolls_for(table, False, 5) is None and er.rolls_for(table, False, 2) is None


def test_the_shipped_table_is_exactly_what_the_fixture_of_real_runs_produces():
    segments = json.load(open(FIXTURE, encoding="utf-8"))
    built = rj.build_table(segments, "x")
    shipped = er.load_rolls()
    assert built["rank5"] == shipped["rank5"] and built["below5"] == shipped["below5"]
    assert shipped["basis"].strip()


def test_the_shipped_table_says_what_the_owner_and_the_journal_found():
    shipped = er.load_rolls()
    for grade in range(1, 6):
        assert er.rolls_for(shipped, True, grade)["typical"] == grade                    # N rolls for grade N at rank 5
    assert [er.rolls_for(shipped, False, g)["typical"] for g in (2, 3, 4)] == [4, 4, 4]  # about 4 each below rank 5
    assert er.rolls_for(shipped, False, 1)["thin"] is True                               # grade 1 varies (3, 4 or 5)
    assert er.rolls_for(shipped, False, 2)["thin"] is False
    assert er.rolls_for(shipped, False, 5) is None
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_engineering_rolls.py -q` — Expected: FAIL (`ModuleNotFoundError: engineering_rolls`).

- [ ] **Step 3: Implement** — create `engineering_rolls.py`:

```python
"""Rolls per grade, from engineering_rolls.json.

An engineer at rank 5 makes every upgrade N rolls for grade N (a rule, confirmed by the owner and by the
journal). Below rank 5 the journal shows about 4 rolls each for grades 1 to 4 (observed, one commander;
grade 1 varies between 3 and 5). rolls_from_journal.py regenerates the file from any journal.
"""
import json
from pathlib import Path

ROLLS_PATH = Path(__file__).resolve().parent / "engineering_rolls.json"


def load_rolls(path=None):
    with open(Path(path) if path else ROLLS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def is_thin(entry):
    """Only an OBSERVED entry can be thin: fewer than 5 samples, or its usual value covers fewer than
    3 in 4 of them. A rule entry never is."""
    if entry.get("source") != "observed":
        return False
    return entry["samples"] < 5 or entry.get("share", 1.0) < 0.75


def rolls_for(table, rank5, grade):
    """The entry for one grade at an engineer of rank 5 (rank5=True) or below, with a `thin` flag, or None
    when the table has none (grade 5 below rank 5 does not exist: nobody crafts above their rank)."""
    entry = table["rank5" if rank5 else "below5"].get(str(grade))
    return None if entry is None else {**entry, "thin": is_thin(entry)}
```

Create `rolls_from_journal.py`:

```python
"""
Work out how many rolls each grade takes, from YOUR OWN engineering crafts.

Reads the journal, follows each engineer's rank, and finds the grades you finished (a higher grade of the
same blueprint on the same module followed, or grade 5, which nothing can follow). At an engineer of rank 5
a grade takes N rolls; below rank 5 it takes about 4. Prints the table Ellie plans with.

    python rolls_from_journal.py                          # your journal folder: print the table
    python rolls_from_journal.py "D:\\Journals"            # another folder
    python rolls_from_journal.py --write engineering_rolls.json --write-fixture tests/fixtures/observed_rolls.json
"""
import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

from verify_blueprints import DEFAULT_JOURNAL_DIR


def read_craft_events(journal_dir):
    """Every non-experimental EngineerCraft, in journal order, with the engineer's rank at that moment
    (tracked from EngineerProgress events; None when the journal has not told us yet)."""
    rank, events = {}, []
    for path in sorted(Path(journal_dir).glob("Journal.*.log")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"EngineerProgress"' not in line and '"EngineerCraft"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "EngineerProgress":
                    for entry in (event.get("Engineers") or [event]):
                        if entry.get("Engineer") and "Rank" in entry:
                            rank[entry["Engineer"]] = entry["Rank"]
                elif event.get("event") == "EngineerCraft" and not event.get("ApplyExperimentalEffect"):
                    events.append({"engineer": event.get("Engineer"), "module": event.get("Module"),
                                   "blueprint": event.get("BlueprintName"), "grade": event.get("Level"),
                                   "rank": rank.get(event.get("Engineer"))})
    return events


def finished_segments(events):
    """[{"rank5": bool, "grade": int, "rolls": int}] for every grade the commander finished. A run is
    consecutive crafts of one blueprint on one module with one engineer; a grade is finished when the
    next step of the run is the next grade up, or when it is grade 5 and the run ends there."""
    runs, current = [], None
    for event in events:
        key = (event["module"], event["blueprint"], event["engineer"])
        if current and current["key"] == key:
            current["events"].append(event)
        else:
            current = {"key": key, "events": [event]}
            runs.append(current)
    segments = []
    for run in runs:
        steps = []                                          # [grade, rolls, rank at the first roll]
        for event in run["events"]:
            if steps and steps[-1][0] == event["grade"]:
                steps[-1][1] += 1
            else:
                steps.append([event["grade"], 1, event["rank"]])
        for i, (grade, rolls, rank) in enumerate(steps):
            if rank is None:
                continue
            followed = i + 1 < len(steps) and steps[i + 1][0] == grade + 1
            last_grade = grade == 5 and i == len(steps) - 1
            if followed or last_grade:
                segments.append({"rank5": rank == 5, "grade": grade, "rolls": rolls})
    return segments


def build_table(segments, basis):
    """The rolls table. rank5: a rule, N rolls for grade N (up_to = the most ever seen, never below N;
    samples = how many finished grades were seen). below5: the most common value seen for grades 1 to 4
    (ties take the smaller), with up_to, samples and the share of samples that equal it."""
    rank5, below5 = {}, {}
    for grade in range(1, 6):
        seen = [s["rolls"] for s in segments if s["rank5"] and s["grade"] == grade]
        rank5[str(grade)] = {"typical": grade, "up_to": max([grade] + seen), "samples": len(seen), "source": "rule"}
    for grade in range(1, 5):
        seen = [s["rolls"] for s in segments if not s["rank5"] and s["grade"] == grade]
        if not seen:
            continue
        counts = collections.Counter(seen)
        top = max(counts.values())
        below5[str(grade)] = {"typical": min(v for v, n in counts.items() if n == top), "up_to": max(seen),
                              "samples": len(seen), "source": "observed", "share": round(top / len(seen), 2)}
    return {"basis": basis, "rank5": rank5, "below5": below5}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Work out rolls per grade from your own engineering crafts.")
    parser.add_argument("journal_dir", nargs="?", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--write", metavar="PATH", help="write the table (engineering_rolls.json)")
    parser.add_argument("--write-fixture", metavar="PATH", help="write the finished grades (game facts only) for the tests")
    args = parser.parse_args(argv)

    events = read_craft_events(args.journal_dir)
    segments = finished_segments(events)
    basis = (f"Observed in {len(events)} engineering crafts from one commander's journal, read on {datetime.date.today().isoformat()}. "
             "The rank-5 figures are the game's rule (grade N takes N rolls), confirmed by the commander; the below-rank-5 figures are the most common value seen.")
    table = build_table(segments, basis)
    print(f"{len(events)} crafts, {len(segments)} finished grades")
    for name in ("rank5", "below5"):
        for grade, entry in table[name].items():
            print(f"  {name} grade {grade}: {entry}")
    if args.write:
        Path(args.write).write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
        print("wrote", args.write)
    if args.write_fixture:
        Path(args.write_fixture).write_text(json.dumps(segments, indent=0) + "\n", encoding="utf-8")
        print("wrote", args.write_fixture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Generate the real table and fixture from the owner's journals**

Run: `python rolls_from_journal.py --write engineering_rolls.json --write-fixture tests/fixtures/observed_rolls.json`
Expected output includes `rank5 grade 1: {'typical': 1, ...}` through grade 5, and `below5 grade 1` with `'typical': 4, 'up_to': 5` and `'share'` below 0.75, and grades 2 to 4 with `'typical': 4`. Open `tests/fixtures/observed_rolls.json` and confirm it holds only `rank5`, `grade` and `rolls` (no names, times or modules). If grade 1's usual value or any `below5` figure differs from the spec's table, **stop and show the owner**: the journal may have changed.

- [ ] **Step 5: Run the tests** — `python -m pytest tests/test_engineering_rolls.py -q` then `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add engineering_rolls.py rolls_from_journal.py engineering_rolls.json tests/test_engineering_rolls.py tests/fixtures/observed_rolls.json
git commit -m "Rolls per grade: a rule at rank 5, the observed value below it, and a script to regenerate it" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: One shared blueprint resolver

A behaviour-preserving refactor: `get_blueprint` and the new tool must resolve a typed module and blueprint name identically.

**Files:**
- Modify: `query_functions.py` (new `_resolve_blueprint`; `get_blueprint` uses it)
- Modify: `tests/test_blueprint_lookup.py`

**Interfaces:**
- Produces: `qf._resolve_blueprint(conn, module_name, upgrade_name, groups) -> (resolved, miss)`. Exactly one is `None`. `resolved = {"fdname": str, "upgrade_name": str, "grades": [int, ...]}`; `miss = {"reason": str}` plus `"available_upgrades": [...]` when useful (the same texts `get_blueprint` gives today).

- [ ] **Step 1: Write the failing test** — append to `tests/test_blueprint_lookup.py`:

```python
def test_resolve_blueprint_gives_the_one_blueprint_or_a_miss(coriolis_db):
    conn = _conn(coriolis_db)
    try:
        groups = qf._resolve_module_groups(conn, "FSD")
        resolved, miss = qf._resolve_blueprint(conn, "FSD", "Increased FSD Range", groups)
        assert miss is None
        assert resolved == {"fdname": "FSD_LongRange", "upgrade_name": "Increased range", "grades": [1, 2, 3, 4, 5]}
        resolved, miss = qf._resolve_blueprint(conn, "FSD", "Turbo", groups)
        assert resolved is None and miss["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
        resolved, miss = qf._resolve_blueprint(conn, "xyzzy", "Dirty", [])
        assert resolved is None and miss["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
        groups = qf._resolve_module_groups(conn, "Module Reinforcement Package")
        resolved, miss = qf._resolve_blueprint(conn, "Module Reinforcement Package", "Dirty", groups)
        assert resolved is None and miss["reason"] == "Module Reinforcement Package has no engineering blueprints in Ellie's data."
    finally:
        conn.close()
```

- [ ] **Step 2: Run to see it fail** — `python -m pytest tests/test_blueprint_lookup.py -q -k resolve_blueprint` — Expected: FAIL (`AttributeError: ... _resolve_blueprint`).

- [ ] **Step 3: Implement** — in `query_functions.py`, directly above `get_blueprint`'s `@_needs_setup` line, add:

```python
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


```

Then replace the body of `get_blueprint` from the line `if not groups:` through the `cost = _coriolis_cost(...)` / `return _blueprint_reply(...)` pair (keeping the sheet check above it and the `finally: conn.close()` below it) with:

```python
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
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_blueprint_lookup.py -q` then `python -m pytest tests -q`. Expected: all pass, including every existing `get_blueprint` test and the reachability test (this is the proof the refactor changed nothing).

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_blueprint_lookup.py
git commit -m "Share one blueprint resolver between get_blueprint and the upgrade planner" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Where an upgrade starts

**Files:**
- Modify: `query_functions.py` (`_start_grade`)
- Modify: `tests/test_plan_upgrade.py`

**Interfaces:**
- Consumes: `_resolve_module_groups`, `_in_marks`.
- Produces: `qf._start_grade(conn, groups, fdname, stated) -> (grade: int, source: str, note: str|None)`. `source` is `"stated"`, `"fitted module"` or `"from scratch"`. Rules of spec 6.2: a stated grade wins; exactly one fitted module of the group carrying this blueprint gives its level; a fitted module with another modification, or none, or several, or no loadout, starts at 0 with a note.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plan_upgrade.py`:

```python
def _fit(path, slot, item, blueprint=None, level=None):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (slot, item, 1, 1, 1.0, 0, blueprint, level, None, None, None, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


FSD = "int_hyperdrive_overcharge_size5_class5"
LASER = "hpt_pulselaser_gimbal_medium"


def _start(path, module, fdname, stated=None):
    conn = _conn(path)
    try:
        return qf._start_grade(conn, qf._resolve_module_groups(conn, module), fdname, stated)
    finally:
        conn.close()


def test_a_stated_grade_wins_over_the_fitted_one(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 3)
    assert _start(coriolis_db, "FSD", "FSD_LongRange", 1) == (1, "stated", None)
    assert _start(coriolis_db, "FSD", "FSD_LongRange", 0) == (0, "stated", None)


def test_the_fitted_module_with_this_modification_gives_its_level(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 3)
    assert _start(coriolis_db, "FSD", "FSD_LongRange") == (3, "fitted module", None)
    assert _start(coriolis_db, "FSD", "fsd_longrange") == (3, "fitted module", None)       # the journal's casing is not trusted


def test_a_fitted_module_with_another_modification_starts_from_scratch_and_says_so(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_FastBoot", 2)
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "from scratch") and "replaces it" in note


def test_a_fitted_unengineered_module_starts_at_grade_0_and_says_so(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD)
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "fitted module") and "unengineered" in note


def test_several_fitted_modules_start_from_scratch_and_name_the_slots(coriolis_db):
    _fit(coriolis_db, "MediumHardpoint1", LASER, "Weapon_Efficient", 2)
    _fit(coriolis_db, "MediumHardpoint2", LASER)
    grade, source, note = _start(coriolis_db, "pulse laser", "Weapon_Efficient")
    assert (grade, source) == (0, "from scratch")
    assert "MediumHardpoint1, MediumHardpoint2" in note and "from grade N" in note


def test_nothing_fitted_or_no_loadout_starts_from_scratch(coriolis_db):
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "from scratch") and "no loadout has been captured" in note


def test_an_install_without_the_loadout_tables_still_starts_from_scratch(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    conn.execute("DROP TABLE current_loadout_modules")
    conn.commit()
    conn.close()
    assert _start(coriolis_db, "FSD", "FSD_LongRange")[:2] == (0, "from scratch")
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_plan_upgrade.py -q -k "start or fitted or loadout"` — Expected: FAIL (`AttributeError: ... _start_grade`).

- [ ] **Step 3: Implement** — in `query_functions.py` (after `_choose_engineer`):

```python
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
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_plan_upgrade.py -q` then `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_plan_upgrade.py
git commit -m "Work out where an upgrade starts from the fitted module's modification" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The planner

**Files:**
- Modify: `query_functions.py` (`_step_cost`, `_plan_summary`, `plan_upgrade`; import `engineering_rolls`)
- Modify: `tests/test_plan_upgrade.py`

**Interfaces:**
- Consumes: Tasks 1 to 5; `get_experimental_effect`; `_held_count`; `engineering_rolls.load_rolls`, `rolls_for`.
- Produces: `qf.plan_upgrade(module_name, upgrade_name, target_grade, from_grade=None, experimental_effect=None) -> dict`. Found: `{"found": True, "module", "upgrade" (Coriolis's name), "target_grade", "from_grade", "start_source", "rolls_basis" ("rank 5"|"below rank 5"), "steps": [{"grade", "rolls", "rolls_up_to", "per_roll", "cost_known"}], "totals", "totals_up_to", "short": {name: {"need", "need_up_to", "have", "short_by"}}, "engineers": [...], "chosen_engineer": str|None, "thin_evidence": bool, "notes": [str], "experimental": {"effect_name", "materials"}|None, "summary": str}`. Miss: `{"found": False, "module", "upgrade", "target_grade", "reason", ...}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plan_upgrade.py`:

```python
import ingest_excel as ie


def _hold(path, key, shown, count):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES (?,?,?,?,?)", (key, "Encoded", shown, count, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


FSD_STEP_4 = {"Chemical Distillery": 1, "Eccentric Hyperspace Trajectories": 1, "Manganese": 1}
FSD_STEP_5 = {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}


def test_the_fsd_plan_from_grade_3_to_5_with_a_rank_5_engineer(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["found"] is True and plan["upgrade"] == "Increased range"
    assert (plan["from_grade"], plan["target_grade"], plan["start_source"], plan["rolls_basis"]) == (3, 5, "stated", "rank 5")
    assert [(s["grade"], s["rolls"], s["per_roll"], s["cost_known"]) for s in plan["steps"]] == [
        (4, 4, FSD_STEP_4, True), (5, 5, FSD_STEP_5, True)]
    assert all(s["rolls_up_to"] >= s["rolls"] for s in plan["steps"])
    assert plan["totals"] == {"Chemical Distillery": 4, "Eccentric Hyperspace Trajectories": 4, "Manganese": 4,
                              "Arsenic": 5, "Chemical Manipulators": 5, "Datamined Wake Exceptions": 5}
    assert plan["chosen_engineer"] == "Felicity Farseer" and plan["thin_evidence"] is False
    assert {e["name"]: e["usable"] for e in plan["engineers"]} == {"Elvira Martuuk": False, "Felicity Farseer": True, "Mel Brandon": False}
    assert plan["summary"] == (
        "Frame Shift Drive, Increased range, grade 3 to 5 (from grade 3). Felicity Farseer (unlocked, rank 5) can do it. "
        "Rolls at rank 5: grade 4: 4, grade 5: 5. "
        "You need 4 Chemical Distillery, 4 Eccentric Hyperspace Trajectories, 4 Manganese, 5 Arsenic, 5 Chemical Manipulators, 5 Datamined Wake Exceptions. "
        "You are short by: 4 Chemical Distillery (have 0), 4 Eccentric Hyperspace Trajectories (have 0), 4 Manganese (have 0), "
        "5 Arsenic (have 0), 5 Chemical Manipulators (have 0), 5 Datamined Wake Exceptions (have 0). "
        "Extra rolls for a better result will cost more.")
    json.dumps(plan)                                              # plain data only


def test_the_fitted_modules_grade_is_the_start_when_no_grade_is_given(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 4)
    plan = qf.plan_upgrade("FSD", "Increased FSD Range", 5)
    assert (plan["from_grade"], plan["start_source"]) == (4, "fitted module") and [s["grade"] for s in plan["steps"]] == [5]
    assert "your fitted module is at grade 4" in plan["summary"]


def test_below_rank_5_the_plan_uses_four_rolls_a_grade_and_stops_at_the_engineers_rank(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 3)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5)
    assert plan["rolls_basis"] == "below rank 5" and plan["chosen_engineer"] == "Felicity Farseer"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(1, 4), (2, 4), (3, 4)]
    assert plan["thin_evidence"] is True
    assert "is the best you have, but grade 5 needs rank 5, so this plan stops at grade 3." in plan["summary"]
    assert any("Grade 1 rolls below rank 5 vary" in n for n in plan["notes"])


def test_with_no_engineer_unlocked_the_plan_assumes_rank_5_and_says_so(coriolis_db):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=2)
    assert plan["chosen_engineer"] is None and plan["rolls_basis"] == "rank 5"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(3, 3), (4, 4), (5, 5)]
    assert ("None of the engineers who offer it is unlocked yet: Elvira Martuuk (not met), Felicity Farseer (not met), "
            "Mel Brandon (not met). The rolls below assume a rank-5 engineer.") in plan["summary"]


def test_an_engineer_who_is_only_invited_or_known_is_not_unlocked(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Invited")
    _engineer(coriolis_db, "Mel Brandon", "Known")
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=4)
    assert plan["chosen_engineer"] is None and "Felicity Farseer (invited)" in plan["summary"] and "Mel Brandon (known)" in plan["summary"]


def test_an_unlocked_engineer_with_no_rank_yet_assumes_rank_5_and_says_so(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked")          # the journal has not given a rank (rank_value is NULL)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["found"] is True and plan["chosen_engineer"] == "Felicity Farseer" and plan["rolls_basis"] == "rank 5"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(4, 4), (5, 5)]
    assert "The journal has not shown Felicity Farseer's rank yet, so the rolls below assume a rank-5 engineer." in plan["summary"]


def test_the_experimental_effect_is_added_once(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=4, experimental_effect="Mass Manager")
    assert plan["found"] is True and plan["experimental"]["effect_name"] == "Mass Manager"
    assert plan["totals"]["Atypical Disrupted Wake Echoes"] == 5 and plan["totals"]["Galvanising Alloys"] == 3
    assert plan["totals"]["Arsenic"] == 5 and "Includes Mass Manager once" in plan["summary"]


def test_an_effect_the_module_cannot_take_refuses_the_whole_plan(coriolis_db):
    plan = qf.plan_upgrade("Thrusters", "Dirty", 3, experimental_effect="Mass Manager")
    assert plan["found"] is False and "can't be applied to Thrusters" in plan["reason"]


def test_what_is_held_reduces_what_is_short(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _hold(coriolis_db, "manganese", "Manganese", 10)
    _hold(coriolis_db, "arsenic", "Arsenic", 2)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["short"]["Manganese"] == {"need": 4, "need_up_to": 4, "have": 10, "short_by": 0}
    assert plan["short"]["Arsenic"] == {"need": 5, "need_up_to": 5, "have": 2, "short_by": 3}
    assert "3 Arsenic (have 2)" in plan["summary"] and "Manganese (have" not in plan["summary"]


def test_a_module_already_at_the_target_has_nothing_to_collect(coriolis_db):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 4, from_grade=4)
    assert plan["found"] is True and plan["steps"] == [] and plan["totals"] == {}
    assert plan["summary"].startswith("Frame Shift Drive, Increased range: your module is already at grade 4")


@pytest.mark.parametrize("target", ["five", None, 0, 6, "", []])
def test_a_target_grade_that_is_not_1_to_5_gets_a_plain_reply(coriolis_db, target):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", target)
    assert plan["found"] is False and plan["reason"] == "The target grade must be a number from 1 to 5."


@pytest.mark.parametrize("start", ["x", 7, -1, []])
def test_a_starting_grade_that_is_not_0_to_4_gets_a_plain_reply(coriolis_db, start):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=start)
    assert plan["found"] is False and plan["reason"] == "The starting grade must be a number from 0 to 4."


def test_an_unknown_module_or_blueprint_is_a_miss_that_lists_the_real_options(coriolis_db):
    plan = qf.plan_upgrade("xyzzy", "Dirty", 3)
    assert plan["found"] is False and plan["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
    plan = qf.plan_upgrade("Frame Shift Drive", "Turbo", 3)
    assert plan["found"] is False and plan["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    for blank in (("", "", 3), (None, None, 3), ("Thrusters", "", 3)):
        assert qf.plan_upgrade(*blank)["found"] is False


def test_a_grade_the_blueprint_does_not_have_is_named(coriolis_db):
    plan = qf.plan_upgrade("Shield Cell Bank", "Rapid charge", 5)
    assert plan["found"] is False and "offered at grades 1-4, not grade 5" in plan["reason"]


def test_a_blueprint_with_no_engineer_and_no_recorded_cost_says_so_and_invents_nothing(coriolis_db):
    plan = qf.plan_upgrade("Cargo Rack", "Expanded Capacity", 5)
    assert plan["found"] is True and plan["engineers"] == [] and plan["chosen_engineer"] is None
    assert [(s["grade"], s["cost_known"]) for s in plan["steps"]] == [(5, False)] and plan["totals"] == {}
    assert "No engineer in Coriolis's data offers it at grade 5." in plan["summary"]
    assert any("no recorded materials" in n for n in plan["notes"])


def test_the_spreadsheet_cost_of_a_grade_wins_as_in_part_one(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    ie.build_schema(conn)
    conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)",
                 ("core_internal", "Frame Shift Drive", "Increased range", 4, json.dumps([{"qty": 2, "item": "Arsenic"}]), None))
    conn.commit()
    conn.close()
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 4, from_grade=3)
    assert plan["steps"][0]["per_roll"] == {"Arsenic": 2}


def test_every_coriolis_blueprint_can_be_planned_to_every_grade(coriolis_db):
    """The reachability pattern of part one, through plan_upgrade: with nobody unlocked the rolls are the
    rank-5 rule (N for grade N), the totals are exactly the steps added up, and every blueprint that has an
    engineer lists one."""
    conn = _conn(coriolis_db)
    failures, checked = [], 0
    try:
        for grp in [r[0] for r in conn.execute("SELECT DISTINCT grp FROM module_blueprints ORDER BY grp")]:
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (grp,)).fetchone()
                      or {"name": {"bh": "armour"}.get(grp, grp)})["name"]
            for (fdname,) in conn.execute("SELECT fdname FROM module_blueprints WHERE grp = ?", (grp,)).fetchall():
                upgrade = conn.execute("SELECT upgrade_name FROM coriolis_blueprints WHERE fdname = ? LIMIT 1", (fdname,)).fetchone()[0]
                for grade in [r[0] for r in conn.execute("SELECT grade FROM coriolis_blueprints WHERE fdname = ?", (fdname,))]:
                    checked += 1
                    plan = qf.plan_upgrade(module, upgrade, grade, from_grade=0)
                    expected = {}
                    for step in plan.get("steps", []):
                        for name, qty in step["per_roll"].items():
                            expected[name] = expected.get(name, 0) + qty * step["rolls"]
                    if (not plan["found"] or plan["totals"] != expected or any(s["rolls"] != s["grade"] for s in plan["steps"])
                            or (not plan["engineers"] and (fdname, grade) != ("CargoRack_IncreasedCapacity", 5))):
                        failures.append((grp, fdname, grade, plan.get("reason")))
    finally:
        conn.close()
    assert checked > 800
    assert failures == []
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_plan_upgrade.py -q -k "plan or fsd or rank or effect or held or target or starting"` — Expected: FAIL (`AttributeError: ... plan_upgrade`).

- [ ] **Step 3: Implement** — in `query_functions.py` add `from engineering_rolls import load_rolls, rolls_for` to the imports and, after `_start_grade`, add:

```python
def _step_cost(conn, module_name, groups, upgrade_name, fdname, grade):
    """({material: qty}, [extras]) for ONE roll at this grade: the spreadsheet's row first (as in
    get_blueprint), otherwise Coriolis's; ({}, []) when neither records anything."""
    sheet = _sheet_blueprint(conn, module_name, groups, upgrade_name, grade)
    if sheet is not None:
        return _split_materials(sheet["materials_json"])
    cost = _coriolis_cost(conn, fdname, grade)
    return (cost["materials"], cost["extras"]) if cost else ({}, [])


def _plan_summary(plan, chosen, mode, offers):
    """The short paragraph a 2-4B model reads out. Every figure in it is in the plan's own fields."""
    module, upgrade, target, start = plan["module"], plan["upgrade"], plan["target_grade"], plan["from_grade"]
    if start >= target:
        return f"{module}, {upgrade}: your module is already at grade {start}, so there is nothing to collect for grade {target}."
    where = {"fitted module": f"your fitted module is at grade {start}", "stated": f"from grade {start}"}.get(
        plan["start_source"], "from an unengineered module")
    parts = [f"{module}, {upgrade}, grade {start} to {target} ({where})."]
    if mode == "usable":
        parts.append(f"{chosen['name']} (unlocked, rank {chosen['rank']}) can do it.")
    elif mode == "low_rank":
        parts.append(f"{chosen['name']} (unlocked, rank {chosen['rank']}) is the best you have, but grade {target} needs rank {target}, "
                     f"so this plan stops at grade {chosen['rank']}.")
    elif mode == "rank_unknown":
        parts.append(f"The journal has not shown {chosen['name']}'s rank yet, so the rolls below assume a rank-5 engineer.")
    elif mode == "none_unlocked":
        listed = ", ".join(f"{o['name']} ({o['status']})" for o in offers)
        parts.append(f"None of the engineers who offer it is unlocked yet: {listed}. The rolls below assume a rank-5 engineer.")
    else:
        parts.append(f"No engineer in Coriolis's data offers it at grade {target}.")
    if plan["steps"]:
        parts.append(f"Rolls at {plan['rolls_basis']}: " + ", ".join(f"grade {s['grade']}: {s['rolls']}" for s in plan["steps"]) + ".")
    if plan["totals"]:
        parts.append("You need " + ", ".join(f"{qty} {name}" for name, qty in plan["totals"].items()) + ".")
        lacking = [(name, row) for name, row in plan["short"].items() if row["short_by"] > 0]
        if lacking:
            parts.append("You are short by: " + ", ".join(f"{row['short_by']} {name} (have {row['have']})" for name, row in lacking) + ".")
        else:
            parts.append("You hold everything.")
    parts.extend(plan["notes"])
    parts.append("Extra rolls for a better result will cost more.")
    return " ".join(parts)


@_needs_setup
def plan_upgrade(module_name: str, upgrade_name: str, target_grade: int, from_grade: int = None, experimental_effect: str = None) -> dict:
    """Plan an engineering upgrade of ONE module up to a grade: rolls per grade, the materials for them, what
    is held and short, and which engineers can do it. Part one's per-roll costs times the rolls in
    engineering_rolls.json (N rolls for grade N at an engineer of rank 5, about 4 below), for the grades
    between the starting grade and the target. The starting grade is the fitted module's own when it can
    be known, else an unengineered module; every limit is stated, nothing is guessed."""
    module_name = str(module_name or "").strip()
    upgrade_name = str(upgrade_name or "").strip()
    target = _grade_number(target_grade)
    base = {"module": module_name, "upgrade": upgrade_name, "target_grade": target if target is not None else target_grade}
    if target is None or not 1 <= target <= 5:
        return {"found": False, **base, "reason": "The target grade must be a number from 1 to 5."}
    stated = None
    if from_grade not in (None, ""):
        stated = _grade_number(from_grade)
        if stated is None or not 0 <= stated <= 4:
            return {"found": False, **base, "reason": "The starting grade must be a number from 0 to 4."}
    effect = None
    if experimental_effect not in (None, ""):
        effect = get_experimental_effect(module_name, str(experimental_effect))
        if not effect["found"]:
            return {"found": False, **base, "reason": effect["reason"]}

    conn = _connect()
    try:
        groups = _resolve_module_groups(conn, module_name)
        resolved, miss = _resolve_blueprint(conn, module_name, upgrade_name, groups)
        if miss:
            return {"found": False, **base, **miss}
        fdname, grades = resolved["fdname"], resolved["grades"]
        if target not in grades:
            plural = "s" if len(grades) > 1 else ""
            return {"found": False, **base, "available_grades": grades,
                    "reason": f"{resolved['upgrade_name']} for {module_name} is offered at grade{plural} {_grade_span(grades)}, not grade {target}."}

        start, start_source, start_note = _start_grade(conn, groups, fdname, stated)
        offers = _engineers_offering(conn, fdname, target)
        chosen, mode = _choose_engineer(offers, target)
        # A usable or lower-ranked engineer sets the rank. With nobody unlocked, or no rank known yet, assume
        # the rank-5 engineer one will need (and the summary says so); never a crash on a missing rank.
        rank = chosen["rank"] if mode in ("usable", "low_rank") else 5
        reach = min(target, rank)
        rank5 = rank == 5
        table = load_rolls()

        steps, notes, totals, totals_up = [], [], {}, {}
        if start_note:
            notes.append(start_note)
        thin = False
        for grade in range(start + 1, reach + 1):
            if grade not in grades:
                continue
            entry = rolls_for(table, rank5, grade)
            if entry is None:
                notes.append(f"There is no rolls figure for grade {grade} at this rank, so it is left out.")
                continue
            materials, extras = _step_cost(conn, module_name, groups, upgrade_name, fdname, grade)
            known = bool(materials or extras)
            steps.append({"grade": grade, "rolls": entry["typical"], "rolls_up_to": entry["up_to"],
                          "per_roll": materials, "cost_known": known})
            if entry["thin"]:
                thin = True
                notes.append(f"Grade {grade} rolls below rank 5 vary (up to {entry['up_to']}); {entry['typical']} is the usual number.")
            if known:
                for name, qty in materials.items():
                    totals[name] = totals.get(name, 0) + qty * entry["typical"]
                    totals_up[name] = totals_up.get(name, 0) + qty * entry["up_to"]
            else:
                notes.append(f"Grade {grade} has no recorded materials, so it is left out of the totals.")
        if not steps and start < reach:
            notes.append("Nothing more can be planned with this engineer at your module's current grade.")

        experimental = None
        if effect:
            experimental = {"effect_name": effect["effect_name"], "materials": effect["materials"]}
            for name, qty in effect["materials"].items():
                totals[name] = totals.get(name, 0) + qty
                totals_up[name] = totals_up.get(name, 0) + qty
            notes.append(f"Includes {effect['effect_name']} once ({_materials_text(effect['materials'], [])}).")

        short = {}
        for name, need in totals.items():
            held = _held_count(conn, name)
            have = held["count"] if held else 0
            short[name] = {"need": need, "need_up_to": totals_up[name], "have": have, "short_by": max(0, need - have)}

        plan = {"found": True, **base, "upgrade": resolved["upgrade_name"], "from_grade": start, "start_source": start_source,
                "rolls_basis": "rank 5" if rank5 else "below rank 5", "steps": steps, "totals": totals,
                "totals_up_to": totals_up, "short": short, "engineers": offers,
                "chosen_engineer": chosen["name"] if chosen else None, "thin_evidence": thin, "notes": notes,
                "experimental": experimental}
        plan["summary"] = _plan_summary(plan, chosen, mode, offers)
        return plan
    finally:
        conn.close()
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_plan_upgrade.py -q`, then `python -m pytest tests -q`. Expected: all pass. If the pinned summary differs only in a rolls figure, check `engineering_rolls.json` against the spec's table before changing any expectation.

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_plan_upgrade.py
git commit -m "plan_upgrade: rolls, materials, shortfall and engineers for one upgrade" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Give the tool to the model

**Files:**
- Modify: `ellie_chat.py` (a `TOOLS` entry and the `DISPATCH` entry)
- Modify: `tests/test_query_snapshot.py`, `README.md`
- Create: `tests/test_ellie_chat_plan_tool.py`

**Interfaces:** Consumes `qf.plan_upgrade`. Produces the registered tool `plan_upgrade`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_ellie_chat_plan_tool.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def _tool():
    return next(t["function"] for t in ec.TOOLS if t["function"]["name"] == "plan_upgrade")


def test_plan_upgrade_is_registered_and_dispatched():
    assert "plan_upgrade" in ec.DISPATCH and _tool()


def test_the_description_has_no_category_words_and_names_what_comes_back():
    tool = _tool()
    assert tool["description"] == (
        "Plan an engineering upgrade of one specific module up to a grade (1-5): rolls per grade, total materials, "
        "what is short, and which engineers can do it.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name as the commander said it, e.g. 'Frame Shift Drive', 'Thrusters', 'Shield Generator'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"
    assert props["target_grade"]["description"] == "Grade to reach, 1 to 5"
    assert props["from_grade"]["description"] == "Grade the module is at now (0 if unengineered); only when the commander says"
    assert props["experimental_effect"]["description"] == "Optional experimental effect to include, e.g. 'Mass Manager'"
    assert tool["parameters"]["required"] == ["module_name", "upgrade_name", "target_grade"]


def test_call_tool_reaches_the_planner(monkeypatch):
    calls = []
    monkeypatch.setitem(ec.DISPATCH, "plan_upgrade", lambda **kw: calls.append(kw) or {"found": False})
    ec.call_tool("plan_upgrade", {"module_name": "FSD", "upgrade_name": "Increased range", "target_grade": 5})
    assert calls == [{"module_name": "FSD", "upgrade_name": "Increased range", "target_grade": 5}]


def test_bad_arguments_are_a_clean_error_not_a_crash():
    assert "error" in ec.call_tool("plan_upgrade", {"wrong_kw": 1})
```

In `tests/test_query_snapshot.py`, add to `TOOL_CALLS` (after the `simulate_module_swap` entries):
`    "plan_upgrade": lambda: qf.plan_upgrade("Beam Laser", "Efficient", 3),`

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_ellie_chat_plan_tool.py tests/test_needs_setup_guards.py -q` — Expected: FAIL for the new tests (the tool is not registered).

- [ ] **Step 3: Implement** — in `ellie_chat.py` add this entry to the `TOOLS` list, after the `simulate_module_swap` entry:

```python
    {
        "type": "function",
        "function": {
            "name": "plan_upgrade",
            "description": "Plan an engineering upgrade of one specific module up to a grade (1-5): rolls per grade, total materials, what is short, and which engineers can do it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name as the commander said it, e.g. 'Frame Shift Drive', 'Thrusters', 'Shield Generator'"},
                    "upgrade_name": {"type": "string", "description": "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"},
                    "target_grade": {"type": "integer", "description": "Grade to reach, 1 to 5"},
                    "from_grade": {"type": "integer", "description": "Grade the module is at now (0 if unengineered); only when the commander says"},
                    "experimental_effect": {"type": "string", "description": "Optional experimental effect to include, e.g. 'Mass Manager'"},
                },
                "required": ["module_name", "upgrade_name", "target_grade"],
            },
        },
    },
```

and to `DISPATCH`: `    "plan_upgrade": qf.plan_upgrade,`.

In `README.md`, the tools list is out of date (it names 7 of what will be 11 tools). Replace these two lines (22 and 23):

```
The tools: `get_blueprint`, `get_experimental_effect`, `missing_materials`, `get_engineer`,
`get_material_count`, `player_state`, `distance_to_system`. Your materials, ranks, ship and location
```

with these three:

```
The tools: `get_blueprint`, `get_experimental_effect`, `missing_materials`, `plan_upgrade`, `get_engineer`,
`get_material_count`, `player_state`, `distance_to_system`, and for your ship `get_current_loadout`,
`get_ship_info` and `simulate_module_swap`. Your materials, ranks, ship and location
```

and in the diagram on line 19 replace `(the 7 tools)         (chat loop)` with `(the 11 tools)        (chat loop)` (one space fewer, so the columns stay aligned). `tests/test_readme.py` does not pin either line.

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_ellie_chat_plan_tool.py tests/test_needs_setup_guards.py tests/test_query_snapshot.py tests/test_readme.py -q` then `python -m pytest tests -q`. Expected: all pass. (The guard tests parametrise over `DISPATCH`, so they now check `plan_upgrade` for the `@_needs_setup` decorator and its position.)

- [ ] **Step 5: Commit**

```bash
git add ellie_chat.py README.md tests/test_ellie_chat_plan_tool.py tests/test_query_snapshot.py
git commit -m "Register plan_upgrade as a tool the model can call" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Benchmark questions, documentation and the final checks

**Files:**
- Modify (local, git-ignored): `.superpowers/bench/bench_v2.py`
- Modify: `docs/blueprint-data.md`
- Verify only: the whole repository

- [ ] **Step 1: Add the plan questions to the benchmark** — in `.superpowers/bench/bench_v2.py`, directly after the `key_materials` function add:

```python
def key_plan(call):
    def f():
        r = call()
        names = list((r.get("totals") or {}).keys())
        return {"names": names, "min_names": min(2, len(names)) if names else 0}
    return f
```
and, directly after the line `add("D20", ...)`, add (all in category `P`, so `--cats P` runs only them):

```python
    P = "plan_upgrade"
    add("P1", "P", T("What do I need to take my FSD to Increased Range grade 5?", {P}, _has("fsd", "frame", "range"), key=key_plan(lambda: qf.plan_upgrade("Frame Shift Drive", "Increased range", 5))))
    add("P2", "P", T("Plan Dirty Drives on my thrusters up to grade 3.", {P}, _has("thruster", "dirty"), key=key_plan(lambda: qf.plan_upgrade("Thrusters", "Dirty", 3))))
    add("P3", "P", T("What materials do I need to take my sensors from grade 2 to grade 4 Long Range?", {P}, _has("sensor", "long"), key=key_plan(lambda: qf.plan_upgrade("Sensors", "Long range", 4, from_grade=2))))
    add("P4", "P", T("Plan a Charge Enhanced power distributor up to grade 3.", {P}, _has("distributor", "charge"), key=key_plan(lambda: qf.plan_upgrade("Power Distributor", "Charge enhanced", 3))))
    add("P5", "P", T("What would grade 5 Armoured on my power plant take?", {P}, _has("power", "armoured"), key=key_plan(lambda: qf.plan_upgrade("Power Plant", "Armoured", 5))))
    add("P6", "P", T("Which engineers can take my life support to Lightweight grade 2, and what do I need?", {P}, _has("life", "lightweight"), key=key_plan(lambda: qf.plan_upgrade("Life Support", "Lightweight", 2))))
```

- [ ] **Step 2: Run the benchmark** (local, free; needs Ollama running with `bonsai-2:4b`) from the repository root:

```bash
BENCH_CODE_DIR="$PWD" python .superpowers/bench/bench_v2.py --models bonsai-2:4b --trials 3 --cats D,P --out "$TEMP/bench_p.json"
```
Expected: each of P1 to P6 passes in at least 2 of 3 trials (the spec's bar). The `D` category (the existing lookups) is run too, because a new tool next to `missing_materials` can steal its questions: D3, D19 and D20 ("what am I still missing for ...") must still call `missing_materials` or `get_blueprint`, and none of D1 to D20 may now call `plan_upgrade`. If a P question fails, read the tool call and answer in the output before changing anything. The usual causes are the model passing a wrong module or splitting a phrase into module and blueprint (fix with the tool description, pinned by a test first), or a `summary` it garbles (shorten it, with a test). If a D question now calls `plan_upgrade`, tighten the description toward "up to a grade" and re-run; do not weaken the P questions.

- [ ] **Step 3: Document it** — append to `docs/blueprint-data.md`:

```markdown

## Plans and the rolls table

`plan_upgrade` answers "what do I need to take this module to grade N?". It multiplies the per-roll costs above by the rolls each grade takes. An engineer at **rank 5** makes every upgrade N rolls for grade N (grade 5 included, a rule). Below rank 5, grades 1 to 4 took about 4 rolls each in the owner's journal (grade 1 varies between 3 and 5, so Ellie says so). The rank used is the chosen engineer's current rank, taken from your journal; with nobody unlocked Ellie assumes the rank-5 engineer you will need and says so. The figures live in `engineering_rolls.json`; to check them against your own play:

```bash
python rolls_from_journal.py
```

prints the table your journal produces. Engineers per blueprint and grade come from Coriolis; Coriolis misspells "Felicity Farseer" in nine lists, which Ellie corrects.
```

- [ ] **Step 4: Whole-branch check** — `python -m pytest tests -q` (expect the baseline 668 plus every new test, all passing); `git log --oneline main..HEAD` lists one commit per task; `git status` is clean apart from the git-ignored benchmark; `git diff main --stat` touches only the files named in this plan. Report the final test count and the benchmark result, then hand the branch over for the merge decision (finishing-a-development-branch). Do not push.

- [ ] **Step 5: Commit**

```bash
git add docs/blueprint-data.md
git commit -m "Document plan_upgrade and the rolls table" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review (run against the spec)

**Spec coverage.** Section 1 done-when 1 (totals equal the steps, for every blueprint and grade): Task 6's reachability test. 2 (rolls follow the rank, file with evidence, script regenerates): Tasks 3 and 6. 3 (engineers from Coriolis, status from the journal, works without the sheet): Tasks 1, 2, 6 (every test runs on a database with no sheet tables; one test adds a sheet row). 4 (starting grade): Task 5. 5 (limits stated): Task 6, one test per row of 6.8 (no engineer / none unlocked / below the target rank / unknown cost / thin / no loadout or several fitted). 6 (benchmark): Task 8. Section 6.1 (the tool, flat arguments, bad grades): Tasks 6 and 7. 6.2: Task 5. 6.3 (rolls file, `source`, thin rule, script): Task 3. 6.4 (candidates, status, usable, chosen): Task 2. 6.5 (names, aliases, every Coriolis name resolves): Task 2. 6.6 (maths, optional effect once, held by game name): Task 6. 6.7 (reply shape and summary): Task 6. 6.9 (tool description): Task 7. Section 7 (tests; connection-closing test gets the tool): Tasks 6 and 7. Section 9 (files; the one-table amendment): Task 1.

**Placeholders.** None: every code step shows the code; Task 3 Step 4 generates the real files from the owner's journals and says what to check.

**Type consistency.** `_engineers_offering` returns `{"name","status","rank","usable"}` and `_choose_engineer` consumes exactly those keys (Task 2 defines both; Task 6 uses them). `_start_grade` returns the `(grade, source, note)` triple used by `plan_upgrade` (Task 5 defines it). `_resolve_blueprint` returns the `{"fdname","upgrade_name","grades"}` / miss pair (Task 4 defines it; Task 6 uses it). `rolls_for` returns `{"typical","up_to","samples","source","thin"}` (Task 3 defines it; Task 6 uses `typical`, `up_to`, `thin`). The plan dict keys named in Task 6's Interfaces are the ones `_plan_summary` and the tests read. `plan_upgrade` is defined once and registered by that name in Task 7.
