# Engineering depth part 1: trustworthy blueprint data. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_blueprint`, `missing_materials`, `get_experimental_effect` and `get_material_count` answer correctly for every engineerable module from Coriolis data alone, say so plainly when a cost is unknown, and match held materials by the name the game shows.

**Architecture:** Module names resolve to Coriolis group codes through two data-driven tables built from the `coriolis-data` clone (`module_group_names` from the module file names, `module_specials` from `modules.json`) plus a short synonym table. A group's allowed blueprints come from the existing `module_blueprints` table. Blueprint names match with the existing forgiving matcher plus one new word-based rule. The spreadsheet tables become optional at every query. A journal check (`verify_blueprints.py`) and a committed fixture of real crafts pin the costs.

**Tech Stack:** Python 3, stdlib `sqlite3`, `pytest`, `openpyxl` (already a dependency, used by the sheet ingest). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-06-engineering-blueprint-data-design.md` (read it first; this plan implements it).

## Global Constraints

Copied from the spec; every task's requirements include these.

- Done when: every Coriolis blueprint and grade can be looked up by a friendly module name and by the blueprint's in-game or Coriolis name and returns Coriolis's cost; the 48 grade combinations and 9 experimental effects seen in the owner's journals return the costs those crafts consumed (two experimental effects differ today: Stripped Down `special_weapon_lightweight`, Drag Munitions `special_drag_munitions`); held materials match by the name the game shows (the 34 affected material types are found); a blueprint with no recorded materials is "cost not recorded", never free; a database built from Coriolis alone (no spreadsheet tables) answers every lookup; `python setup.py` offers to update the Coriolis clone and rebuilds dependent tables.
- Costs are **per roll** (`materials_per: "roll"`); an experimental effect's cost is **per application** (`materials_per: "application"`).
- Source precedence is unchanged: the spreadsheet's row first when it has the module, upgrade and grade; otherwise Coriolis. The spreadsheet is optional at every step.
- Material names are compared with `_material_key` (lower case, spaces and hyphens removed). Held materials match on the journal's stored key **or** the game's display name.
- A reply never states a number or material that did not come from a table. Tool functions return dicts, never raise on bad model input (`""`, `None`, `"five"`, an unknown module), keep `@_needs_setup`, and open exactly one connection through `_connect()` closed in `try/finally`.
- No new dependencies; `requirements.txt` is unchanged. No change to the swap simulator's behaviour, the journal watcher, or the system prompt.
- Tests that read `coriolis-data` are listed in `NEEDS_CORIOLIS_DATA` in `tests/conftest.py` so they skip, with a reason, on a fresh clone. Pure tests need no data.
- Run tests from the repository root with `python -m pytest`. The baseline before this work is **500 passed**.
- Every commit message ends with a blank line and `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. **Do not push** and do not change Git credentials.
- Windows: when printing text that may contain non-ASCII characters from a script, set `PYTHONIOENCODING=utf-8`.

## Setup before Task 1

Work on a branch in the main checkout: `git checkout -b engineering-blueprint-data` (a separate worktree is optional; removing the last one on this OneDrive folder needed manual steps). `coriolis-data/` must exist (run `python setup.py` once if it does not). Confirm the baseline: `python -m pytest tests -q` prints `500 passed`.

## Review Focus

Failure modes the spec implies that a person using Ellie will hit. Each has a test in the task that owns the code.

1. **A module or blueprint name typed oddly** ("FRAME-SHIFT DRIVE", "thruster", "", "   ", a trailing plural): resolves, or gives a clean "unknown" reply listing real names; never an exception. Tests: Task 3 (resolver table), Task 4 (empty strings).
2. **A grade given as text or out of range** ("5", "five", 0, 6, `None`): text digits work; anything else gets "The grade must be a number from 1 to 5" or "offered at grades 1-5, not grade 6". Tests: Task 4.
3. **A material shown under a different name than the journal stored**, or never held: found by either name; never held counts 0 without error. Tests: Task 5.
4. **A database without the spreadsheet tables, or from before this change**: Coriolis-only answers every lookup; an install without the new tables gets the "run `python setup.py`" reply and setup repairs it. Tests: Tasks 1, 3, 4.
5. **A sheet row with a `(0)` annotation or a line with no quantity ("Needs personal research")** in an already-ingested database: the annotation never shows, the quantity-less line is reported separately and never counted as a number. Tests: Task 4.

---

### Task 1: Group-name and specials tables

**Files:**
- Modify: `ingest_ship_modules.py` (`build_schema`, new `build_module_group_names`, `build_module_specials`, `ingest_all`)
- Modify: `setup.py` (`STEP_TABLES`, `setup_ship_modules`)
- Modify: `tests/test_ingest_ship_modules.py`, `tests/test_setup_ship_modules.py`

**Interfaces:**
- Produces: tables `module_group_names (grp TEXT, name TEXT, PRIMARY KEY (grp, name))` (lower-case names, each module file's name, with a trailing plural also stored singular) and `module_specials (grp TEXT, edname TEXT, PRIMARY KEY (grp, edname))`. Functions `ism.build_module_group_names(conn, repo_path, commit=True) -> int` and `ism.build_module_specials(conn, repo_path, commit=True) -> int`. `ism.ingest_all(conn, repo_path)` still returns the 3-tuple `(ship_count, module_count, blueprint_count)` and now also fills both tables.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest_ship_modules.py`:

```python
def test_group_names_come_from_the_module_files(tmp_path):
    conn = _fresh_db(tmp_path)
    count = ism.build_module_group_names(conn, str(REPO))
    conn.commit()
    assert count > 80                                                    # 96 on the data of 2026-11-06

    def names(grp):
        return {r[0] for r in conn.execute("SELECT name FROM module_group_names WHERE grp = ?", (grp,))}

    assert "frame shift drive" in names("fsd")
    assert {"thrusters", "thruster"} <= names("t")                      # a trailing plural is also stored singular
    assert {"collector limpet controllers", "collector limpet controller"} <= names("cc")
    assert "power plant" in names("pp") and "life support" in names("ls")
    assert "sensors" in names("s")


def test_every_group_with_blueprints_has_a_name_except_ship_armour(tmp_path):
    """Measured on the real data: 43 of the 44 groups with blueprints take their name from
    a module file; ship armour (bh) has no module file and is named by a synonym instead."""
    conn = _fresh_db(tmp_path)
    ism.build_module_blueprints(conn, str(REPO))
    ism.build_module_group_names(conn, str(REPO))
    conn.commit()
    nameless = {r[0] for r in conn.execute(
        "SELECT DISTINCT grp FROM module_blueprints WHERE grp NOT IN (SELECT grp FROM module_group_names)")}
    assert nameless == {"bh"}


def test_no_name_belongs_to_two_groups(tmp_path):
    conn = _fresh_db(tmp_path)
    ism.build_module_group_names(conn, str(REPO))
    shared = conn.execute("SELECT name FROM module_group_names GROUP BY name HAVING COUNT(*) > 1").fetchall()
    assert shared == []


def test_module_specials_list_which_group_can_take_which_effect(tmp_path):
    conn = _fresh_db(tmp_path)
    count = ism.build_module_specials(conn, str(REPO))
    conn.commit()
    assert count > 100                                                   # 177 on the data of 2026-11-06

    def specials(grp):
        return {r[0] for r in conn.execute("SELECT edname FROM module_specials WHERE grp = ?", (grp,))}

    assert "special_fsd_heavy" in specials("fsd")              # Mass Manager
    assert "special_fsd_heavy" not in specials("t")
    assert "special_engine_toughened" in specials("t")          # Double Braced
    assert "special_drag_munitions" in specials("fc")


def test_ingest_all_fills_the_new_tables_and_keeps_its_three_part_result(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "all.db"))
    result = ism.ingest_all(conn, str(REPO))
    assert len(result) == 3
    for table in ("module_group_names", "module_specials"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
    again = ism.ingest_all(conn, str(REPO))                      # a rerun rebuilds, it does not duplicate
    assert again == result
    conn.close()
```

In `tests/test_setup_ship_modules.py` change the `TABLES` line to
`TABLES = ("ship_stats", "module_stats", "module_blueprints", "module_group_names", "module_specials")`
and update the module docstring's first line to say "all five tables". Then append:

```python
def test_an_install_from_before_the_name_tables_is_repaired_without_asking(setup_db, monkeypatch, capsys):
    conn = sqlite3.connect(str(setup_db))
    conn.execute("DROP TABLE module_group_names")
    conn.execute("DROP TABLE module_specials")
    conn.commit()
    conn.close()
    _must_not_ask(monkeypatch)
    assert setup_script.setup_ship_modules() is True
    assert "module_group_names, module_specials are empty -- re-ingesting ship & module stats." in capsys.readouterr().out
    assert _count(setup_db, "module_group_names") > 0 and _count(setup_db, "module_specials") > 0
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py -q`
Expected: FAIL (`AttributeError: ... build_module_group_names`; the setup tests fail on the missing tables).

- [ ] **Step 3: Implement the ingest**

In `ingest_ship_modules.py`, in `build_schema`, immediately before the final `if commit: conn.commit()`, add:

```python
    # Two more IF NOT EXISTS tables, for the same reason as module_blueprints: an
    # install that has the older tables gets these from this script, never lost to a DROP.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS module_group_names (
            grp TEXT, name TEXT, PRIMARY KEY (grp, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS module_specials (
            grp TEXT, edname TEXT, PRIMARY KEY (grp, edname)
        )
        """
    )
```

After `build_module_blueprints` add:

```python
def _stem_names(stem):
    """'frame_shift_drive' -> {'frame shift drive'}. A trailing plural ('thrusters',
    'collector_limpet_controllers') is also stored singular, because players say both."""
    name = " ".join(stem.lower().split("_"))
    names = {name}
    if name.endswith("s") and not name.endswith("ss"):
        names.add(name[:-1])
    return names


def build_module_group_names(conn, repo_path, commit=True):
    """Each module group's name, taken from the file its modules live in
    (modules/*/frame_shift_drive.json -> 'frame shift drive'), so a group Coriolis adds
    later gets a name after the next ingest with no code change. Clears and rebuilds."""
    conn.execute("DELETE FROM module_group_names")
    count = 0
    for subdir in MODULE_DIRS:
        module_dir = Path(repo_path) / "modules" / subdir
        if not module_dir.is_dir():
            raise FileNotFoundError(f"coriolis-data input folder not found: {module_dir}")
        for path in sorted(module_dir.glob("*.json")):
            data = json.load(open(path, encoding="utf-8"))
            groups = {m.get("grp") for variants in data.values() if isinstance(variants, list)
                      for m in variants if m.get("grp")}
            for grp in groups:
                for name in _stem_names(path.stem):
                    count += conn.execute("INSERT OR IGNORE INTO module_group_names VALUES (?,?)", (grp, name)).rowcount
    if commit:
        conn.commit()
    return count


def build_module_specials(conn, repo_path, commit=True):
    """Which experimental effects (specials) each group can take, from the same
    modifications/modules.json as the blueprint allow-list. Clears and rebuilds."""
    data = json.load(open(Path(repo_path) / "modifications" / "modules.json", encoding="utf-8"))
    conn.execute("DELETE FROM module_specials")
    count = 0
    for grp, entry in data.items():
        for edname in (entry.get("specials") or []):
            count += conn.execute("INSERT OR IGNORE INTO module_specials VALUES (?,?)", (grp, edname)).rowcount
    if commit:
        conn.commit()
    return count
```

In `ingest_all`, after `blueprint_count = build_module_blueprints(conn, repo_path, commit=False)` add:

```python
        build_module_group_names(conn, repo_path, commit=False)
        build_module_specials(conn, repo_path, commit=False)
```

Update `ingest_all`'s docstring first line to "Rebuild ship_stats, module_stats, module_blueprints, module_group_names and module_specials". Leave the return value as the 3-tuple.

- [ ] **Step 4: Teach setup.py about the new tables**

In `setup.py`: in `STEP_TABLES`, change the ship line to
`"Ship/module stats (loadout calculator)": ["ship_stats", "module_stats", "module_blueprints", "module_group_names", "module_specials"],`.
In `setup_ship_modules`, change `tables = ("ship_stats", "module_stats", "module_blueprints")` to
`tables = ("ship_stats", "module_stats", "module_blueprints", "module_group_names", "module_specials")`
and replace the two-line `print("ship_stats, module_stats and module_blueprints already have " ...)` with:

```python
        print("The ship & module tables already have rows ("
              + ", ".join(f"{t} {counts[t]}" for t in tables) + ").")
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py -q`
Expected: all pass. Then run `python -m pytest tests -q`; expected: 500 + the new tests passed, none failed.

- [ ] **Step 6: Commit**

```bash
git add ingest_ship_modules.py setup.py tests/test_ingest_ship_modules.py tests/test_setup_ship_modules.py
git commit -m "Ingest module group names and specials; setup repairs installs without them" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Pure name logic in `blueprint_mapping.py`

**Files:**
- Modify: `blueprint_mapping.py` (add; the keyword map stays until Task 7)
- Create: `tests/test_blueprint_mapping.py`

**Interfaces:**
- Produces: `GROUP_SYNONYMS: dict[str, str]` (name -> group code), `BLUEPRINT_ALIASES: dict[str, tuple[str, ...]]` (fdname -> extra in-game names), `squash(text) -> str` (lower case, only letters and digits), `name_tokens(text) -> set[str]` (lower-case words, splitting camelCase), `clean_material_name(text) -> str` (strips a trailing `(n)`).

- [ ] **Step 1: Write the failing tests** — create `tests/test_blueprint_mapping.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm


@pytest.mark.parametrize("text, expected", [
    ("Frame Shift Drive", "frameshiftdrive"),
    ("Multi-cannon", "multicannon"),
    ("  Auto Field-Maintenance Unit ", "autofieldmaintenanceunit"),
    ("", ""), (None, ""),
])
def test_squash_keeps_only_letters_and_digits_in_lower_case(text, expected):
    assert bm.squash(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Increased range", {"increased", "range"}),
    ("LongRange", {"long", "range"}),                  # the part of FSD_LongRange after the underscore
    ("FastBoot", {"fast", "boot"}),
    ("Increased FSD Range", {"increased", "fsd", "range"}),
    ("", set()), (None, set()),
])
def test_name_tokens_split_words_and_camel_case(text, expected):
    assert bm.name_tokens(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Conductive Polymers (0)", "Conductive Polymers"),
    ("Flawed Focus Crystals (100)", "Flawed Focus Crystals"),
    ("Precipitated Alloys (87)", "Precipitated Alloys"),
    ("Carbon", "Carbon"),
    ("Modified Embedded Firmware", "Modified Embedded Firmware"),
    ("Merc Coin", "Merc Coin"),
    ("", ""), (None, ""),
])
def test_clean_material_name_drops_only_a_trailing_count_in_brackets(text, expected):
    assert bm.clean_material_name(text) == expected


def test_the_synonym_and_alias_tables_are_well_formed():
    for name, grp in bm.GROUP_SYNONYMS.items():
        assert name == name.lower() and name.strip() == name, name
        assert grp and grp == grp.lower(), name
    assert bm.GROUP_SYNONYMS["armour"] == "bh" and bm.GROUP_SYNONYMS["fsd"] == "fsd"
    for fdname, aliases in bm.BLUEPRINT_ALIASES.items():
        assert "_" in fdname and isinstance(aliases, tuple) and aliases
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest tests/test_blueprint_mapping.py -q` — Expected: FAIL (`AttributeError: module 'blueprint_mapping' has no attribute 'squash'`).

- [ ] **Step 3: Implement** — in `blueprint_mapping.py` add `import re` on its own line directly above `MODULE_TO_CORIOLIS_KEYWORDS = {`, and append at the end of the file:

```python


# ---------------------------------------------------------------------------
# Part 1 of "engineering depth": names. Everything below is pure (no database).
# ---------------------------------------------------------------------------

# Module names the module files in coriolis-data cannot supply (those files name
# each group, e.g. 'frame_shift_drive' -> "frame shift drive"): abbreviations, a group
# that has no file (ship armour, 'bh'), and the wording players use. name -> group code.
# Names are compared with squash(), so case, spaces and hyphens do not matter.
GROUP_SYNONYMS = {
    "armour": "bh", "armor": "bh", "ship armour": "bh", "ship armor": "bh",
    "bulkheads": "bh", "bulkhead": "bh",
    "engines": "t", "main engines": "t", "drives": "t",
    "distributor": "pd",
    "fsd": "fsd",
    "afmu": "am",
    "hrp": "hr",
    "dss": "ss", "detailed surface scanner": "ss",
    "manifest scanner": "cs",
    "ecm": "ec", "electronic counter measures": "ec",
    "kws": "kw",
    "scb": "scb",
    "fsdi": "fi",
    "wake scanner": "ws",
    "prismatic shield generator": "psg",
}

# In-game blueprint names that share no words with Coriolis's own name for the
# blueprint, keyed by Coriolis fdname. Names that DO share the words ("Increased FSD
# Range" for "Increased range") need no entry: the matcher's word rule finds them.
# An alias is added only once the owner has seen the name in the game.
BLUEPRINT_ALIASES = {
    "Engine_Reinforced": ("Drive Strengthening",),
}


def squash(text):
    """Lower case, letters and digits only: 'Multi-cannon' and 'multi cannon' compare equal."""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def name_tokens(text):
    """The lower-case words in a name, splitting camelCase: 'LongRange' -> {'long', 'range'}."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return set(re.findall(r"[a-z0-9]+", spaced.lower()))


def clean_material_name(text):
    """A material name without the stray count the sheet sometimes leaves in brackets:
    'Conductive Polymers (0)' -> 'Conductive Polymers'."""
    return re.sub(r"\s*\(\d+\)\s*$", "", text or "").strip()
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_blueprint_mapping.py -q` — Expected: all pass.

- [ ] **Step 5: Owner check (do not block on it)** — add a line to the SDD ledger or tell the owner: "In the game, what is the thrusters 'Reinforced' blueprint called? If it is not 'Drive Strengthening', change `BLUEPRINT_ALIASES`." Also ask whether any other in-game blueprint name shares no words with Coriolis's (candidates to look at: the armour blueprints).

- [ ] **Step 6: Commit**

```bash
git add blueprint_mapping.py tests/test_blueprint_mapping.py
git commit -m "Add name helpers, group synonyms and a blueprint alias to blueprint_mapping" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Module resolver and word-based blueprint matching

**Files:**
- Modify: `query_functions.py` (imports; new `_resolve_module_groups`; `_blueprint_name_matches`)
- Modify: `tests/conftest.py` (shared `coriolis_db` fixture; `NEEDS_CORIOLIS_DATA`)
- Create: `tests/test_blueprint_lookup.py`

**Interfaces:**
- Consumes: Task 1 tables; Task 2 helpers (`GROUP_SYNONYMS`, `BLUEPRINT_ALIASES`, `squash`, `name_tokens`).
- Produces: `_resolve_module_groups(conn, typed) -> list[str]` (sorted group codes; `[]` when unknown or blank). `_blueprint_name_matches(rows, name)` keeps its signature and its four rules, gains rule 5 (alias) and a final word rule. Fixture `coriolis_db` (function scope): a temporary database built from the real `coriolis-data` with the live-state, ship/module, group-name, allow-list, special and Coriolis blueprint tables and **no spreadsheet tables**, with `query_functions.DB_PATH` pointed at it; it returns the path.

- [ ] **Step 1: Add the shared fixture** — in `tests/conftest.py` add to the imports `import shutil` and, below `pytest_collection_modifyitems`, append:

```python
@pytest.fixture(scope="session")
def _coriolis_template(tmp_path_factory):
    """Built once per test run: the real coriolis-data in a database with no spreadsheet tables."""
    import json
    import sqlite3
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import ingest_coriolis as ic
    import ingest_ship_modules as ism
    import journal_watcher as jw

    repo = DATA_MARKER.parent.parent
    path = str(tmp_path_factory.mktemp("coriolis") / "template.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    ism.ingest_all(conn, str(repo))
    ic.build_schema(conn)
    ic.ingest_blueprints(conn, json.load(open(repo / "modifications" / "blueprints.json", encoding="utf-8")))
    ic.ingest_specials(conn, json.load(open(repo / "modifications" / "specials.json", encoding="utf-8")))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def coriolis_db(_coriolis_template, tmp_path, monkeypatch):
    """A private copy of the template, wired into query_functions. Returns its path."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import query_functions as qf

    path = str(tmp_path / "coriolis_only.db")
    shutil.copyfile(_coriolis_template, path)
    monkeypatch.setattr(qf, "DB_PATH", path)
    return path
```

and add `"test_blueprint_lookup.py"` to the `NEEDS_CORIOLIS_DATA` set.

- [ ] **Step 2: Write the failing tests** — create `tests/test_blueprint_lookup.py`:

```python
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.parametrize("typed, expected", [
    ("Frame Shift Drive", ["fsd"]), ("FSD", ["fsd"]), ("frame shift drive", ["fsd"]),
    ("FRAME-SHIFT DRIVE", ["fsd"]), ("  Frame   Shift Drive ", ["fsd"]),
    ("Thrusters", ["t"]), ("thruster", ["t"]), ("engines", ["t"]),
    ("Power Plant", ["pp"]), ("Power Distributor", ["pd"]), ("distributor", ["pd"]),
    ("Life Support", ["ls"]), ("Sensors", ["s"]), ("sensor", ["s"]),
    ("Shield Generator", ["sg"]), ("Armour", ["bh"]), ("bulkheads", ["bh"]),
    ("Beam Laser", ["bl"]), ("Multi-cannon", ["mc"]), ("Hull Reinforcement Package", ["hr"]), ("HRP", ["hr"]),
    ("Detailed Surface Scanner", ["ss"]), ("Manifest Scanner", ["cs"]),
    ("Collector Limpet Controller", ["cc"]), ("Cargo Rack", ["cr"]), ("Fuel Scoop", ["fs"]),
    ("Module Reinforcement Package", ["mrp"]),
    ("fsd", ["fsd"]),                                  # a raw group code
    ("xyzzy", []), ("", []), ("   ", []), (None, []),
])
def test_a_typed_module_name_resolves_to_its_group(coriolis_db, typed, expected):
    conn = _conn(coriolis_db)
    try:
        assert qf._resolve_module_groups(conn, typed) == expected
    finally:
        conn.close()


FSD_ROWS = [
    {"fdname": "FSD_FastBoot", "upgrade_name": "Faster boot sequence"},
    {"fdname": "FSD_LongRange", "upgrade_name": "Increased range"},
    {"fdname": "FSD_Shielded", "upgrade_name": "Shielded"},
]
ENGINE_ROWS = [
    {"fdname": "Engine_Dirty", "upgrade_name": "Dirty"},
    {"fdname": "Engine_Reinforced", "upgrade_name": "Reinforced"},
    {"fdname": "Engine_Tuned", "upgrade_name": "Clean"},
]


@pytest.mark.parametrize("typed, fdname", [
    ("Increased range", "FSD_LongRange"),               # rule 1, unchanged
    ("FSD_LongRange", "FSD_LongRange"),                  # rule 2, unchanged
    ("long_range", "FSD_LongRange"),                     # rule 3, unchanged
    ("increased-range", "FSD_LongRange"),                # rule 4, unchanged
    ("Increased FSD Range", "FSD_LongRange"),            # the word rule: every word of "Increased range" is there
    ("long range", "FSD_LongRange"),                     # the word rule on the fdname's own words
    ("Faster FSD Boot Sequence", "FSD_FastBoot"),
    ("Shielded FSD", "FSD_Shielded"),
])
def test_the_word_rule_finds_in_game_names(typed, fdname):
    assert [r["fdname"] for r in qf._blueprint_name_matches(FSD_ROWS, typed)] == [fdname]


def test_the_word_rule_reads_clean_and_dirty_drive_tuning():
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Dirty Drive Tuning")] == ["Engine_Dirty"]
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Clean Drive Tuning")] == ["Engine_Tuned"]


def test_an_alias_covers_a_name_that_shares_no_words():
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Drive Strengthening")] == ["Engine_Reinforced"]


def test_the_match_with_the_most_words_wins():
    rows = [{"fdname": "A_Reinforced", "upgrade_name": "Reinforced"},
            {"fdname": "A_HeavyDuty", "upgrade_name": "Heavy duty"}]
    assert [r["fdname"] for r in qf._blueprint_name_matches(rows, "heavy duty reinforced thing")] == ["A_HeavyDuty"]


@pytest.mark.parametrize("typed", ["Turbo", "", "   ", "the", "grade"])
def test_a_name_with_none_of_the_words_matches_nothing(typed):
    assert qf._blueprint_name_matches(FSD_ROWS, typed) == []


def test_an_earlier_rule_always_beats_the_word_rule():
    rows = [{"fdname": "X_Shielded", "upgrade_name": "Shielded"},
            {"fdname": "X_ShieldedPlus", "upgrade_name": "Shielded plus"}]
    assert [r["fdname"] for r in qf._blueprint_name_matches(rows, "Shielded plus")] == ["X_ShieldedPlus"]
```

- [ ] **Step 3: Run to see them fail** — `python -m pytest tests/test_blueprint_lookup.py -q` — Expected: FAIL (`AttributeError: ... _resolve_module_groups`; the word-rule cases fail).

- [ ] **Step 4: Implement** — in `query_functions.py`:

Directly under the existing `from blueprint_mapping import MODULE_TO_CORIOLIS_KEYWORDS` line add:

```python
from blueprint_mapping import BLUEPRINT_ALIASES, GROUP_SYNONYMS, clean_material_name, name_tokens, squash
```

Insert directly above `def _materials_to_dict`:

```python
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
    return [key] if key in known else []


```

Replace the whole `_blueprint_name_matches` function with:

```python
def _blueprint_name_matches(rows, name):
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
         'Increased range'); the matches with the MOST words win.
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
    scored = []
    for r in rows:
        sizes = [len(words) for words in (name_tokens(r["upgrade_name"]), name_tokens(tail(r["fdname"]) or ""))
                 if words and words <= typed]
        if sizes:
            scored.append((max(sizes), r))
    if not scored:
        return []
    best = max(size for size, _ in scored)
    return [r for size, r in scored if size == best]
```

- [ ] **Step 5: Run the tests** — `python -m pytest tests/test_blueprint_lookup.py -q` then the whole suite `python -m pytest tests -q`. Expected: all pass (the word rule only fires when rules 1-5 find nothing, so the swap simulator's matches are unchanged; if an existing swap test now fails, the new rule matched a name that used to miss. Read the failing test before changing anything.)

- [ ] **Step 6: Commit**

```bash
git add query_functions.py tests/conftest.py tests/test_blueprint_lookup.py
git commit -m "Resolve typed module names to groups; match in-game blueprint names by their words" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `get_blueprint` through the allow-list, with honest unknowns

**Files:**
- Modify: `query_functions.py` (import line; replace `_materials_to_dict` and `get_blueprint`; new helpers)
- Modify: `tests/test_blueprint_lookup.py`

**Interfaces:**
- Consumes: Task 3's `_resolve_module_groups`, `_blueprint_name_matches`; the `coriolis_db` fixture.
- Produces:
  - `get_blueprint(module_name, upgrade_name, grade) -> dict`. Found: `{"found": True, "source": "excel"|"coriolis", "module", "upgrade", "grade", "materials": {name: qty}, "materials_per": "roll", "cost_known": bool, "effect": str|None, "summary": str}` plus `"other_requirements": [str]` only when the sheet lists a line with no quantity. Not found: `{"found": False, "module", "upgrade", "grade", "reason": str}` plus `"available_upgrades": [str]` or `"available_grades": [int]` when relevant.
  - `_split_materials(materials_json) -> (dict, list)`; `_coriolis_cost(conn, fdname, grade) -> {"upgrade_name", "materials", "extras"} | None`; `_sheet_rows`, `_sheet_module_names(conn, table, module_name, groups)`, `_group_label(conn, grp)`, `_in_marks(values)`, `_grade_span(grades)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_blueprint_lookup.py`:

```python
import json

import ingest_excel as ie


def _add_sheet(path, blueprint_rows=(), cost_rows=()):
    """Create the spreadsheet tables in a test database and fill them."""
    conn = sqlite3.connect(path)
    ie.build_schema(conn)
    for row in blueprint_rows:
        conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)", row)
    for row in cost_rows:
        conn.execute("INSERT INTO ungraded_costs VALUES (?,?,?,?,?)", row)
    conn.commit()
    conn.close()


def test_a_database_with_no_spreadsheet_tables_still_answers_from_coriolis(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 5)
    assert r["found"] is True and r["source"] == "coriolis"
    assert r["materials"] == {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}
    assert r["materials_per"] == "roll" and r["cost_known"] is True and r["grade"] == 5
    assert r["summary"] == ("Frame Shift Drive, Increased range, grade 5, per roll: "
                            "1 Arsenic, 1 Chemical Manipulators, 1 Datamined Wake Exceptions.")


def test_the_player_s_words_work_for_the_module_and_the_blueprint(coriolis_db):
    a = qf.get_blueprint("FSD", "Increased FSD Range", 5)
    b = qf.get_blueprint("frame shift drive", "FSD_LongRange", "5")
    assert a["materials"] == b["materials"] == {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}
    t = qf.get_blueprint("Thrusters", "Dirty Drive Tuning", 3)
    assert t["materials"] == {"Chromium": 1, "Mechanical Components": 1, "Specialised Legacy Firmware": 1}
    assert qf.get_blueprint("Thrusters", "Drive Strengthening", 1)["upgrade"] == "Reinforced"


def test_an_unrecorded_cost_is_not_reported_as_free(coriolis_db):
    r = qf.get_blueprint("Cargo Rack", "Expanded Capacity", 5)
    assert r["found"] is True and r["materials"] == {} and r["cost_known"] is False
    assert r["summary"] == "Cargo Rack, Expanded Capacity, grade 5: no materials are recorded for this upgrade, so its cost is not known."


def test_a_grade_the_blueprint_does_not_have_names_the_grades_it_does(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 6)
    assert r["found"] is False and r["available_grades"] == [1, 2, 3, 4, 5]
    assert r["reason"] == "Increased range for Frame Shift Drive is offered at grades 1-5, not grade 6."
    assert "offered at grades 1-4, not grade 5" in qf.get_blueprint("Shield Cell Bank", "Rapid charge", 5)["reason"]
    assert qf.get_blueprint("Frame Shift Drive", "Increased range", 0)["reason"].endswith("not grade 0.")


@pytest.mark.parametrize("grade", ["five", None, "", [], {}])
def test_a_grade_that_is_not_a_number_gets_a_plain_reply(coriolis_db, grade):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", grade)
    assert r["found"] is False and r["reason"] == "The grade must be a number from 1 to 5."


def test_a_blueprint_that_belongs_to_another_module_says_where_it_applies(coriolis_db):
    r = qf.get_blueprint("Thrusters", "Increased range", 3)
    assert r["found"] is False
    assert r["reason"] == ("'Increased range' doesn't apply to Thrusters; it applies to Frame Shift Drive. "
                           "Blueprints available for Thrusters: Clean, Dirty, Reinforced.")
    assert r["available_upgrades"] == ["Clean", "Dirty", "Reinforced"]


def test_an_unknown_blueprint_for_a_known_module_lists_the_real_ones(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Turbo", 2)
    assert r["found"] is False and r["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    assert r["reason"] == ("Ellie doesn't know a blueprint called 'Turbo'. "
                           "Blueprints available for Frame Shift Drive: Faster boot sequence, Increased range, Shielded.")


def test_an_unknown_module_and_a_module_with_no_blueprints(coriolis_db):
    r = qf.get_blueprint("xyzzy", "Dirty", 1)
    assert r["found"] is False and r["reason"].startswith("Ellie doesn't know a module called 'xyzzy'. Core modules she knows:")
    r = qf.get_blueprint("Module Reinforcement Package", "Dirty", 1)
    assert r["found"] is False and r["reason"] == "Module Reinforcement Package has no engineering blueprints in Ellie's data."


@pytest.mark.parametrize("module, upgrade", [("", ""), ("   ", "Dirty"), ("Thrusters", ""), (None, None), (5, 7)])
def test_blank_or_odd_arguments_give_a_clean_reply_not_an_exception(coriolis_db, module, upgrade):
    r = qf.get_blueprint(module, upgrade, 1)
    assert r["found"] is False and isinstance(r["reason"], str) and r["reason"]


def test_every_coriolis_blueprint_and_grade_is_reachable_by_name(coriolis_db):
    """The reachability test from the spec: every (group, blueprint, grade) in Coriolis is found
    by the module's own name, three ways of typing the blueprint, and has Coriolis's cost."""
    conn = _conn(coriolis_db)
    failures = []
    checked = 0
    try:
        groups = [r[0] for r in conn.execute("SELECT DISTINCT grp FROM module_blueprints ORDER BY grp")]
        for grp in groups:
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (grp,)).fetchone()
                      or {"name": {"bh": "armour"}.get(grp, grp)})["name"]
            for (fdname,) in conn.execute("SELECT fdname FROM module_blueprints WHERE grp = ?", (grp,)).fetchall():
                rows = conn.execute("SELECT upgrade_name, grade, materials_json FROM coriolis_blueprints WHERE fdname = ?", (fdname,)).fetchall()
                if not rows:
                    failures.append((grp, fdname, "in modules.json but not in blueprints.json"))
                    continue
                for row in rows:
                    for typed in (row["upgrade_name"], f"{row['upgrade_name']} {module}", fdname):
                        checked += 1
                        got = qf.get_blueprint(module, typed, row["grade"])
                        want = json.loads(row["materials_json"])
                        if not got["found"] or got["materials"] != want:
                            failures.append((grp, fdname, row["grade"], typed, got.get("reason") or got.get("materials")))
    finally:
        conn.close()
    assert checked > 1500
    assert failures == []


def test_the_spreadsheet_row_wins_when_it_has_the_module_upgrade_and_grade(coriolis_db):
    _add_sheet(coriolis_db, blueprint_rows=[(
        "core_internal", "Frame Shift Drive", "Increased range", 5,
        json.dumps([{"qty": 2, "item": "Arsenic"}]), "More jump range")])
    r = qf.get_blueprint("FSD", "Increased FSD Range", 5)         # found through a different spelling of both names
    assert r["source"] == "excel" and r["materials"] == {"Arsenic": 2} and r["effect"] == "More jump range"
    assert qf.get_blueprint("FSD", "Increased range", 4)["source"] == "coriolis"      # no sheet row for grade 4


def test_sheet_annotations_never_show_and_a_line_without_a_quantity_is_reported_apart(coriolis_db):
    _add_sheet(coriolis_db, blueprint_rows=[(
        "optional_internal", "Fuel Scoop", "Shielded", 3,
        json.dumps([{"qty": 1, "item": "Conductive Polymers (0)"}, {"qty": 5, "item": "Merc Coin"},
                    {"qty": None, "item": "Needs personal research"}]), None)])
    r = qf.get_blueprint("Fuel Scoop", "Shielded", 3)
    assert r["source"] == "excel" and r["materials"] == {"Conductive Polymers": 1, "Merc Coin": 5}
    assert r["other_requirements"] == ["Needs personal research"]
    assert r["summary"] == ("Fuel Scoop, Shielded, grade 3, per roll: 1 Conductive Polymers, 5 Merc Coin; "
                            "also listed: Needs personal research.")


def test_a_database_from_before_this_change_gets_the_setup_message(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    conn.execute("DROP TABLE module_group_names")
    conn.commit()
    conn.close()
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 5)
    assert r["found"] is False and "python setup.py" in r["reason"]
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_blueprint_lookup.py -q` — Expected: the new tests FAIL (the old `get_blueprint` returns the "not fully set up" reply because the sheet table is missing; the word-based reply strings differ).

- [ ] **Step 3: Implement** — in `query_functions.py`:

Replace the line `from blueprint_mapping import MODULE_TO_CORIOLIS_KEYWORDS` with nothing (delete it) — Task 3 already added the line that imports what is needed.

Delete `_materials_to_dict` and the whole old `get_blueprint` function (keep `@_needs_setup`-decorated functions that follow). Put this block where they were:

```python
_MODULE_HINT = ("Core modules she knows: frame shift drive, thrusters, power plant, power distributor, "
                "life support, sensors, shield generator, armour; weapons, utilities and optional "
                "internals by their usual names.")


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
    hits = _blueprint_name_matches(candidates, upgrade_name)
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
        if not groups:
            return {"found": False, **base, "reason": f"Ellie doesn't know a module called '{module_name}'. {_MODULE_HINT}"}
        allowed = [r["fdname"] for r in conn.execute(
            f"SELECT DISTINCT fdname FROM module_blueprints WHERE grp IN ({_in_marks(groups)})", groups)]
        known = conn.execute(
            f"SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints WHERE fdname IN ({_in_marks(allowed)})", allowed).fetchall() if allowed else []
        if not known:
            return {"found": False, **base, "reason": f"{module_name} has no engineering blueprints in Ellie's data."}
        offered = sorted({r["upgrade_name"] for r in known})
        hits = _blueprint_name_matches(known, upgrade_name)
        if len(hits) > 1:
            names = sorted({h["upgrade_name"] for h in hits})
            return {"found": False, **base, "available_upgrades": offered,
                    "reason": f"'{upgrade_name}' could mean more than one blueprint for {module_name}: {', '.join(names)}. Ask which one."}
        if not hits:
            return {"found": False, **base, "available_upgrades": offered,
                    "reason": _unknown_upgrade_reason(conn, module_name, upgrade_name, offered)}
        fdname = hits[0]["fdname"]
        grades = sorted(r["grade"] for r in conn.execute("SELECT grade FROM coriolis_blueprints WHERE fdname = ?", (fdname,)))
        if grade not in grades:
            plural = "s" if len(grades) > 1 else ""
            return {"found": False, **base, "available_grades": grades,
                    "reason": f"{hits[0]['upgrade_name']} for {module_name} is offered at grade{plural} {_grade_span(grades)}, not grade {grade}."}
        cost = _coriolis_cost(conn, fdname, grade)
        return _blueprint_reply(base, "coriolis", cost["upgrade_name"], cost["materials"], cost["extras"], None)
    finally:
        conn.close()
```

`missing_materials` and `get_experimental_effect` still call `_materials_to_dict`; keep them working until Tasks 5 and 6 replace them, by adding this temporary shim below the block above (Tasks 5 and 6 delete it):

```python
def _materials_to_dict(materials_json):
    """Removed in Task 6; kept so missing_materials and get_experimental_effect keep working until then."""
    return _split_materials(materials_json)[0]
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_blueprint_lookup.py -q`, then `python -m pytest tests -q`. Expected: all pass. If the reachability test fails, its failure list shows which group, blueprint and typing style missed: fix the data-driven cause (a missing synonym in `GROUP_SYNONYMS`), not the test.

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_blueprint_lookup.py
git commit -m "get_blueprint: resolve through the allow-list, work without the sheet, say when a cost is unknown" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Held materials by the name the game shows

**Files:**
- Modify: `query_functions.py` (`_held_count`, `get_material_count`, `missing_materials`)
- Create: `tests/test_held_materials.py`, `tests/fixtures/held_material_names.json`
- Modify: `tests/test_blueprint_lookup.py`

**Interfaces:**
- Consumes: Task 4's `get_blueprint`, `_split_materials`.
- Produces: `_held_count(conn, name) -> sqlite3.Row | None` (columns `material_name, display_name, category, count`; the stored key wins over a display-name match). `get_material_count` is unchanged in shape. `missing_materials` keeps its keys (`found, module, upgrade, grade, have_everything, missing, required, source`) and adds `cost_known`, `materials_per`, `summary`, plus `other_requirements` when present; when the cost is unknown `have_everything` is `None`.

- [ ] **Step 1: Create the fixture** — `tests/fixtures/held_material_names.json`, the 34 material types the journal stores under a different key than the game's name (stored key, name shown, category), copied from the owner's inventory:

```json
[["adaptiveencryptors", "Adaptive Encryptors Capture", "Encoded"], ["ancientculturaldata", "Pattern Beta Obelisk Data", "Encoded"], ["ancienthistoricaldata", "Pattern Gamma Obelisk Data", "Encoded"], ["archivedemissiondata", "Irregular Emission Data", "Encoded"], ["bulkscandata", "Anomalous Bulk Scan Data", "Encoded"], ["compactemissionsdata", "Abnormal Compact Emissions Data", "Encoded"], ["consumerfirmware", "Modified Consumer Firmware", "Encoded"], ["disruptedwakeechoes", "Atypical Disrupted Wake Echoes", "Encoded"], ["emissiondata", "Unexpected Emission Data", "Encoded"], ["encryptedfiles", "Unusual Encrypted Files", "Encoded"], ["encryptionarchives", "Atypical Encryption Archives", "Encoded"], ["encryptioncodes", "Tagged Encryption Codes", "Encoded"], ["fsdtelemetry", "Anomalous FSD Telemetry", "Encoded"], ["guardian_moduleblueprint", "Guardian Module Blueprint Fragment", "Encoded"], ["guardian_weaponblueprint", "Guardian Weapon Blueprint Fragment", "Encoded"], ["hyperspacetrajectories", "Eccentric Hyperspace Trajectories", "Encoded"], ["industrialfirmware", "Cracked Industrial Firmware", "Encoded"], ["legacyfirmware", "Specialised Legacy Firmware", "Encoded"], ["scanarchives", "Unidentified Scan Archives", "Encoded"], ["scandatabanks", "Classified Scan Databanks", "Encoded"], ["scrambledemissiondata", "Exceptional Scrambled Emission Data", "Encoded"], ["shieldcyclerecordings", "Distorted Shield Cycle Recordings", "Encoded"], ["shielddensityreports", "Untypical Shield Scans", "Encoded"], ["shieldfrequencydata", "Peculiar Shield Frequency Data", "Encoded"], ["shieldpatternanalysis", "Aberrant Shield Pattern Analysis", "Encoded"], ["shieldsoakanalysis", "Inconsistent Shield Soak Analysis", "Encoded"], ["symmetrickeys", "Open Symmetric Keys", "Encoded"], ["wakesolutions", "Strange Wake Solutions", "Encoded"], ["fedcorecomposites", "Core Dynamics Composites", "Manufactured"], ["fedproprietarycomposites", "Proprietary Composites", "Manufactured"], ["guardian_powerconduit", "Guardian Power Conduit", "Manufactured"], ["guardian_sentinel_weaponparts", "Guardian Sentinel Weapon Parts", "Manufactured"], ["guardian_sentinel_wreckagecomponents", "Guardian Wreckage Components", "Manufactured"], ["uncutfocuscrystals", "Flawed Focus Crystals", "Manufactured"]]
```

- [ ] **Step 2: Write the failing tests** — create `tests/test_held_materials.py` (needs no Coriolis data):

```python
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw
import query_functions as qf

PAIRS = json.load(open(Path(__file__).resolve().parent / "fixtures" / "held_material_names.json", encoding="utf-8"))


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    path = str(tmp_path / "held.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    for i, (key, shown, category) in enumerate(PAIRS):
        conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", (key, category, shown, i + 1, "2026-11-06T00:00:00Z"))
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("iron", "Raw", "Iron", 40, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(qf, "DB_PATH", path)
    return path


def test_all_34_material_types_the_old_lookup_missed_are_found_by_the_name_the_game_shows(inventory):
    assert len(PAIRS) == 34
    for i, (key, shown, category) in enumerate(PAIRS):
        r = qf.get_material_count(shown)
        assert r == {"found": True, "material_name": shown, "category": category, "count": i + 1}, shown


def test_the_journal_s_own_key_and_loose_spellings_work_too(inventory):
    assert qf.get_material_count("disruptedwakeechoes")["count"] == 8      # position 8 in the fixture
    assert qf.get_material_count("ATYPICAL disrupted-wake echoes")["material_name"] == "Atypical Disrupted Wake Echoes"
    assert qf.get_material_count("iron")["count"] == 40 and qf.get_material_count("Iron")["count"] == 40


def test_a_material_never_held_is_not_found_and_does_not_raise(inventory):
    assert qf.get_material_count("Unobtainium") == {"found": False, "material_name": "Unobtainium"}
    assert qf.get_material_count("")["found"] is False


def test_the_stored_key_wins_over_a_display_name_that_squashes_the_same(inventory):
    conn = sqlite3.connect(inventory)
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("zinc", "Raw", "Zinc", 7, "2026-11-06T00:00:00Z"))
    conn.execute("INSERT INTO materials_inventory VALUES (?,?,?,?,?)", ("other", "Raw", "Z-inc", 99, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()
    assert qf.get_material_count("zinc")["count"] == 7
```

Append to `tests/test_blueprint_lookup.py`:

```python
def _hold(path, key, shown, count, category="Encoded"):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES (?,?,?,?,?)", (key, category, shown, count, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()


def test_missing_materials_sees_what_the_commander_holds_under_the_game_s_name(coriolis_db):
    _hold(coriolis_db, "disruptedwakeechoes", "Atypical Disrupted Wake Echoes", 64)       # the bug: this used to read as 0
    r = qf.missing_materials("Frame Shift Drive", "Increased range", 1)
    assert r["found"] is True and r["have_everything"] is True and r["missing"] == {}
    assert r["summary"] == "You have everything for Frame Shift Drive, Increased range, grade 1 (per roll)."


def test_missing_materials_names_what_is_short_and_how_much(coriolis_db):
    _hold(coriolis_db, "chemicalprocessors", "Chemical Processors", 0, "Manufactured")
    r = qf.missing_materials("Frame Shift Drive", "Increased range", 2)               # 1 Atypical Disrupted Wake Echoes + 1 Chemical Processors
    assert r["have_everything"] is False
    assert r["missing"] == {"Atypical Disrupted Wake Echoes": {"need": 1, "have": 0, "short_by": 1},
                            "Chemical Processors": {"need": 1, "have": 0, "short_by": 1}}
    assert r["summary"] == ("For Frame Shift Drive, Increased range, grade 2 (per roll) you are short by: "
                            "1 Atypical Disrupted Wake Echoes (have 0), 1 Chemical Processors (have 0).")


def test_missing_materials_does_not_call_an_unrecorded_cost_nothing_missing(coriolis_db):
    r = qf.missing_materials("Cargo Rack", "Expanded Capacity", 5)
    assert r["found"] is True and r["cost_known"] is False and r["have_everything"] is None and r["missing"] == {}
    assert "no materials are recorded" in r["summary"]


def test_missing_materials_carries_a_blueprint_miss_through(coriolis_db):
    r = qf.missing_materials("Frame Shift Drive", "Turbo", 2)
    assert r["found"] is False and r["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    assert r["module"] == "Frame Shift Drive" and r["upgrade"] == "Turbo" and r["grade"] == 2
    assert qf.missing_materials("Frame Shift Drive", "Increased range", "five")["reason"] == "The grade must be a number from 1 to 5."
```

- [ ] **Step 3: Run to see them fail** — `python -m pytest tests/test_held_materials.py tests/test_blueprint_lookup.py -q` — Expected: FAIL (the key lookup misses display names; no `summary` yet).

- [ ] **Step 4: Implement** — in `query_functions.py` add above `get_material_count`:

```python
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
```

Replace the body of `get_material_count` with:

```python
    conn = _connect()
    try:
        row = _held_count(conn, material_name)
    finally:
        conn.close()
    if not row:
        return {"found": False, "material_name": material_name}
    return {"found": True, "material_name": row["display_name"], "category": row["category"], "count": row["count"]}
```
(keeping its docstring and `@_needs_setup`). Replace the whole `missing_materials` function with:

```python
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
        summary = f"You have everything for {label} (per roll)."
    else:
        summary = (f"For {label} (per roll) you are short by: "
                   + ", ".join(f"{m['short_by']} {name} (have {m['have']})" for name, m in missing.items()) + ".")
    if shared.get("other_requirements"):
        summary += f" Also listed: {', '.join(shared['other_requirements'])}."
    return {**shared, "have_everything": have_enough, "missing": missing, "summary": summary}
```

- [ ] **Step 5: Run the tests** — `python -m pytest tests/test_held_materials.py tests/test_blueprint_lookup.py -q`, then `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add query_functions.py tests/test_held_materials.py tests/test_blueprint_lookup.py tests/fixtures/held_material_names.json
git commit -m "Match held materials by the name the game shows; missing_materials says what is short" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Experimental effects: applicability and the same name handling

**Files:**
- Modify: `query_functions.py` (`get_experimental_effect`, new helpers; delete the `_materials_to_dict` shim)
- Modify: `tests/test_blueprint_lookup.py`

**Interfaces:**
- Consumes: Task 4 helpers (`_split_materials`, `_sheet_rows`, `_sheet_module_names`, `_group_label`, `_in_marks`, `_MODULE_HINT`), Task 3 matcher, the `module_specials` table.
- Produces: `get_experimental_effect(module_name, effect_name) -> dict`. Found: `{"found": True, "source", "module", "effect_name", "materials", "materials_per": "application", "cost_known", "description", "summary"}` (plus `other_requirements` if any). Not found: `{"found": False, "module", "effect_name", "reason"}` plus `"available_effects"` when relevant. `_special_cost(conn, edname) -> {"name", "description", "materials", "extras"} | None` (case-insensitive on `edname`), used by `verify_blueprints.py`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_blueprint_lookup.py`:

```python
def test_an_effect_that_fits_the_module_returns_its_cost_per_application(coriolis_db):
    r = qf.get_experimental_effect("Frame Shift Drive", "Mass Manager")
    assert r["found"] is True and r["source"] == "coriolis"
    assert r["materials"] == {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}
    assert r["materials_per"] == "application" and r["cost_known"] is True
    assert r["summary"] == ("Frame Shift Drive, Mass Manager, each time it is applied: "
                            "5 Atypical Disrupted Wake Echoes, 3 Galvanising Alloys, 1 Eccentric Hyperspace Trajectories.")


def test_an_effect_the_module_cannot_take_is_refused_and_says_where_it_fits(coriolis_db):
    r = qf.get_experimental_effect("Thrusters", "Mass Manager")
    assert r["found"] is False
    assert r["reason"] == ("'Mass Manager' can't be applied to Thrusters; it applies to Frame Shift Drive. "
                           "Experimental effects available for Thrusters: "
                           "Double Braced, Drag Drives, Drive Distributors, Stripped Down, Thermal Spread.")
    assert r["available_effects"] == ["Double Braced", "Drag Drives", "Drive Distributors", "Stripped Down", "Thermal Spread"]


def test_the_same_effect_name_means_the_right_effect_for_each_module(coriolis_db):
    weapon = qf.get_experimental_effect("Cannon", "Stripped Down")
    engine = qf.get_experimental_effect("Thrusters", "Stripped Down")
    assert weapon["found"] and engine["found"] and weapon["materials"] != engine["materials"]


def test_an_unknown_effect_unknown_module_and_a_module_without_effects(coriolis_db):
    r = qf.get_experimental_effect("Frame Shift Drive", "Turbo")
    assert r["found"] is False and r["reason"].startswith("Ellie doesn't know an experimental effect called 'Turbo'. Experimental effects available for Frame Shift Drive:")
    assert qf.get_experimental_effect("xyzzy", "Mass Manager")["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
    assert qf.get_experimental_effect("Module Reinforcement Package", "Mass Manager")["reason"] == "Module Reinforcement Package has no experimental effects in Ellie's data."
    assert qf.get_experimental_effect("", "")["found"] is False


def test_every_effect_coriolis_lists_for_a_group_is_reachable_by_name(coriolis_db):
    conn = _conn(coriolis_db)
    failures, checked = [], 0
    try:
        for row in conn.execute("SELECT grp, edname FROM module_specials ORDER BY grp, edname").fetchall():
            effect = conn.execute("SELECT name, materials_json FROM coriolis_experimental_effects WHERE lower(edname) = lower(?)", (row["edname"],)).fetchone()
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (row["grp"],)).fetchone() or {"name": row["grp"]})["name"]
            if effect is None:
                failures.append((row["grp"], row["edname"], "listed for the group but missing from specials.json"))
                continue
            checked += 1
            got = qf.get_experimental_effect(module, effect["name"])
            if not got["found"] or got["materials"] != json.loads(effect["materials_json"]):
                failures.append((row["grp"], row["edname"], got.get("reason") or got.get("materials")))
    finally:
        conn.close()
    assert checked > 100
    assert failures == []


def test_a_spreadsheet_effect_row_is_used_first_and_its_annotations_are_cleaned(coriolis_db):
    _add_sheet(coriolis_db, cost_rows=[(
        "core_internal_experimental", "Frame Shift Drive", "Mass Manager",
        json.dumps([{"qty": 5, "item": "Atypical Disrupted Wake Echoes (3)"}]), "Heavier ship")])
    r = qf.get_experimental_effect("FSD", "mass manager")
    assert r["source"] == "excel" and r["materials"] == {"Atypical Disrupted Wake Echoes": 5} and r["description"] == "Heavier ship"
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_blueprint_lookup.py -q -k "effect or experimental"` — Expected: FAIL.

- [ ] **Step 3: Implement** — in `query_functions.py` delete the `_materials_to_dict` shim added in Task 4, and replace the whole old `get_experimental_effect` with:

```python
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
    hits = _blueprint_name_matches(candidates, effect_name)
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
        hits = _blueprint_name_matches([{"fdname": r["edname"], "upgrade_name": r["name"]} for r in rows], effect_name)
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
```

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_blueprint_lookup.py -q`, then `python -m pytest tests -q`. Expected: all pass. (If the "Thrusters" effect list in the refusal test differs from `Double Braced, Drag Drives, Drive Distributors, Stripped Down, Thermal Spread`, it means Coriolis changed: fix the test's expected text from the actual data after checking `modules.json`'s `t.specials`.)

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_blueprint_lookup.py
git commit -m "get_experimental_effect: check the module can take the effect; same name handling and honest unknowns" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Sheet hygiene and a cross-check that covers core modules

**Files:**
- Modify: `ingest_excel.py` (`parse_materials`)
- Modify: `cross_check.py` (rewrite `main` around the new resolver; add suspect-name report)
- Modify: `blueprint_mapping.py` (delete `MODULE_TO_CORIOLIS_KEYWORDS`)
- Create: `tests/test_ingest_excel.py`, `tests/test_cross_check.py`
- Modify: `tests/conftest.py` (`NEEDS_CORIOLIS_DATA` gets `test_cross_check.py`)

**Interfaces:**
- Consumes: `clean_material_name`; `qf._resolve_module_groups`, `qf._blueprint_name_matches`, `qf._coriolis_cost`, `qf._split_materials`, `qf._in_marks`.
- Produces: `ingest_excel.parse_materials` (same shape; item names lose a trailing `(n)`); `cross_check.compare_row(conn, row) -> (status, detail)` with `status` in `"match" | "mismatch" | "no_coriolis_row" | "no_reference"`; `cross_check.suspect_material_names(conn) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_excel.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_excel as ie


def test_a_bracketed_count_after_a_material_name_is_dropped():
    cell = "1 Conductive Polymers (0)\n100 Flawed Focus Crystals (100)\n5 Merc Coin"
    assert ie.parse_materials(cell) == [
        {"qty": 1, "item": "Conductive Polymers"},
        {"qty": 100, "item": "Flawed Focus Crystals"},
        {"qty": 5, "item": "Merc Coin"},
    ]


def test_a_line_without_a_quantity_is_kept_verbatim_with_no_quantity():
    assert ie.parse_materials("Needs personal research") == [{"qty": None, "item": "Needs personal research"}]


def test_empty_and_placeholder_cells_stay_empty():
    assert ie.parse_materials(None) == [] and ie.parse_materials("-") == [] and ie.parse_materials("  ") == []
```

`tests/test_cross_check.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cross_check as cc
import ingest_excel as ie


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ie.build_schema(conn)
    return conn


def _row(conn, module, upgrade, grade, items, category="core_internal"):
    conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)", (category, module, upgrade, grade, json.dumps(items), None))
    return conn.execute("SELECT category, module_name, upgrade, grade, materials_json FROM graded_blueprints ORDER BY rowid DESC LIMIT 1").fetchone()


def test_a_core_module_row_is_compared_against_coriolis(coriolis_db):
    conn = _db(coriolis_db)
    same = _row(conn, "Frame Shift Drive", "Increased range", 5,
                [{"qty": 1, "item": "Arsenic"}, {"qty": 1, "item": "Chemical Manipulators"}, {"qty": 1, "item": "Datamined Wake Exceptions"}])
    assert cc.compare_row(conn, same)[0] == "match"
    differs = _row(conn, "Frame Shift Drive", "Increased range", 5, [{"qty": 2, "item": "Arsenic"}])
    status, detail = cc.compare_row(conn, differs)
    assert status == "mismatch" and detail["coriolis"] == {"arsenic": 1, "chemical manipulators": 1, "datamined wake exceptions": 1}
    assert cc.compare_row(conn, _row(conn, "Frame Shift Drive", "Turbo", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_coriolis_row"
    assert cc.compare_row(conn, _row(conn, "Guardian FSD Booster", "Shielded", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_reference"
    assert cc.compare_row(conn, _row(conn, "Nonexistent", "Dirty", 1, [{"qty": 1, "item": "Carbon"}]))[0] == "no_reference"
    conn.close()


def test_names_no_other_source_knows_are_listed_for_the_owner(coriolis_db):
    conn = _db(coriolis_db)
    _row(conn, "Fuel Scoop", "Shielded", 3, [{"qty": 1, "item": "Carbon"}, {"qty": 5, "item": "Merc Coin"},
                                              {"qty": 1, "item": "Adaptive Encyptors Capture"}, {"qty": None, "item": "Needs personal research"}])
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES ('merccoin','Other','Merc Coin',3,'2026-11-06T00:00:00Z')")
    suspects = cc.suspect_material_names(conn)
    assert "Adaptive Encyptors Capture" in suspects and "Needs personal research" in suspects
    assert "Carbon" not in suspects                       # Coriolis uses it
    assert "Merc Coin" not in suspects                    # the journal shows the commander holds it
    conn.close()
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_ingest_excel.py tests/test_cross_check.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement the ingest cleaning** — in `ingest_excel.py` add `from blueprint_mapping import clean_material_name` under the existing imports and change `out.append({"qty": qty, "item": item.strip()})` to `out.append({"qty": qty, "item": clean_material_name(item)})`.

- [ ] **Step 4: Rewrite `cross_check.py`** — replace the whole file with:

```python
"""
Cross-check the spreadsheet (graded_blueprints, from Engineers.xlsx) against Coriolis
(coriolis_blueprints) and report every difference, plus the material names the
sheet uses that no other source knows (typos, currency, placeholders) so the
owner can fix the sheet.

A sheet row is matched to Coriolis the way Ellie's own lookup does it: the module name
resolves to its group(s), the group's allow-list (module_blueprints) says which blueprints
it can take, and the upgrade name is matched with the forgiving blueprint matcher.

Guardian modules use a different unlock mechanic and aren't in Coriolis's blueprint
file; those are reported as "no reference", not as mismatches.

    python cross_check.py
"""
import json
import sqlite3
from pathlib import Path

import query_functions as qf

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")


def normalize_materials(materials_json):
    """{name: qty} with lower-cased keys, so 'Sulphur' == 'sulphur', and no stray '(n)'."""
    counted, _extras = qf._split_materials(materials_json)
    return {name.lower().strip(): qty for name, qty in counted.items()}


def compare_row(conn, row):
    """(status, detail) for one graded_blueprints row. status: 'match', 'mismatch'
    (detail has the two cost dicts), 'no_coriolis_row' (Coriolis has no such blueprint
    and grade) or 'no_reference' (the module isn't one Coriolis has blueprints for)."""
    module = row["module_name"] or ""
    groups = ["bh"] if row["category"] == "armour" and not module else qf._resolve_module_groups(conn, module)
    if not groups:
        return "no_reference", None
    allowed = [r["fdname"] for r in conn.execute(
        f"SELECT DISTINCT fdname FROM module_blueprints WHERE grp IN ({qf._in_marks(groups)})", groups)]
    if not allowed:
        return "no_reference", None
    known = conn.execute(
        f"SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints WHERE fdname IN ({qf._in_marks(allowed)})", allowed).fetchall()
    hits = qf._blueprint_name_matches(known, row["upgrade"])
    cost = qf._coriolis_cost(conn, hits[0]["fdname"], row["grade"]) if len(hits) == 1 else None
    if cost is None:
        return "no_coriolis_row", None
    sheet = normalize_materials(row["materials_json"])
    coriolis = {name.lower().strip(): qty for name, qty in cost["materials"].items()}
    if sheet == coriolis:
        return "match", None
    return "mismatch", {"sheet": sheet, "coriolis": coriolis}


def suspect_material_names(conn):
    """Material names the sheet uses that neither Coriolis nor the commander's own
    inventory knows, sorted: typos, currency, placeholders."""
    known = set()
    for table in ("coriolis_blueprints", "coriolis_experimental_effects"):
        for (materials_json,) in conn.execute(f"SELECT materials_json FROM {table}"):
            known.update(qf._material_key(n) for n in qf._split_materials(materials_json)[0])
    for row in conn.execute("SELECT material_name, display_name FROM materials_inventory"):
        known.add(row[0])
        known.add(qf._material_key(row[1] or ""))
    suspects = set()
    for table in ("graded_blueprints", "ungraded_costs"):
        for (materials_json,) in conn.execute(f"SELECT materials_json FROM {table}"):
            counted, extras = qf._split_materials(materials_json)
            for name in list(counted) + extras:
                if qf._material_key(name) not in known:
                    suspects.add(name)
    return sorted(suspects)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT category, module_name, upgrade, grade, materials_json FROM graded_blueprints").fetchall()

    buckets = {"match": [], "mismatch": [], "no_coriolis_row": [], "no_reference": []}
    for row in rows:
        status, detail = compare_row(conn, row)
        buckets[status].append((row, detail))

    print(f"Checked: {len(rows)} rows")
    print(f"  Matches Coriolis exactly: {len(buckets['match'])}")
    print(f"  MISMATCHES: {len(buckets['mismatch'])}")
    print(f"  No Coriolis blueprint found for this module+upgrade+grade: {len(buckets['no_coriolis_row'])}")
    print(f"  No reference available (Guardian tech / unmapped module): {len(buckets['no_reference'])}")
    print()

    if buckets["mismatch"]:
        print("=== MISMATCHES ===")
        for row, detail in buckets["mismatch"]:
            print(f"\n{row['module_name']} / {row['upgrade']} / Grade {row['grade']}")
            print(f"  Sheet:    {detail['sheet']}")
            print(f"  Coriolis: {detail['coriolis']}")

    if buckets["no_coriolis_row"]:
        print("\n=== NO CORIOLIS ROW FOUND (module+upgrade+grade combo not in Coriolis data) ===")
        for row, _ in buckets["no_coriolis_row"]:
            print(f"  {row['module_name']} / {row['upgrade']} / Grade {row['grade']}")

    suspects = suspect_material_names(conn)
    if suspects:
        print("\n=== MATERIAL NAMES IN THE SHEET THAT NO OTHER SOURCE KNOWS (typos, currency, placeholders?) ===")
        for name in suspects:
            print(f"  {name}")

    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Remove the keyword map** — in `blueprint_mapping.py` delete the whole `MODULE_TO_CORIOLIS_KEYWORDS = {...}` block and the top docstring's mention of it; replace the module docstring with:

```python
"""
Names for the engineering blueprint lookups. Coriolis groups blueprints by module
group ('fsd', 't', 'pp', ...) and names each blueprint by an internal 'fdname'
(FSD_LongRange); players and the sheet use module and blueprint names in words. This
file holds the pure parts of that translation: the hand-kept synonyms the module files
cannot supply, in-game blueprint aliases, and the small string helpers the lookups
share. The database-backed resolver lives in query_functions.py.
Used by: query_functions.py, ingest_excel.py.
"""
```
Confirm nothing else uses it: `grep -rn "MODULE_TO_CORIOLIS_KEYWORDS" --include=*.py .` must print nothing outside `archive/` and `.superpowers/`. Add `"test_cross_check.py"` to `NEEDS_CORIOLIS_DATA` in `tests/conftest.py`.

- [ ] **Step 6: Run the tests** — `python -m pytest tests/test_ingest_excel.py tests/test_cross_check.py -q` then `python -m pytest tests -q`. Expected: all pass. Then on the owner's real database (read-only, safe): `python cross_check.py` — it needs `python setup.py` to have rebuilt the tables first; if `module_blueprints` is missing it will say so. Skip this check if the owner has not run setup yet; Task 12 covers it.

- [ ] **Step 7: Commit**

```bash
git add ingest_excel.py cross_check.py blueprint_mapping.py tests/test_ingest_excel.py tests/test_cross_check.py tests/conftest.py
git commit -m "Clean sheet annotations; cross-check through the resolver and list names no source knows" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Tool descriptions for the model

**Files:**
- Modify: `ellie_chat.py` (the three blueprint tool entries in `TOOLS`)
- Create: `tests/test_ellie_chat_blueprint_tools.py`

**Interfaces:** none new; descriptions only.

- [ ] **Step 1: Write the failing test** — create `tests/test_ellie_chat_blueprint_tools.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def _tool(name):
    return next(t["function"] for t in ec.TOOLS if t["function"]["name"] == name)


def test_get_blueprint_says_per_roll_and_covers_the_core_modules():
    tool = _tool("get_blueprint")
    assert tool["description"] == (
        "Get the materials ONE roll of an engineering blueprint costs at a given grade (1-5), for any module: "
        "frame shift drive, thrusters, power plant, power distributor, life support, sensors, shield generator, "
        "armour, weapons, utilities and optional internals.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name, e.g. 'Frame Shift Drive', 'Thrusters', 'Beam Laser'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"


def test_missing_materials_describes_one_roll_and_its_arguments():
    tool = _tool("missing_materials")
    assert tool["description"] == (
        "Check which materials, if any, are missing for ONE roll of an engineering blueprint at a given grade, "
        "compared to what the commander currently has on hand.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name, e.g. 'Frame Shift Drive', 'Thrusters', 'Beam Laser'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"
    assert props["grade"]["description"] == "Blueprint grade, 1 to 5"


def test_the_experimental_effect_example_is_a_module_that_can_take_it():
    tool = _tool("get_experimental_effect")
    assert tool["description"] == (
        "Get the materials an experimental effect costs each time it is applied to a module "
        "(ungraded, single cost). The effect must be one that module can take.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name, e.g. 'Frame Shift Drive', 'Cannon'"
    assert props["effect_name"]["description"] == "Experimental effect name, e.g. 'Mass Manager', 'Drag Munitions'"
```

- [ ] **Step 2: Run to see it fail** — `python -m pytest tests/test_ellie_chat_blueprint_tools.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement** — in `ellie_chat.py` set the `get_blueprint` entry to:

```python
            "description": "Get the materials ONE roll of an engineering blueprint costs at a given grade (1-5), for any module: frame shift drive, thrusters, power plant, power distributor, life support, sensors, shield generator, armour, weapons, utilities and optional internals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name, e.g. 'Frame Shift Drive', 'Thrusters', 'Beam Laser'"},
                    "upgrade_name": {"type": "string", "description": "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"},
                    "grade": {"type": "integer", "description": "Blueprint grade, 1 to 5"},
                },
```
(keep the `"required": [...]` line), the `get_experimental_effect` entry to:

```python
            "description": "Get the materials an experimental effect costs each time it is applied to a module (ungraded, single cost). The effect must be one that module can take.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name, e.g. 'Frame Shift Drive', 'Cannon'"},
                    "effect_name": {"type": "string", "description": "Experimental effect name, e.g. 'Mass Manager', 'Drag Munitions'"},
                },
```
and the `missing_materials` entry to:

```python
            "description": "Check which materials, if any, are missing for ONE roll of an engineering blueprint at a given grade, compared to what the commander currently has on hand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name, e.g. 'Frame Shift Drive', 'Thrusters', 'Beam Laser'"},
                    "upgrade_name": {"type": "string", "description": "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"},
                    "grade": {"type": "integer", "description": "Blueprint grade, 1 to 5"},
                },
```
Do not touch the system prompt: `tests/test_ellie_chat_ship_tools.py` pins its last three rules.

- [ ] **Step 4: Run the tests** — `python -m pytest tests/test_ellie_chat_blueprint_tools.py tests/test_ellie_chat_ship_tools.py tests/test_ellie_chat_loop.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ellie_chat.py tests/test_ellie_chat_blueprint_tools.py
git commit -m "Tool descriptions: core-module examples, per-roll cost, effect must fit the module" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Journal check and the real-crafts fixture

**Files:**
- Create: `verify_blueprints.py`, `tests/test_verify_blueprints.py`, `tests/fixtures/observed_crafts.json`
- Modify: `tests/conftest.py` (`NEEDS_CORIOLIS_DATA` gets `test_verify_blueprints.py`)

**Interfaces:**
- Consumes: `qf._coriolis_cost`, `qf._special_cost`, `qf._material_key`.
- Produces: `read_crafts(journal_dir) -> list[dict]` (`blueprint, grade, effect, ingredients{shown name: count}, when, module, engineer`); `summarise(crafts) -> {"grade_crafts": [{"blueprint","grade","ingredients","crafts"}], "experimental_crafts": [{"effect","ingredients","crafts"}]}`; `compare(conn, summary) -> list[{"kind","id","grade","status","observed","expected","crafts"}]` with `status` in `"match" | "mismatch" | "unknown"`. CLI: `python verify_blueprints.py [journal_dir]` and `python verify_blueprints.py --write-fixture PATH [journal_dir]`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_verify_blueprints.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import verify_blueprints as vb

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "observed_crafts.json"

# Differences between Coriolis and the owner's own crafts that are understood and recorded in
# docs/blueprint-data.md. A new difference fails the test; so does one that goes away (then
# remove it from this set).
KNOWN_DIFFERENCES = {"special_weapon_lightweight", "special_drag_munitions"}


def _craft(blueprint, level, ingredients, effect=None, when="2026-01-01T00:00:00Z"):
    event = {"timestamp": when, "event": "EngineerCraft", "Slot": "FrameShiftDrive", "Module": "int_hyperdrive_size5_class5",
             "Engineer": "Felicity Farseer", "BlueprintName": blueprint, "Level": level,
             "Ingredients": [{"Name": n.lower().replace(" ", ""), "Name_Localised": n, "Count": c} for n, c in ingredients.items()]}
    if effect:
        event["ApplyExperimentalEffect"] = effect
    return json.dumps(event)


def _journal(tmp_path, *lines):
    (tmp_path / "Journal.2026-01-01T000000.01.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_read_crafts_takes_only_engineer_craft_events_and_the_displayed_names(tmp_path):
    folder = _journal(tmp_path,
                      '{"event":"Music","MusicTrack":"x"}',
                      "this is not json",
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      '{"event":"EngineerCraft","BlueprintName":"Armour_Advanced","Level":1,"Ingredients":[{"Name":"iron","Count":1}]}')
    crafts = vb.read_crafts(folder)
    assert [c["blueprint"] for c in crafts] == ["FSD_LongRange", "Armour_Advanced"]
    assert crafts[0]["ingredients"] == {"Atypical Disrupted Wake Echoes": 1}
    assert crafts[1]["ingredients"] == {"iron": 1}                    # no localised name for raw elements


def test_summarise_counts_repeats_and_keeps_experimental_crafts_apart(tmp_path):
    folder = _journal(tmp_path,
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1},
                             effect="special_fsd_heavy"))
    summary = vb.summarise(vb.read_crafts(folder))
    assert summary["grade_crafts"] == [{"blueprint": "FSD_LongRange", "grade": 1, "ingredients": {"Atypical Disrupted Wake Echoes": 1}, "crafts": 2}]
    assert summary["experimental_crafts"] == [{"effect": "special_fsd_heavy", "crafts": 1,
                                               "ingredients": {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}}]


def test_compare_reports_match_mismatch_and_unknown(coriolis_db):
    summary = {
        "grade_crafts": [
            {"blueprint": "FSD_LongRange", "grade": 5, "crafts": 3,
             "ingredients": {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}},
            {"blueprint": "FSD_LongRange", "grade": 4, "crafts": 1, "ingredients": {"Arsenic": 9}},
            {"blueprint": "No_Such_Blueprint", "grade": 1, "crafts": 1, "ingredients": {"Carbon": 1}},
        ],
        "experimental_crafts": [
            {"effect": "special_fsd_heavy", "crafts": 1,
             "ingredients": {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}},
        ],
    }
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        results = vb.compare(conn, summary)
    finally:
        conn.close()
    assert [(r["id"], r["status"]) for r in results] == [
        ("FSD_LongRange", "match"), ("FSD_LongRange", "mismatch"), ("No_Such_Blueprint", "unknown"), ("special_fsd_heavy", "match")]
    assert results[1]["expected"] == {"Chemical Distillery": 1, "Eccentric Hyperspace Trajectories": 1, "Manganese": 1}


def test_a_name_the_journal_stores_differently_still_matches(coriolis_db):
    """The journal names raw elements in lower case ('arsenic'); Coriolis capitalises them."""
    summary = {"grade_crafts": [{"blueprint": "FSD_LongRange", "grade": 5, "crafts": 1,
                                 "ingredients": {"arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}}],
                "experimental_crafts": []}
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        assert vb.compare(conn, summary)[0]["status"] == "match"
    finally:
        conn.close()


def test_the_owner_s_real_crafts_match_coriolis_except_the_recorded_differences(coriolis_db):
    summary = json.load(open(FIXTURE, encoding="utf-8"))
    assert len(summary["grade_crafts"]) == 48 and len(summary["experimental_crafts"]) == 9
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        results = vb.compare(conn, summary)
    finally:
        conn.close()
    assert [r for r in results if r["status"] == "unknown"] == []
    assert {r["id"] for r in results if r["status"] == "mismatch"} == KNOWN_DIFFERENCES
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_verify_blueprints.py -q` — Expected: FAIL (`ModuleNotFoundError: verify_blueprints`).

- [ ] **Step 3: Implement** — create `verify_blueprints.py`:

```python
"""
Check Ellie's engineering costs against what YOUR OWN crafts really consumed.

Every time you engineer a module the game writes an EngineerCraft event to your journal
with the exact materials it took. This replays them all and compares each with the
cost Ellie would give for that blueprint and grade (or experimental effect). A match is
a cost proven in the game; a mismatch is a cost worth reporting.

    python verify_blueprints.py                      # your journal folder, Ellie's database
    python verify_blueprints.py "D:\\Journals"        # another journal folder
    python verify_blueprints.py --write-fixture tests/fixtures/observed_crafts.json
"""
import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

import query_functions as qf

DEFAULT_JOURNAL_DIR = Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"


def read_crafts(journal_dir):
    """Every EngineerCraft event in the folder's Journal.*.log files. `ingredients` maps
    the name the game shows (the journal's own name for raw elements, which have no
    display name) to the count consumed. `effect` is the experimental effect's internal
    name when this craft applied one, else None."""
    crafts = []
    for path in sorted(Path(journal_dir).glob("Journal.*.log")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"EngineerCraft"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") != "EngineerCraft":
                    continue
                ingredients = {i.get("Name_Localised") or i["Name"]: i["Count"]
                               for i in event.get("Ingredients", []) if "Name" in i and "Count" in i}
                crafts.append({"blueprint": event.get("BlueprintName"), "grade": event.get("Level"),
                               "effect": event.get("ApplyExperimentalEffect"), "ingredients": ingredients,
                               "when": event.get("timestamp"), "module": event.get("Module"), "engineer": event.get("Engineer")})
    return crafts


def summarise(crafts):
    """Distinct crafts with how many times each happened. Game facts only: no times,
    no commander data. Two spellings of one material ('Iron' and 'iron') are one
    ingredient; the capitalised spelling is the one kept."""
    grade, special, shown = collections.Counter(), collections.Counter(), {}
    for c in crafts:
        for name in c["ingredients"]:
            key = qf._material_key(name)
            if key not in shown or (name[:1].isupper() and not shown[key][:1].isupper()):
                shown[key] = name
        items = tuple(sorted((qf._material_key(n), q) for n, q in c["ingredients"].items()))
        if c["effect"]:
            special[(c["effect"], items)] += 1
        else:
            grade[(c["blueprint"], c["grade"], items)] += 1

    def named(items):
        return {shown[key]: qty for key, qty in items}

    return {
        "grade_crafts": [{"blueprint": bp, "grade": level, "ingredients": named(items), "crafts": n}
                         for (bp, level, items), n in sorted(grade.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0))],
        "experimental_crafts": [{"effect": effect, "ingredients": named(items), "crafts": n}
                                for (effect, items), n in sorted(special.items(), key=lambda kv: str(kv[0][0]))],
    }


def _judge(kind, ident, grade, observed, expected, crafts):
    if expected is None:
        status = "unknown"
    else:
        same = ({qf._material_key(n): q for n, q in observed.items()} == {qf._material_key(n): q for n, q in expected.items()})
        status = "match" if same else "mismatch"
    return {"kind": kind, "id": ident, "grade": grade, "status": status, "observed": observed, "expected": expected, "crafts": crafts}


def compare(conn, summary):
    """Judge every summarised craft against Ellie's recorded cost. Statuses: 'match',
    'mismatch' (the game consumed something else), 'unknown' (Ellie has no such blueprint)."""
    results = []
    for entry in summary["grade_crafts"]:
        cost = qf._coriolis_cost(conn, entry["blueprint"], entry["grade"])
        results.append(_judge("blueprint", entry["blueprint"], entry["grade"], entry["ingredients"], cost and cost["materials"], entry["crafts"]))
    for entry in summary["experimental_crafts"]:
        cost = qf._special_cost(conn, entry["effect"])
        results.append(_judge("effect", entry["effect"], None, entry["ingredients"], cost and cost["materials"], entry["crafts"]))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check Ellie's engineering costs against your own crafts.")
    parser.add_argument("journal_dir", nargs="?", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--write-fixture", metavar="PATH", help="write the distinct crafts to a JSON file and stop")
    args = parser.parse_args(argv)

    crafts = read_crafts(args.journal_dir)
    summary = summarise(crafts)
    if args.write_fixture:
        Path(args.write_fixture).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{len(crafts)} crafts -> {len(summary['grade_crafts'])} grade combinations, "
              f"{len(summary['experimental_crafts'])} experimental effects -> {args.write_fixture}")
        return 0

    conn = sqlite3.connect(qf.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        results = compare(conn, summary)
    finally:
        conn.close()
    counts = collections.Counter(r["status"] for r in results)
    print(f"{len(crafts)} crafts in {args.journal_dir}")
    print(f"  match: {counts['match']}   mismatch: {counts['mismatch']}   unknown to Ellie: {counts['unknown']}")
    detail = collections.defaultdict(list)
    for c in crafts:
        detail[(c["effect"] or c["blueprint"], None if c["effect"] else c["grade"])].append(c)
    for r in results:
        if r["status"] == "match":
            continue
        print(f"\n{r['status'].upper()}: {r['id']}" + (f" grade {r['grade']}" if r["grade"] else "") + f" ({r['crafts']} craft(s))")
        print(f"  the game consumed: {r['observed']}")
        print(f"  Ellie says:        {r['expected']}")
        for c in detail[(r["id"], r["grade"])][:3]:
            print(f"  e.g. {c['when']}  {c['module']}  {c['engineer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Add `"test_verify_blueprints.py"` to `NEEDS_CORIOLIS_DATA` in `tests/conftest.py`.

- [ ] **Step 4: Generate the fixture from the owner's journals** — run: `python verify_blueprints.py --write-fixture tests/fixtures/observed_crafts.json`. Expected output: `257 crafts -> 48 grade combinations, 9 experimental effects -> tests/fixtures/observed_crafts.json`. Open the file and confirm it holds only blueprint names, grades, material names and counts (no timestamps, ship or commander names).

- [ ] **Step 5: Run the tests** — `python -m pytest tests/test_verify_blueprints.py -q`. Expected: all pass. If `test_the_owner_s_real_crafts_match_coriolis_except_the_recorded_differences` reports a different set of mismatches, do not edit `KNOWN_DIFFERENCES` to make it pass: stop and show the mismatches to the owner (Coriolis data may have moved since 2026-11-06).

- [ ] **Step 6: Commit**

```bash
git add verify_blueprints.py tests/test_verify_blueprints.py tests/fixtures/observed_crafts.json tests/conftest.py
git commit -m "Add verify_blueprints.py and a fixture of real crafts that pins the costs" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Update path, docs and README

**Files:**
- Modify: `setup.py` (`coriolis_refreshed`, `update_coriolis_clone`, `setup_coriolis`, `setup_ship_modules`)
- Create: `docs/blueprint-data.md`, `tests/test_setup_coriolis_update.py`
- Modify: `README.md`, `tests/test_setup_ship_modules.py`, `tests/test_readme.py`

**Interfaces:**
- Produces: `setup.coriolis_refreshed: bool` (module global, `False` at start, set `True` when `setup_coriolis` re-ingested in this run); `setup.update_coriolis_clone() -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_setup_coriolis_update.py` (needs no Coriolis data):

```python
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setup as setup_script


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A fake coriolis-data clone (with a .git folder and the marker file) and a stub ingest."""
    folder = tmp_path / "coriolis-data"
    (folder / ".git").mkdir(parents=True)
    marker = folder / "modifications" / "blueprints.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    ingested = []
    monkeypatch.setitem(sys.modules, "ingest_coriolis", types.SimpleNamespace(main=lambda: ingested.append(True)))
    monkeypatch.setattr(setup_script, "CORIOLIS_DIR", folder)
    monkeypatch.setattr(setup_script, "CORIOLIS_MARKER", marker)
    monkeypatch.setattr(setup_script, "_table_count", lambda table: 387)       # Coriolis already ingested
    monkeypatch.setattr(setup_script, "coriolis_refreshed", False)
    monkeypatch.setattr(setup_script.shutil, "which", lambda name: "git")
    return types.SimpleNamespace(folder=folder, ingested=ingested)


def _record_git(monkeypatch, returncode=0):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(setup_script.subprocess, "run", run)
    return calls


def test_saying_yes_pulls_the_clone_reingests_and_marks_it_refreshed(clone, monkeypatch):
    calls = _record_git(monkeypatch)
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: True)
    assert setup_script.setup_coriolis() is True
    assert calls == [["git", "-C", str(clone.folder), "pull", "--ff-only"]]
    assert clone.ingested == [True] and setup_script.coriolis_refreshed is True


def test_saying_no_changes_nothing(clone, monkeypatch, capsys):
    calls = _record_git(monkeypatch)
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: False)
    assert setup_script.setup_coriolis() is True
    assert calls == [] and clone.ingested == [] and setup_script.coriolis_refreshed is False
    assert "Skipped." in capsys.readouterr().out


def test_a_failed_pull_still_reingests_the_copy_you_have(clone, monkeypatch, capsys):
    _record_git(monkeypatch, returncode=1)
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: True)
    assert setup_script.setup_coriolis() is True
    assert "Updating coriolis-data failed" in capsys.readouterr().out
    assert clone.ingested == [True]


def test_a_folder_that_is_not_a_git_clone_is_not_pulled(clone, monkeypatch, capsys):
    (clone.folder / ".git").rmdir()
    calls = _record_git(monkeypatch)
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: True)
    assert setup_script.setup_coriolis() is True
    assert calls == [] and clone.ingested == [True]
    assert "isn't a git clone" in capsys.readouterr().out


def test_without_git_installed_it_says_so_and_still_reingests(clone, monkeypatch, capsys):
    calls = _record_git(monkeypatch)
    monkeypatch.setattr(setup_script.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_script, "_ask_yes_no", lambda prompt, default=True: True)
    assert setup_script.setup_coriolis() is True
    assert calls == [] and clone.ingested == [True]
    assert "git isn't on PATH" in capsys.readouterr().out
```

Append to `tests/test_setup_ship_modules.py`:

```python
def test_after_a_coriolis_refresh_the_ship_tables_are_rebuilt_without_asking(setup_db, monkeypatch, capsys):
    monkeypatch.setattr(setup_script, "coriolis_refreshed", True)
    _must_not_ask(monkeypatch)
    before = _count(setup_db, "module_group_names")
    assert before > 0                                           # complete tables: only the refresh makes it rebuild
    assert setup_script.setup_ship_modules() is True
    assert "Coriolis data was just refreshed -- rebuilding the ship & module tables from it." in capsys.readouterr().out
    assert _count(setup_db, "module_group_names") == before
```

In `tests/test_readme.py` append:

```python
def test_the_readme_explains_how_to_update_after_a_game_update():
    assert "## Updating after a game update" in README
    assert "python setup.py" in README[README.index("## Updating after a game update"):]
    assert "docs/blueprint-data.md" in README
    assert "Blueprint costs come from your spreadsheet first, then fall back to Coriolis's broader" not in README


def test_the_blueprint_data_page_exists_and_states_the_precedence_and_per_roll_costs():
    page = (Path(__file__).resolve().parent.parent / "docs" / "blueprint-data.md").read_text(encoding="utf-8")
    assert "per roll" in page and "sheet first" in page.lower()
    assert "python verify_blueprints.py" in page
```

- [ ] **Step 2: Run to see them fail** — `python -m pytest tests/test_setup_coriolis_update.py tests/test_setup_ship_modules.py tests/test_readme.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement setup.py** — add near the top constants: `coriolis_refreshed = False  # set when this run re-ingests Coriolis; the ship step then rebuilds without asking`. Add before `setup_coriolis`:

```python
def update_coriolis_clone():
    """git pull --ff-only in the existing clone. True when the update ran cleanly or was not
    possible but harmless; False when it failed (the caller carries on with the copy you have)."""
    if not (CORIOLIS_DIR / ".git").exists():
        print("coriolis-data isn't a git clone (maybe it was downloaded as a zip), so it can't be updated here.")
        return True
    if shutil.which("git") is None:
        print("git isn't on PATH -- install Git, or update the coriolis-data folder yourself, then re-run this script.")
        return False
    result = subprocess.run(["git", "-C", str(CORIOLIS_DIR), "pull", "--ff-only"])
    if result.returncode != 0:
        print("Updating coriolis-data failed -- see the error above. Continuing with the copy you have.")
        return False
    return True
```

Replace `setup_coriolis` with:

```python
def setup_coriolis():
    global coriolis_refreshed
    _heading("Coriolis data (blueprint costs)")
    existing = _table_count("coriolis_blueprints")
    updating = False
    if existing:
        print(f"coriolis_blueprints already has {existing} rows.")
        if not _ask_yes_no("Update the Coriolis data (git pull) and re-ingest it?", default=False):
            print("Skipped.")
            return True
        updating = True

    if not CORIOLIS_MARKER.exists():
        if not _ask_yes_no(f"Clone {CORIOLIS_URL} into ./coriolis-data?"):
            print("Skipped -- blueprint lookups will only have your Excel sheet, if any.")
            return False
        if shutil.which("git") is None:
            print("git isn't on PATH -- install Git, or clone the repo yourself, then re-run this script.")
            return False
        result = subprocess.run(["git", "clone", "--depth", "1", CORIOLIS_URL, str(CORIOLIS_DIR)])
        if result.returncode != 0:
            print("Clone failed -- see the error above.")
            return False
    elif updating:
        update_coriolis_clone()

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_coriolis
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False
    ingest_coriolis.main()
    coriolis_refreshed = True
    return True
```

In `setup_ship_modules`, replace the `if not empty:` block with:

```python
    if not empty:
        if coriolis_refreshed:
            print("Coriolis data was just refreshed -- rebuilding the ship & module tables from it.")
        else:
            print("The ship & module tables already have rows ("
                  + ", ".join(f"{t} {counts[t]}" for t in tables) + ").")
            if not _ask_yes_no("Re-ingest anyway?", default=False):
                print("Skipped.")
                return True
    elif len(empty) < len(tables):
```
(keep the existing body of the `elif` unchanged). Check `tests/test_setup_*.py` for any test that pins the old prompt text `Re-clone/re-ingest anyway?`: `grep -rn "Re-clone" tests` must print nothing; if it does, update that test to the new wording.

- [ ] **Step 4: Write `docs/blueprint-data.md`**:

```markdown
# Where Ellie's engineering costs come from

Ellie never gives a material cost from memory. Every number comes from a table in her local
database, `elite-dangerous.db`, and the reply says which source it used.

## The two sources

1. **Coriolis** ([EDCD/coriolis-data](https://github.com/EDCD/coriolis-data)), community-maintained and
   cloned into `coriolis-data/` by `python setup.py`. It covers every engineerable module: core
   internals (frame shift drive, thrusters, power plant, power distributor, life support, sensors),
   shields, armour, weapons, utilities and optional internals. Everyone has this.
2. **Your own spreadsheet**, `Engineers.xlsx` (optional, not part of the repository). If you keep one, the
   rows you filled in are used first.

## Which one wins

Sheet first: when your spreadsheet has the module, upgrade and grade, that row is used. Otherwise Coriolis
answers. Where both have an answer they agree on 617 of 622 rows (checked 2026-11-06 with `python cross_check.py`).

## What a cost means

- A blueprint's cost is **per roll**: what one craft uses. How many rolls a grade needs is not part of it.
- An experimental effect's cost is **per application**.
- If a source lists an upgrade with no materials, Ellie says "no materials are recorded", never "free".

## Known differences between the sources (2026-11-06)

For you to decide, nothing is changed silently. The sheet is used for these.

| Module / upgrade / grade | Sheet | Coriolis |
|---|---|---|
| Mining Laser / Long Range / 2 | Sulphur, Heat Dispersion Plate, Modified Consumer Firmware, 5 Merc Coin | Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 3 | Chromium, Modified Consumer Firmware, Heat Exchangers, 5 Merc Coin | Focus Crystals, Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 4 | Modified Consumer Firmware, Conductive Polymers, Selenium, Heat Vanes, 10 Merc Coin | Conductive Polymers, Focus Crystals, Modified Consumer Firmware |
| Mining Laser / Long Range / 5 | Cracked Industrial Firmware, Thermic Alloys, Biotech Conductors, Proto Heat Radiators, 25 Merc Coin | Biotech Conductors, Cracked Industrial Firmware, Thermic Alloys |
| Chaff Launcher / Ammo Capacity / 1 | Mechanical Scrap | Mechanical Scrap, Niobium, Vanadium |

## Checking costs against your own crafts

Every craft you make is written to your journal with the materials it really used.

```bash
python verify_blueprints.py
```

compares each one with the cost Ellie gives and prints any difference, with when and on which module it
happened. A mismatch is worth reporting to Coriolis (open an issue on their GitHub repository with the
blueprint, what the game used, and how many crafts showed it).

## Updating after a game update

Coriolis's maintainers update their data after a patch. Then run:

```bash
python setup.py
```

and answer **yes** to "Update the Coriolis data (git pull) and re-ingest it?". It pulls the newest
Coriolis data, re-ingests the cost tables and rebuilds the ship, module and name tables from it. If you only
changed your spreadsheet, run `python ingest_excel.py` instead.

What is rebuilt: the reference tables (blueprint costs, effects, ship and module stats, which module takes which
blueprint and effect, module names). What is never touched: the live tables the journal watcher keeps (your
materials, ship, fuel and cargo).
```

- [ ] **Step 5: Update the README** — replace the Limitations bullet that begins "Blueprint costs come from your spreadsheet first" (two lines) with:

```markdown
- Blueprint costs come from your spreadsheet first, when you keep one, then from Coriolis's data, which
  covers every engineerable module. Costs are per roll. See [docs/blueprint-data.md](docs/blueprint-data.md).
```
and insert, directly above `## Running the tests`:

```markdown
## Updating after a game update

Coriolis's maintainers update their data after a patch. Run `python setup.py` again and answer yes to
"Update the Coriolis data (git pull) and re-ingest it?": it pulls the newest data and rebuilds the cost,
ship and module tables from it. Your live game state is never touched. Details and the `python verify_blueprints.py`
check against your own crafts are in [docs/blueprint-data.md](docs/blueprint-data.md).
```

- [ ] **Step 6: Run the tests** — `python -m pytest tests/test_setup_coriolis_update.py tests/test_setup_ship_modules.py tests/test_readme.py -q`, then `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add setup.py docs/blueprint-data.md README.md tests/test_setup_coriolis_update.py tests/test_setup_ship_modules.py tests/test_readme.py
git commit -m "setup.py can update the Coriolis clone; document sources, precedence and updating" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 11: The two differing experimental costs (decision gate)

**Do not start this task without the owner.** It decides how Ellie treats the two experimental effects whose cost in the owner's journal differs from Coriolis's.

**Files (only if a correction is chosen):**
- Create: `blueprint_corrections.json`
- Modify: `ingest_coriolis.py` (`apply_corrections`, `main`), `docs/blueprint-data.md`, `tests/test_verify_blueprints.py` (`KNOWN_DIFFERENCES`)
- Create: `tests/test_ingest_coriolis_corrections.py`

- [ ] **Step 1: Re-read the evidence** — run `python verify_blueprints.py`. Expected: 2 mismatches, `special_drag_munitions` (the game consumed Carbon 5, Grid Resistors 5, Molybdenum 2; Coriolis says Molybdenum 5; 1 craft) and `special_weapon_lightweight`, called Stripped Down (the game consumed Carbon 5; Coriolis says Carbon 3; 2 crafts). The output prints the module and engineer for each. Check whether the modules involved matter (Drag Munitions is a Fragment Cannon effect; Stripped Down applies to many weapon groups).

- [ ] **Step 2: Ask the owner** (use `AskUserQuestion`) with the evidence, and these options: (a) add a small corrections file with the journal evidence now, and report upstream in parallel (recommended in the spec); (b) report upstream only and leave Ellie unchanged; (c) leave both as they are and only document them. A single craft is thin evidence for Drag Munitions: say so, and recommend correcting only what repeated crafts show. If the owner picks (b) or (c), skip to Step 6 (docs only).

- [ ] **Step 3: Write the failing test (option a)** — create `tests/test_ingest_coriolis_corrections.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_coriolis as ic


def _db():
    conn = sqlite3.connect(":memory:")
    ic.build_schema(conn)
    conn.execute("INSERT INTO coriolis_blueprints VALUES ('FSD_LongRange','Increased range','Frame shift drive',3,?,'{}')", (json.dumps({"Carbon": 1}),))
    conn.execute("INSERT INTO coriolis_experimental_effects VALUES ('special_weapon_lightweight','Stripped Down','d',?)", (json.dumps({"Carbon": 3, "Tin": 1}),))
    return conn


def test_a_correction_replaces_the_cost_and_reports_how_many_rows_it_changed():
    conn = _db()
    applied = ic.apply_corrections(conn, {
        "graded": [{"fdname": "FSD_LongRange", "grade": 3, "materials": {"Carbon": 2}, "evidence": "e"}],
        "experimental": [{"edname": "Special_Weapon_Lightweight", "materials": {"Carbon": 5, "Tin": 1}, "evidence": "e"}]})
    assert applied == 2
    assert json.loads(conn.execute("SELECT materials_json FROM coriolis_blueprints").fetchone()[0]) == {"Carbon": 2}
    assert json.loads(conn.execute("SELECT materials_json FROM coriolis_experimental_effects").fetchone()[0]) == {"Carbon": 5, "Tin": 1}


def test_a_correction_that_matches_nothing_is_reported_not_ignored():
    conn = _db()
    assert ic.apply_corrections(conn, {"graded": [], "experimental": [{"edname": "special_gone", "materials": {}, "evidence": "e"}]}) == 0


def test_the_shipped_corrections_file_is_well_formed_and_every_entry_has_evidence():
    data = json.load(open(Path(__file__).resolve().parent.parent / "blueprint_corrections.json", encoding="utf-8"))
    assert set(data) == {"graded", "experimental"}
    for entry in data["graded"] + data["experimental"]:
        assert entry["evidence"].strip() and isinstance(entry["materials"], dict) and entry["materials"]
```

- [ ] **Step 4: Implement** — in `ingest_coriolis.py` add after `ingest_specials`:

```python
def apply_corrections(conn, corrections):
    """Replace Coriolis's cost wherever the commander's own crafts (verify_blueprints.py)
    proved it wrong. Returns how many rows changed; the caller reports entries that changed
    nothing, because that means Coriolis renamed or fixed them."""
    changed = 0
    for entry in corrections.get("graded", []):
        changed += conn.execute("UPDATE coriolis_blueprints SET materials_json = ? WHERE fdname = ? AND grade = ?",
                                (json.dumps(entry["materials"]), entry["fdname"], entry["grade"])).rowcount
    for entry in corrections.get("experimental", []):
        changed += conn.execute("UPDATE coriolis_experimental_effects SET materials_json = ? WHERE lower(edname) = lower(?)",
                                (json.dumps(entry["materials"]), entry["edname"])).rowcount
    return changed
```
and in `main()`, after `sp_count = ingest_specials(conn, specials)`, add:

```python
    corrections_path = SCRIPT_DIR / "blueprint_corrections.json"
    if corrections_path.exists():
        corrections = json.load(open(corrections_path, encoding="utf-8"))
        wanted = len(corrections.get("graded", [])) + len(corrections.get("experimental", []))
        applied = apply_corrections(conn, corrections)
        print(f"corrections: {applied} of {wanted} applied" + ("" if applied == wanted else " -- some no longer match Coriolis; check blueprint_corrections.json"))
```
Create `blueprint_corrections.json` with only the corrections the owner chose in Step 2, in this shape (example for Stripped Down; use the exact ingredient names from the `verify_blueprints.py` output):

```json
{
  "graded": [],
  "experimental": [
    {"edname": "special_weapon_lightweight",
     "materials": {"Carbon": 5, "Salvaged Alloys": 5, "Tin": 1},
     "evidence": "Stripped Down on weapons: 2 crafts in the owner's journals consumed 5 Carbon (Coriolis says 3); checked 2026-11-06 with verify_blueprints.py"}
  ]
}
```
Then in `tests/test_verify_blueprints.py` change `KNOWN_DIFFERENCES` to the effects that still differ after the corrections (empty set if none), and note: the verify tests build their database from Coriolis alone (`coriolis_db`), so corrections applied only in `main()` are not seen there. Make `tests/conftest.py`'s `_coriolis_template` apply the shipped corrections too: after `ic.ingest_specials(...)` add

```python
    corrections_file = Path(__file__).resolve().parent.parent / "blueprint_corrections.json"
    if corrections_file.exists():
        ic.apply_corrections(conn, json.load(open(corrections_file, encoding="utf-8")))
```

- [ ] **Step 5: Run the tests** — `python -m pytest tests -q`. Expected: all pass.

- [ ] **Step 6: Record the decision** — add a section "Corrections and reports" to `docs/blueprint-data.md` stating what was decided and why (which effect, how many crafts, what Coriolis says, whether it was reported upstream). If an upstream report is wanted, draft its text for the owner to post (blueprint or effect, what the game consumed, the number of crafts, the dates); never post it for them.

- [ ] **Step 7: Commit**

```bash
git add -A blueprint_corrections.json ingest_coriolis.py docs/blueprint-data.md tests/test_ingest_coriolis_corrections.py tests/test_verify_blueprints.py tests/conftest.py
git commit -m "Record how the two differing experimental costs are handled" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
(Adjust the `git add` list to the files that actually changed; if the owner chose option b or c, only `docs/blueprint-data.md` changes.)

---

### Task 12: Benchmark questions and the final checks

**Files:**
- Modify (local, not committed: `.superpowers/` is git-ignored): `.superpowers/bench/bench_v2.py`, `.superpowers/bench/bench_models.py`
- Verify only: the whole repository

- [ ] **Step 1: Let the benchmark build the new tables in its database copy** — in `.superpowers/bench/bench_models.py`, in `prepare_db()`, directly after `jw.build_schema(conn)` add:

```python
    import json as _json
    import ingest_coriolis as _ic
    import ingest_ship_modules as _ism
    _repo = WORKTREE / "coriolis-data"
    _ism.ingest_all(conn, str(_repo))          # ship, module, allow-list, group-name and special tables
    _ic.build_schema(conn)
    conn.execute("DELETE FROM coriolis_blueprints")
    conn.execute("DELETE FROM coriolis_experimental_effects")
    _ic.ingest_blueprints(conn, _json.load(open(_repo / "modifications" / "blueprints.json", encoding="utf-8")))
    _ic.ingest_specials(conn, _json.load(open(_repo / "modifications" / "specials.json", encoding="utf-8")))
    conn.commit()
```

- [ ] **Step 2: Turn the two gaps into lookups and add six questions** — in `.superpowers/bench/bench_v2.py` delete the two lines `add("G1", ...)` and `add("G2", ...)`, and after the line `add("D12", ...)` insert:

```python
    add("D13", "D", T("What does FSD Increased Range grade 5 cost in materials?", {"get_blueprint", "missing_materials"}, _has("range", "fsd", "frame"), key=key_materials(lambda: qf.get_blueprint("Frame Shift Drive", "Increased range", 5))))
    add("D14", "D", T("What materials do I need for grade 5 Dirty Drives on thrusters?", {"get_blueprint", "missing_materials"}, _has("dirty", "thruster"), key=key_materials(lambda: qf.get_blueprint("Thrusters", "Dirty", 5))))
    add("D15", "D", T("What does a grade 3 Armoured power plant cost in materials?", {"get_blueprint", "missing_materials"}, _has("armour", "power"), key=key_materials(lambda: qf.get_blueprint("Power Plant", "Armoured", 3))))
    add("D16", "D", T("Grade 2 Charge Enhanced on a power distributor: what materials?", {"get_blueprint", "missing_materials"}, _has("charge", "distributor"), key=key_materials(lambda: qf.get_blueprint("Power Distributor", "Charge enhanced", 2))))
    add("D17", "D", T("What materials does grade 1 Lightweight need on life support?", {"get_blueprint", "missing_materials"}, _has("lightweight", "life"), key=key_materials(lambda: qf.get_blueprint("Life Support", "Lightweight", 1))))
    add("D18", "D", T("What do I need for grade 4 Long Range on sensors?", {"get_blueprint", "missing_materials"}, _has("long", "sensor"), key=key_materials(lambda: qf.get_blueprint("Sensors", "Long range", 4))))
    add("D19", "D", T("What am I still missing for FSD Increased Range grade 3?", {"missing_materials"}, _has("range", "fsd", "frame"), key=key_materials(lambda: qf.missing_materials("Frame Shift Drive", "Increased range", 3), min_names=1)))
    add("D20", "D", T("What am I still missing for Dirty Drives grade 2?", {"missing_materials"}, _has("dirty", "thruster"), key=key_materials(lambda: qf.missing_materials("Thrusters", "Dirty", 2), min_names=1)))
```

- [ ] **Step 3: Read the full suite and the owner's real data** — `python -m pytest tests -q` (expected: all pass; report the exact count, baseline 500). Then ask the owner to run `python setup.py` on the live database (it adds the new tables, answers yes to re-ingest when asked, and does not touch their live state). **Do not run it yourself.** After they have, run read-only: `python verify_blueprints.py` and `python cross_check.py` against the live database and show the results.

- [ ] **Step 4: Run the benchmark locally** (no Claude usage; needs Ollama with `bonsai-2:4b` loaded) from the repository root:

```bash
set BENCH_CODE_DIR=%CD%
python .superpowers/bench/bench_v2.py --models bonsai-2:4b --trials 3 --cats D
```
(On Git Bash use `BENCH_CODE_DIR="$PWD" python ...`.) Expected: D13 and D14 (the former G1 and G2) and the six new questions each pass in at least 2 of 3 trials, the same bar as the other lookups. If a question fails, read its answer and the tool call in the output before changing anything: the usual causes are the model passing a name the resolver misses (add a synonym with a test) or a reply wording the model garbles (shorten the `summary`, with a test).

- [ ] **Step 5: Whole-branch check** — `git log --oneline main..HEAD` lists one commit per task; `git status` is clean; `git diff main --stat` touches only the files in the spec's section 9. Report the final test count, the benchmark result for D13-D20, and `verify_blueprints.py`'s counts. Then hand the branch to the owner for the merge decision (finishing-a-development-branch). Do not push.

---

## Self-review (run against the spec)

**Spec coverage.** Section 1 done-when 1 (reachability): Tasks 3-4 (`test_every_coriolis_blueprint_and_grade_is_reachable_by_name`) and Task 6 for effects. 2 (journal costs): Task 9 fixture test; the two differences: Task 11. 3 (held materials): Task 5. 4 (unknown cost): Tasks 4, 5. 5 (benchmark): Task 12. 6 (Coriolis alone): Task 4's first test and the `coriolis_db` fixture, which has no sheet tables. 7 (update step): Task 10. Section 6.1 (names): Tasks 1-3. 6.2 (matching and aliases): Tasks 2-3. 6.3 (precedence, sheet optional): Task 4. 6.4 (honest unknowns, grades, wrong module): Task 4. 6.5: Task 5. 6.6: Task 6. 6.7 (reply shape, descriptions): Tasks 4-6, 8. 6.8 (sheet hygiene): Tasks 4 (read side) and 7 (ingest and report). 6.9: Task 10. 6.10: Task 9. Section 8 risks: 1 (setup re-run) Tasks 1 and 10; 2 (experimental differences) Task 11; 3 (sheet conflicts) Task 10 docs; 4 (aliases) Task 2 step 5; 5 (per roll) global constraint and replies.

**Placeholders.** None: every code step shows the code; Task 11's file contents depend on the owner's decision by design and the plan says exactly what to do for each option.

**Type consistency.** `_split_materials -> (dict, list)` is used in Tasks 4, 5, 6, 7 as defined; `_coriolis_cost` returns `{"upgrade_name", "materials", "extras"}` and `_special_cost` returns `{"name", "description", "materials", "extras"}`, used by Tasks 4, 6, 7, 9 with those keys; `_resolve_module_groups(conn, typed)` and `_blueprint_name_matches(rows, name)` signatures are the same in Tasks 3, 4, 6, 7; `_in_marks`, `_group_label`, `_sheet_rows`, `_sheet_module_names(conn, table, module_name, groups)`, `_MODULE_HINT` are defined in Task 4 before Task 6 uses them; the `coriolis_db` fixture is defined in Task 3 and used by Tasks 4-9; `NEEDS_CORIOLIS_DATA` gains each new data-dependent test file in the task that creates it (`test_blueprint_lookup.py` in 3, `test_cross_check.py` in 7, `test_verify_blueprints.py` in 9).
