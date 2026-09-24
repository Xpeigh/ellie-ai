# Ship Loadout Calculator (Phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Ellie real, deterministic "what if I fit X" answers for ship loadouts — mass, jump range, and power margin — instead of the model guessing.

**Architecture:** A new ingest script (`ingest_ship_modules.py`) loads ship and module stats from `coriolis-data` (already cloned) into two new tables. `journal_watcher.py` gains a `Loadout` handler that captures the commander's real current fit, including the game's own already-computed jump range and any real engineering modifiers, into two more tables. A new pure-calculation module holds the verified jump-range formula and blueprint-delta math, with no database dependency, so it's cleanly unit-testable on its own. Three new query functions wire it together; three new tool definitions expose them to the chat model, following every existing convention in `query_functions.py`/`ellie_chat.py` exactly.

**Tech Stack:** Python, sqlite3, pytest (new to this project — see Task 0).

**Spec:** [docs/superpowers/specs/2026-09-22-ship-loadout-calculator-design.md](../specs/2026-09-22-ship-loadout-calculator-design.md)

## Global Constraints

- Never fabricate a number. Every new query function returns `{"found": False, "reason": ...}` rather than guessing when data is missing, ambiguous, or incompatible — same convention as all 7 existing tools.
- New tables are owned entirely by the code that creates them; ingestion scripts only drop/recreate their own tables (never touch another script's tables), matching `ingest_coriolis.py`.
- Tool arguments stay flat (no nested lists/objects) — this project's own Phase 4 testing found small models unreliable with complex argument shapes.
- A blueprint grade whose `features` min equals max is deterministic — report one exact value, never a fabricated min–max spread (spec, "Blueprint delta application").
- Snapshot tables (`current_loadout`, `current_loadout_modules`) are replaced wholesale on every `Loadout` event, the same pattern `Materials`/`reset_materials` already uses — never merged/diffed.
- For the commander's *current* fitted modules, always prefer the journal's own real `Engineering.Modifiers` values over recomputed blueprint math (the journal is ground truth, including experimental-effect contributions blueprint tables can't capture). Recompute from blueprint tables only for a *hypothetical* module the commander doesn't own.

## Review Focus

1. **Fresh session, zero `Loadout` events ever captured.** The table exists (schema was created) but has 0 rows — a different failure mode than "no such table," which the existing `@_needs_setup` decorator doesn't catch. Must return a clean message, not a `TypeError` on an empty result. *(Task 3)*
2. **A ship has no module of the requested group currently fitted at all** (e.g. "what if I add a shield booster" when none is fitted — an empty slot has no row in `current_loadout_modules`). **Scope, stated honestly:** `ship_stats.slots_json` only gives slot *sizes* per position, not the slot *names* the journal uses (`"Slot11_Size2"`) or per-slot type eligibility for ordinary (non-reserved) slots — there's no verified mapping from one to the other. Phase A does **not** guess which empty slot is meant; it fails cleanly with a message asking for an explicit `slot`, the same honest-limitation treatment already given to bulkheads. *(Task 5)*
3. **The model passes a `module_group` string that matches nothing in the known mapping** (invented or misremembered term). Must fail with a clear reason, not a `KeyError`. *(Task 5)*
4. **A `blueprint_name` that exists but doesn't apply to the target module's category** (e.g. an FSD blueprint requested for a shield generator). Must return `{"found": False}`, reusing `get_blueprint`'s own category-matching mechanism — not a wrong blueprint silently applied. *(Task 5)*
5. **A blueprint grade with `min_pct == max_pct` for a given stat** (several real ones are deterministic, e.g. `FSD_LongRange` grade 1's `mass`/`integrity`/`power`). Must report a single value, not `(x, x)` presented as a meaningful range. *(Task 4)*

---

## Task 0: Add pytest

This project has no automated test framework yet (prior verification in this project has been ad hoc scratch scripts). pytest is the one new dependency this plan needs.

**Files:**
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `pytest` available for every later task's tests.

- [ ] **Step 1: Add pytest to requirements.txt**

Add a line `pytest` to `requirements.txt` (alongside the existing `ollama`/`openpyxl`/`ijson`/`requests`/`fastapi`/`uvicorn` entries).

- [ ] **Step 2: Install it**

Run: `pip install pytest`
Expected: installs cleanly (nothing else in this project conflicts with it).

- [ ] **Step 3: Verify pytest runs (even with nothing to collect yet)**

Run: `pytest --version`
Expected: prints a pytest version, exit 0.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add pytest for the ship loadout calculator's test suite"
```

---

## Task 1: Ingest ship and module stats from coriolis-data

**Files:**
- Create: `ingest_ship_modules.py`
- Test: `tests/test_ingest_ship_modules.py`

**Interfaces:**
- Produces: `ship_stats(ship_key, ed_id, name, manufacturer, hull_mass, properties_json, slots_json, defaults_json, bulkheads_json)`, `module_stats(symbol, coriolis_id, ed_id, grp, class, rating, mass, power, cost, extra_json)`. `symbol` is always lowercased (matches the journal's own `Item` field casing-insensitively). `extra_json` holds every field not already pulled into a real column — for a power plant this is where its real output lives, under the key `pgen` (**not** `power` — verified: `modules/standard/power_plant.json` has no `power` field at all, only `pgen`; every *consuming* module family does use `power` for draw). `build_ships(conn, repo_path) -> int`, `build_modules(conn, repo_path) -> int`, `build_schema(conn)`, all importable from `ingest_ship_modules`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_ship_modules.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_ship_modules as ism

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"

# every module symbol from the commander's own real, current ship
# (Journal.*.log, Loadout event, 2026-09-19T16:11:31Z) -- verified present
# in coriolis-data before writing this test, not assumed.
REAL_FITTED_SYMBOLS = [
    "int_powerplant_size4_class5",
    "int_engine_size7_class2",
    "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
    "int_lifesupport_size5_class2",
    "int_powerdistributor_size5_class2",
    "int_sensors_size8_class2",
    "int_fueltank_size7_class3",
    "int_fuelscoop_size7_class5",
    "int_repairer_size4_class5",
    "int_cargorack_size2_class1",
    "int_dockingcomputer_advanced",
]


def _fresh_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    ism.build_schema(conn)
    return conn


def test_ingest_ships_and_modules_from_real_repo(tmp_path):
    conn = _fresh_db(tmp_path)
    ship_count = ism.build_ships(conn, str(REPO))
    module_count = ism.build_modules(conn, str(REPO))
    conn.commit()

    assert ship_count > 30
    assert module_count > 100

    row = conn.execute(
        "SELECT name, hull_mass FROM ship_stats WHERE ship_key = 'python'"
    ).fetchone()
    assert row is not None
    assert row[0] == "Python"
    assert row[1] == 350
    conn.close()


def test_every_real_fitted_module_resolves(tmp_path):
    """Every module on the commander's own actual ship must ingest --
    this is the real regression check, not a synthetic fixture."""
    conn = _fresh_db(tmp_path)
    ism.build_modules(conn, str(REPO))
    conn.commit()

    missing = []
    for symbol in REAL_FITTED_SYMBOLS:
        row = conn.execute(
            "SELECT symbol FROM module_stats WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            missing.append(symbol)
    assert not missing, f"real fitted modules missing from module_stats: {missing}"
    conn.close()


def test_power_plant_output_is_in_extra_json_as_pgen(tmp_path):
    """Regression test for the power/pgen field-name trap found while
    writing this plan: a power plant has no 'power' column value, its
    real output lives in extra_json['pgen']."""
    conn = _fresh_db(tmp_path)
    ism.build_modules(conn, str(REPO))
    conn.commit()

    row = conn.execute(
        "SELECT power, extra_json FROM module_stats WHERE symbol = ?",
        ("int_powerplant_size4_class5",),
    ).fetchone()
    assert row is not None
    power_col, extra_json = row
    assert power_col is None
    extra = json.loads(extra_json)
    assert extra["pgen"] == 15.6
    conn.close()


def test_rerun_is_safe_and_idempotent(tmp_path):
    conn = _fresh_db(tmp_path)
    ism.build_ships(conn, str(REPO))
    ism.build_modules(conn, str(REPO))
    conn.commit()
    first_ships = conn.execute("SELECT COUNT(*) FROM ship_stats").fetchone()[0]

    ism.build_schema(conn)  # simulates re-running the whole script
    ism.build_ships(conn, str(REPO))
    ism.build_modules(conn, str(REPO))
    conn.commit()
    second_ships = conn.execute("SELECT COUNT(*) FROM ship_stats").fetchone()[0]

    assert first_ships == second_ships
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest_ship_modules.py -v`
Expected: `ModuleNotFoundError: No module named 'ingest_ship_modules'`

- [ ] **Step 3: Write the implementation**

Create `ingest_ship_modules.py`:

```python
"""
Ingests coriolis-data's ships/*.json and modules/{standard,internal,hardpoints}/*.json
into ship_stats and module_stats -- the base-game (unengineered) stats the
ship-loadout calculator needs for mass/jump-range/power-budget math. Same
convention as ingest_coriolis.py: only drops/recreates its own two tables,
no new download (coriolis-data is already cloned for blueprint ingestion),
safe to rerun any time.

CORE_FIELDS below are pulled into their own columns; everything else in a
module's dict is kept verbatim in extra_json -- including group-specific
fields like an FSD's optmass/fuelmul/fuelpower/maxfuel, or a power plant's
real output field, which is 'pgen', NOT 'power' (power plants have no
'power' field at all -- verified against modules/standard/power_plant.json
before this was written; every *consuming* module family uses 'power' for
draw, only the power plant itself is the odd one out).
"""
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_PATH = sys.argv[1] if len(sys.argv) > 1 else str(SCRIPT_DIR / "coriolis-data")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else str(SCRIPT_DIR / "elite-dangerous.db")

MODULE_DIRS = ["standard", "internal", "hardpoints"]
_CORE_FIELDS = {"symbol", "id", "edID", "grp", "class", "rating", "mass", "power", "cost"}


def build_schema(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS ship_stats;
        DROP TABLE IF EXISTS module_stats;
        CREATE TABLE ship_stats (
            ship_key TEXT PRIMARY KEY, ed_id INTEGER, name TEXT, manufacturer TEXT,
            hull_mass REAL, properties_json TEXT, slots_json TEXT, defaults_json TEXT,
            bulkheads_json TEXT
        );
        CREATE TABLE module_stats (
            symbol TEXT PRIMARY KEY, coriolis_id TEXT, ed_id INTEGER, grp TEXT,
            class INTEGER, rating TEXT, mass REAL, power REAL, cost INTEGER,
            extra_json TEXT
        );
        """
    )
    conn.commit()


def build_ships(conn, repo_path):
    count = 0
    for path in sorted((Path(repo_path) / "ships").glob("*.json")):
        data = json.load(open(path, encoding="utf-8"))
        for ship_key, entry in data.items():
            props = entry.get("properties", {})
            conn.execute(
                "INSERT OR REPLACE INTO ship_stats VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ship_key,
                    entry.get("edID"),
                    props.get("name"),
                    props.get("manufacturer"),
                    props.get("hullMass"),
                    json.dumps(props),
                    json.dumps(entry.get("slots", {})),
                    json.dumps(entry.get("defaults", {})),
                    json.dumps(entry.get("bulkheads", [])),
                ),
            )
            count += 1
    conn.commit()
    return count


def build_modules(conn, repo_path):
    count = 0
    for subdir in MODULE_DIRS:
        for path in sorted((Path(repo_path) / "modules" / subdir).glob("*.json")):
            data = json.load(open(path, encoding="utf-8"))
            for _group_key, variants in data.items():
                if not isinstance(variants, list):
                    continue
                for m in variants:
                    symbol = m.get("symbol")
                    if not symbol:
                        continue
                    extra = {k: v for k, v in m.items() if k not in _CORE_FIELDS}
                    conn.execute(
                        "INSERT OR REPLACE INTO module_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            symbol.lower(),
                            m.get("id"),
                            m.get("edID"),
                            m.get("grp"),
                            m.get("class"),
                            m.get("rating"),
                            m.get("mass"),
                            m.get("power"),
                            m.get("cost"),
                            json.dumps(extra),
                        ),
                    )
                    count += 1
    conn.commit()
    return count


def main():
    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)
    ship_count = build_ships(conn, REPO_PATH)
    module_count = build_modules(conn, REPO_PATH)
    print(f"ship_stats: {ship_count} rows")
    print(f"module_stats: {module_count} rows")
    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest_ship_modules.py -v`
Expected: 4 passed

- [ ] **Step 5: Run it for real against the project's actual database**

Run: `python ingest_ship_modules.py`
Expected: prints real `ship_stats`/`module_stats` row counts (ship_stats should be in the 30s; module_stats in the low hundreds).

- [ ] **Step 6: Commit**

```bash
git add ingest_ship_modules.py tests/test_ingest_ship_modules.py
git commit -m "Add ship_stats/module_stats ingestion from coriolis-data"
```

---

## Task 2: Capture the journal's Loadout event

**Files:**
- Modify: `journal_watcher.py` (add to `build_schema`, add a new `process_event` branch)
- Test: `tests/test_journal_watcher_loadout.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `current_loadout(id, ship_key, ship_id, ship_name, max_jump_range, unladen_mass, cargo_capacity, fuel_main, fuel_reserve, hull_value, modules_value, rebuy, last_updated)` (singleton, `id=1`), `current_loadout_modules(slot, item, is_on, priority, health, value, engineering_blueprint, engineering_level, engineering_quality, engineering_experimental, engineering_modifiers_json, last_updated)`. Both replaced wholesale on every `Loadout` event (delete-all-then-insert, matching `reset_materials`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_journal_watcher_loadout.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw

# the commander's own real Loadout event (Journal.*.log, 2026-09-19T16:11:31Z),
# trimmed to the fields this test needs -- not synthesized.
REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z",
    "event": "Loadout",
    "Ship": "explorer_nx",
    "ShipID": 48,
    "ShipName": " ",
    "ShipIdent": "XP-13E",
    "HullValue": 177925284,
    "ModulesValue": 180610931,
    "HullHealth": 1.0,
    "UnladenMass": 1286.300049,
    "CargoCapacity": 4,
    "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14},
    "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0, "Value": 1405203},
        {
            "Slot": "FrameShiftDrive",
            "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
            "On": True, "Priority": 0, "Health": 1.0, "Value": 79991013,
            "Engineering": {
                "Engineer": "Elvira Martuuk", "EngineerID": 300160,
                "BlueprintID": 128673694, "BlueprintName": "FSD_LongRange",
                "Level": 5, "Quality": 0.6,
                "ExperimentalEffect": "special_fsd_heavy",
                "ExperimentalEffect_Localised": "Mass Manager",
                "Modifiers": [
                    {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
                    {"Label": "Integrity", "Value": 147.016006, "OriginalValue": 188.0, "LessIsGood": 0},
                    {"Label": "PowerDraw", "Value": 1.2075, "OriginalValue": 1.05, "LessIsGood": 1},
                    {"Label": "FSDOptimalMass", "Value": 7333.768066, "OriginalValue": 4670.0, "LessIsGood": 0},
                ],
            },
        },
        {"Slot": "CargoHatch", "Item": "modularcargobaydoor", "On": True, "Priority": 2, "Health": 1.0},
    ],
}


def _fresh_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    jw.build_schema(conn)
    return conn


def test_loadout_populates_current_loadout(tmp_path):
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    row = conn.execute("SELECT * FROM current_loadout WHERE id = 1").fetchone()
    assert row is not None
    cols = [d[0] for d in conn.execute("SELECT * FROM current_loadout").description]
    data = dict(zip(cols, row))
    assert data["ship_key"] == "explorer_nx"
    assert data["max_jump_range"] == 73.964989
    assert data["unladen_mass"] == 1286.300049
    assert data["fuel_main"] == 128.0
    assert data["fuel_reserve"] == 1.14
    conn.close()


def test_loadout_populates_modules_with_real_engineering(tmp_path):
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    row = conn.execute(
        "SELECT item, engineering_blueprint, engineering_level, engineering_quality, "
        "engineering_experimental, engineering_modifiers_json "
        "FROM current_loadout_modules WHERE slot = 'FrameShiftDrive'"
    ).fetchone()
    assert row is not None
    item, blueprint, level, quality, experimental, modifiers_json = row
    assert item == "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii"
    assert blueprint == "FSD_LongRange"
    assert level == 5
    assert quality == 0.6
    assert experimental == "special_fsd_heavy"
    modifiers = json.loads(modifiers_json)
    optmass_mod = next(m for m in modifiers if m["Label"] == "FSDOptimalMass")
    assert optmass_mod["Value"] == 7333.768066

    # a module with no Engineering block has NULL engineering fields, not "None" strings
    plant = conn.execute(
        "SELECT engineering_blueprint FROM current_loadout_modules WHERE slot = 'PowerPlant'"
    ).fetchone()
    assert plant[0] is None
    conn.close()


def test_second_loadout_event_replaces_modules_wholesale(tmp_path):
    """A slot no longer present in a later Loadout event must not linger --
    same snapshot-replace semantics as Materials/reset_materials."""
    conn = _fresh_conn(tmp_path)
    jw.process_event(conn, REAL_LOADOUT_EVENT)

    smaller_event = dict(REAL_LOADOUT_EVENT)
    smaller_event["Modules"] = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0}
    ]
    jw.process_event(conn, smaller_event)

    remaining = conn.execute("SELECT slot FROM current_loadout_modules").fetchall()
    assert remaining == [("PowerPlant",)]
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_journal_watcher_loadout.py -v`
Expected: FAIL — `current_loadout` table doesn't exist yet (`build_schema` doesn't create it).

- [ ] **Step 3: Add the two tables to `build_schema`**

In `journal_watcher.py`, inside the `conn.executescript(...)` call in `build_schema`, add after the existing `engineer_progress` table definition (before the closing `"""`):

```sql
        CREATE TABLE IF NOT EXISTS current_loadout (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ship_key TEXT, ship_id INTEGER, ship_name TEXT,
            max_jump_range REAL, unladen_mass REAL, cargo_capacity REAL,
            fuel_main REAL, fuel_reserve REAL,
            hull_value INTEGER, modules_value INTEGER, rebuy INTEGER,
            last_updated TEXT
        );
        INSERT OR IGNORE INTO current_loadout (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS current_loadout_modules (
            slot TEXT PRIMARY KEY,
            item TEXT, is_on INTEGER, priority INTEGER, health REAL, value INTEGER,
            engineering_blueprint TEXT, engineering_level INTEGER, engineering_quality REAL,
            engineering_experimental TEXT, engineering_modifiers_json TEXT,
            last_updated TEXT
        );
```

- [ ] **Step 4: Add the `Loadout` handler**

In `journal_watcher.py`, add this function near `reset_materials` (same file, same style):

```python
def set_loadout(conn, event):
    """Loadout is a full snapshot of the current ship, fired on launch, on
    any module change, and on request -- replace both tables wholesale,
    the same convention reset_materials() already uses for Materials."""
    now = _now_iso()
    fuel = event.get("FuelCapacity") or {}
    conn.execute(
        "UPDATE current_loadout SET ship_key=?, ship_id=?, ship_name=?, max_jump_range=?, "
        "unladen_mass=?, cargo_capacity=?, fuel_main=?, fuel_reserve=?, hull_value=?, "
        "modules_value=?, rebuy=?, last_updated=? WHERE id = 1",
        (
            event.get("Ship"), event.get("ShipID"), event.get("ShipName"),
            event.get("MaxJumpRange"), event.get("UnladenMass"), event.get("CargoCapacity"),
            fuel.get("Main"), fuel.get("Reserve"),
            event.get("HullValue"), event.get("ModulesValue"), event.get("Rebuy"),
            now,
        ),
    )

    conn.execute("DELETE FROM current_loadout_modules")
    for m in event.get("Modules", []):
        eng = m.get("Engineering") or {}
        conn.execute(
            "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                m.get("Slot"), m.get("Item"), int(bool(m.get("On"))), m.get("Priority"),
                m.get("Health"), m.get("Value"),
                eng.get("BlueprintName"), eng.get("Level"), eng.get("Quality"),
                eng.get("ExperimentalEffect"),
                json.dumps(eng["Modifiers"]) if "Modifiers" in eng else None,
                now,
            ),
        )
    conn.commit()
```

Then add a branch to `process_event`'s `if/elif` chain (anywhere among the other `elif etype == "..."` branches):

```python
    elif etype == "Loadout":
        set_loadout(conn, event)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_journal_watcher_loadout.py -v`
Expected: 3 passed

- [ ] **Step 6: Run the full existing test suite to check nothing broke**

Run: `pytest tests/ -v`
Expected: all tests pass (Task 1's + Task 2's).

- [ ] **Step 7: Commit**

```bash
git add journal_watcher.py tests/test_journal_watcher_loadout.py
git commit -m "Capture the journal's Loadout event (current ship, mass, jump range, real engineering)"
```

---

## Task 3: `get_current_loadout()` and `get_ship_info()` query functions

**Files:**
- Modify: `query_functions.py`
- Test: `tests/test_ship_loadout_queries.py`

**Interfaces:**
- Consumes: `current_loadout`/`current_loadout_modules` (Task 2), `ship_stats` (Task 1).
- Produces: `get_current_loadout() -> dict`, `get_ship_info(ship_name: str) -> dict`, both decorated with the existing `@_needs_setup`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ship_loadout_queries.py`:

```python
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"

REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z", "event": "Loadout",
    "Ship": "explorer_nx", "ShipID": 48, "ShipName": " ", "ShipIdent": "XP-13E",
    "HullValue": 177925284, "ModulesValue": 180610931, "HullHealth": 1.0,
    "UnladenMass": 1286.300049, "CargoCapacity": 4, "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14}, "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0},
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii", "On": True, "Priority": 0, "Health": 1.0},
    ],
}


def _db(tmp_path, with_loadout=True, with_ship_modules=True):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    if with_ship_modules:
        ism.build_schema(conn)
        ism.build_ships(conn, str(REPO))
        ism.build_modules(conn, str(REPO))
    if with_loadout:
        jw.process_event(conn, REAL_LOADOUT_EVENT)
    conn.close()
    qf.DB_PATH = path
    return path


def test_get_current_loadout_returns_real_data(tmp_path):
    _db(tmp_path)
    result = qf.get_current_loadout()
    assert result["found"] is True
    assert result["ship"] == "explorer_nx"
    assert result["max_jump_range"] == 73.964989
    assert any(m["slot"] == "FrameShiftDrive" for m in result["modules"])


def test_get_current_loadout_before_any_loadout_event(tmp_path):
    """Review Focus #1: the table exists but is empty (fresh session,
    haven't undocked yet) -- a clean message, not a crash."""
    _db(tmp_path, with_loadout=False)
    result = qf.get_current_loadout()
    assert result["found"] is False
    assert "reason" in result


def test_get_ship_info_known_ship(tmp_path):
    _db(tmp_path)
    result = qf.get_ship_info("Python")
    assert result["found"] is True
    assert result["hull_mass"] == 350

def test_get_ship_info_unknown_ship(tmp_path):
    _db(tmp_path)
    result = qf.get_ship_info("Not A Real Ship")
    assert result["found"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ship_loadout_queries.py -v`
Expected: FAIL — `AttributeError: module 'query_functions' has no attribute 'get_current_loadout'`

- [ ] **Step 3: Implement both functions**

Add to `query_functions.py` (near the other functions, same style — `@_needs_setup`, `_connect()`, plain dict returns):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ship_loadout_queries.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add query_functions.py tests/test_ship_loadout_queries.py
git commit -m "Add get_current_loadout and get_ship_info query functions"
```

---

## Task 4: The calculation core (jump range, blueprint deltas)

This is the highest-risk piece — see the spec's "Open implementation risks." The formula below is not asserted from memory: it was derived and verified against the commander's own real ship before this plan was written (computed 73.964990 ly against his real, game-computed `MaxJumpRange` of 73.964989 — a 0.000001 ly difference from floating point only).

**Files:**
- Create: `loadout_calc.py`
- Test: `tests/test_loadout_calc.py`

**Interfaces:**
- Consumes: nothing (pure functions, no database).
- Produces: `compute_jump_range(unladen_mass, fsd_optmass, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower) -> float`, `apply_blueprint_delta(base_value, pct_range) -> (float, float)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_loadout_calc.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_loadout_calc.py -v`
Expected: `ModuleNotFoundError: No module named 'loadout_calc'`

- [ ] **Step 3: Write the implementation**

Create `loadout_calc.py`:

```python
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


def compute_jump_range(unladen_mass, fsd_optmass, fsd_maxfuel, fsd_fuelmul, fsd_fuelpower):
    """Max single-jump range in ly. total_mass is the ship's unladen mass
    plus exactly one jump's worth of fuel (fsd_maxfuel) -- that combination
    is what "max jump range" means in-game, verified above."""
    total_mass = unladen_mass + fsd_maxfuel
    return ((fsd_maxfuel / fsd_fuelmul) ** (1 / fsd_fuelpower)) * fsd_optmass / total_mass


def apply_blueprint_delta(base_value, pct_range):
    """pct_range is Coriolis's own [min_pct, max_pct] (e.g. [0.45, 0.55] for
    a +45% to +55% roll-dependent change). Returns (min_value, max_value) --
    equal when the blueprint grade is deterministic for this stat
    (min_pct == max_pct), never a fabricated spread (Review Focus #5)."""
    min_pct, max_pct = pct_range
    return (base_value * (1 + min_pct), base_value * (1 + max_pct))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_loadout_calc.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add loadout_calc.py tests/test_loadout_calc.py
git commit -m "Add the verified jump-range and blueprint-delta calculation core"
```

---

## Task 5: `simulate_module_swap()` — the core tool

**Files:**
- Modify: `query_functions.py`
- Test: `tests/test_simulate_module_swap.py`

**Interfaces:**
- Consumes: `loadout_calc.compute_jump_range`, `loadout_calc.apply_blueprint_delta` (Task 4); `ship_stats`/`module_stats` (Task 1); `current_loadout`/`current_loadout_modules` (Task 2); `coriolis_blueprints` (already existed before this plan, used by `get_blueprint`).
- Produces: `simulate_module_swap(module_group, module_class, module_rating, blueprint_name=None, grade=None, slot=None, variant=None) -> dict`, plus a new `MODULE_GROUP_TO_GRP` mapping dict in `query_functions.py`. When several real modules share the requested group/class/rating (100 such combinations exist in coriolis-data -- every FSD class has a standard and an SCO variant, weapons have fixed/gimballed/turret mounts, power plants have standard and Guardian), the function returns `{"found": False, "reason": ..., "variants": [...]}` listing them rather than picking one; `variant` (a flat, optional string) narrows the match.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_simulate_module_swap.py`:

```python
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import journal_watcher as jw
import ingest_ship_modules as ism
import ingest_coriolis as ic

REPO = Path(__file__).resolve().parent.parent / "coriolis-data"

REAL_LOADOUT_EVENT = {
    "timestamp": "2026-09-19T16:11:31Z", "event": "Loadout",
    "Ship": "explorer_nx", "ShipID": 48, "ShipName": " ", "ShipIdent": "XP-13E",
    "HullValue": 177925284, "ModulesValue": 180610931, "HullHealth": 1.0,
    "UnladenMass": 1286.300049, "CargoCapacity": 4, "MaxJumpRange": 73.964989,
    "FuelCapacity": {"Main": 128.0, "Reserve": 1.14}, "Rebuy": 17926810,
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size4_class5", "On": True, "Priority": 1, "Health": 1.0},
        {
            "Slot": "FrameShiftDrive",
            "Item": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
            "On": True, "Priority": 0, "Health": 1.0,
            "Engineering": {
                "Engineer": "Elvira Martuuk", "BlueprintName": "FSD_LongRange",
                "Level": 5, "Quality": 0.6, "ExperimentalEffect": "special_fsd_heavy",
                "Modifiers": [
                    {"Label": "Mass", "Value": 208.0, "OriginalValue": 160.0, "LessIsGood": 1},
                    {"Label": "PowerDraw", "Value": 1.2075, "OriginalValue": 1.05, "LessIsGood": 1},
                    {"Label": "FSDOptimalMass", "Value": 7333.768066, "OriginalValue": 4670.0, "LessIsGood": 0},
                ],
            },
        },
    ],
}


def _db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    ism.build_schema(conn)
    ism.build_ships(conn, str(REPO))
    ism.build_modules(conn, str(REPO))
    ic.build_schema(conn)
    ic.ingest_blueprints(conn, __import__("json").load(open(REPO / "modifications" / "blueprints.json")))
    ic.ingest_specials(conn, __import__("json").load(open(REPO / "modifications" / "specials.json")))
    jw.process_event(conn, REAL_LOADOUT_EVENT)
    conn.commit()
    conn.close()
    qf.DB_PATH = path
    return path


def test_swap_unengineered_module_reports_deltas(tmp_path):
    _db(tmp_path)
    # explorer_nx's FrameShiftDrive is a size-8 slot. Class 8 rating A has two
    # real variants (both SCO: the Mk II the commander fits, and the plain SCO
    # drive) -- ask for the plain one explicitly, unengineered.
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A",
        variant="int_hyperdrive_overcharge_size8_class5",
    )
    assert result["found"] is True
    assert result["compatible"] is True
    assert result["slot"] == "FrameShiftDrive"
    assert "new_jump_range" in result
    assert "new_unladen_mass" in result


def test_swap_with_blueprint_applies_deterministic_delta(tmp_path):
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name="Increased range", grade=1,
        variant="int_hyperdrive_size7_class1",  # the standard (non-SCO) 7E drive
    )
    assert result["found"] is True
    assert result["compatible"] is True
    # grade 1 mass delta [0.1, 0.1] is deterministic -- a single number,
    # not a {"min", "max"} dict presenting a fake spread (Review Focus #5)
    assert isinstance(result["added"]["mass"], (int, float))


def test_swap_class_too_big_for_slot_is_incompatible_not_wrong_number(tmp_path):
    _db(tmp_path)
    # explorer_nx's FSD slot is class 8 -- class 9 doesn't exist for any ship.
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=9, module_rating="A")
    assert result["found"] is False


def test_unknown_module_group_fails_cleanly(tmp_path):
    """Review Focus #3: a module_group the mapping doesn't recognize."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="warp core", module_class=1, module_rating="A")
    assert result["found"] is False
    assert "reason" in result


def test_blueprint_not_applicable_to_module_category_fails_cleanly(tmp_path):
    """Review Focus #4: a real blueprint name, wrong category."""
    _db(tmp_path)
    result = qf.simulate_module_swap(
        module_group="life support", module_class=5, module_rating="D",
        blueprint_name="Increased range", grade=1,  # an FSD blueprint
    )
    assert result["found"] is False


def test_blueprint_effect_this_code_cant_apply_is_reported_not_dropped(tmp_path):
    """A features key with no numeric-application logic (this plan's
    simulate_module_swap only knows mass/power/optmass) must show up in
    unmodeled_blueprint_effects, never be silently ignored."""
    _db(tmp_path)
    # FSD_Shielded grade 1's real features (verified against
    # modifications/blueprints.json): integrity, mass, optmass, thermload.
    # This code applies mass and optmass (fsd) -- integrity and thermload
    # must be named as unmodeled, not silently dropped.
    result = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=7, module_rating="E",
        blueprint_name="Shielded", grade=1,
        variant="int_hyperdrive_size7_class1",
    )
    assert result["found"] is True
    assert set(result["unmodeled_blueprint_effects"]) == {"integrity", "thermload"}


def test_guardian_fsd_booster_adds_flat_bonus_to_jump_range(tmp_path):
    """A fitted Guardian FSD Booster contributes a flat additive bonus to
    the reported jump range -- verified against real coriolis-data (class
    2 -> jumpboost 6 ly). Swapping something OTHER than the FSD or the
    booster must still pick up the currently-fitted booster's
    contribution when computing the resulting range."""
    path = _db(tmp_path)
    without = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Slot08_Size4", "int_guardianfsdbooster_size2", 1, 1, 1.0, None,
         None, None, None, None, None, "2026-09-19T16:11:31Z"),
    )
    conn.commit()
    conn.close()
    with_booster = qf.simulate_module_swap(module_group="life support", module_class=5, module_rating="D")

    assert without["found"] is True and with_booster["found"] is True
    # no blueprint on this swap, so both sides collapse to a plain float
    # (see _min_max_or_single) rather than a {"min","max"} dict
    assert isinstance(without["new_jump_range"], (int, float))
    assert isinstance(with_booster["new_jump_range"], (int, float))
    assert abs((with_booster["new_jump_range"] - without["new_jump_range"]) - 6) < 0.01


def test_nothing_fitted_of_that_group_fails_cleanly_not_a_crash(tmp_path):
    """Review Focus #2: nothing of this group is currently fitted (no row
    in current_loadout_modules at all) -- Phase A doesn't guess which
    empty slot is meant (see the plan's honest scope note), but it must
    fail with a clear, actionable reason, never a KeyError/crash, and
    never silently pick a slot."""
    _db(tmp_path)
    # shield boosters are utility mounts: class 0, ratings A-E, one module
    # each (verified against coriolis-data)
    result = qf.simulate_module_swap(module_group="shield booster", module_class=0, module_rating="A")
    assert result["found"] is False
    assert "slot" in result["reason"].lower()

    # the explicit-slot escape hatch must work for this same case --
    # "TinyHardpoint2" is the journal's real naming for a utility mount
    # (the commander's real ship has a heat sink in TinyHardpoint1)
    result_with_slot = qf.simulate_module_swap(
        module_group="shield booster", module_class=0, module_rating="A", slot="TinyHardpoint2"
    )
    assert result_with_slot["found"] is True


def test_ambiguous_module_lists_variants_instead_of_guessing(tmp_path):
    """Several real modules share one group/class/rating with materially
    different stats (FSD 8A: the plain SCO drive has maxfuel 20.7, the Mk II
    has 6.8). Without a variant the tool must list them, never pick one;
    a partial variant string must narrow to exactly one."""
    _db(tmp_path)
    result = qf.simulate_module_swap(module_group="frame shift drive", module_class=8, module_rating="A")
    assert result["found"] is False
    symbols = {v["symbol"] for v in result["variants"]}
    assert symbols == {
        "int_hyperdrive_overcharge_size8_class5",
        "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
    }

    narrowed = qf.simulate_module_swap(
        module_group="frame shift drive", module_class=8, module_rating="A", variant="mk ii"
    )
    assert narrowed["found"] is True
    assert narrowed["added"]["symbol"] == "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_simulate_module_swap.py -v`
Expected: `AttributeError: module 'query_functions' has no attribute 'simulate_module_swap'`

- [ ] **Step 3: Write the implementation**

Add to `query_functions.py`. First, the module-group mapping (near `blueprint_mapping`'s import, module level) — covers the common cases; anything not listed can still be reached by passing the raw Coriolis `grp` code directly, so nothing is unreachable, just less convenient for rare modules:

```python
from loadout_calc import compute_jump_range, apply_blueprint_delta

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
    """Which slot a swap targets. Resolves from what's currently fitted,
    matched by module group. When NOTHING of that group is fitted, there's
    no verified way to map Coriolis's slot-size layout to the journal's
    own slot names (see the plan's Review Focus #2 scope note) -- rather
    than guess, this asks for an explicit slot, the same honest treatment
    bulkheads already get."""
    if explicit_slot:
        return explicit_slot, None

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
```

- [ ] **Step 4: Run tests, fix what fails**

Run: `pytest tests/test_simulate_module_swap.py -v`

This function is intricate enough that the first run will very likely surface at least one real bug (a wrong join, a `None` not handled) — that's expected and is exactly what this test suite is for. Fix issues in `simulate_module_swap` (not the tests, which encode real verified facts) until all 9 tests pass. Re-run after each fix.

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -v`
Expected: every test across all tasks so far passes.

- [ ] **Step 6: Commit**

```bash
git add query_functions.py tests/test_simulate_module_swap.py
git commit -m "Add simulate_module_swap: the core what-if loadout calculator"
```

---

## Task 6: Wire the three tools into `ellie_chat.py`

**Files:**
- Modify: `ellie_chat.py`
- Test: `tests/test_ellie_chat_ship_tools.py`

**Interfaces:**
- Consumes: `query_functions.get_current_loadout`, `.get_ship_info`, `.simulate_module_swap` (Tasks 3, 5).
- Produces: three new entries in `TOOLS` and `DISPATCH`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ellie_chat_ship_tools.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def test_new_tools_are_dispatched():
    for name in ("get_current_loadout", "get_ship_info", "simulate_module_swap"):
        assert name in ec.DISPATCH, f"{name} missing from DISPATCH"
        tool_names = [t["function"]["name"] for t in ec.TOOLS]
        assert name in tool_names, f"{name} missing from TOOLS"


def test_call_tool_reaches_get_ship_info(monkeypatch):
    calls = []
    monkeypatch.setitem(ec.DISPATCH, "get_ship_info", lambda ship_name: calls.append(ship_name) or {"found": False})
    ec.call_tool("get_ship_info", {"ship_name": "Python"})
    assert calls == ["Python"]


def test_call_tool_bad_args_for_simulate_module_swap_is_clean_error():
    result = ec.call_tool("simulate_module_swap", {"wrong_kw": 1})
    assert "error" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ellie_chat_ship_tools.py -v`
Expected: FAIL — the three tools aren't in `TOOLS`/`DISPATCH` yet.

- [ ] **Step 3: Add the tool definitions**

In `ellie_chat.py`, add to the `TOOLS` list (same style as the existing 7 entries):

```python
    {
        "type": "function",
        "function": {
            "name": "get_current_loadout",
            "description": "Get the commander's current ship, its real mass and jump range as reported by the game, and every fitted module including real engineering modifiers.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ship_info",
            "description": "Get a named ship's base stats and slot layout.",
            "parameters": {
                "type": "object",
                "properties": {"ship_name": {"type": "string", "description": "Ship name, e.g. 'Python'"}},
                "required": ["ship_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_module_swap",
            "description": "'What if I fit a different module' -- swaps one module in the commander's CURRENT ship for a hypothetical one and reports the real resulting mass, max jump range (unladen, one jump's fuel), and power changes, or whether it's compatible at all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_group": {"type": "string", "description": "e.g. 'frame shift drive', 'power plant', 'shield booster'"},
                    "module_class": {"type": "integer", "description": "Module class/size, 0-8 (utility mounts such as shield boosters are class 0)"},
                    "module_rating": {"type": "string", "description": "Module rating letter, e.g. 'A'"},
                    "blueprint_name": {"type": "string", "description": "Optional engineering blueprint to apply to the hypothetical module, e.g. 'Increased range'"},
                    "grade": {"type": "integer", "description": "Blueprint grade 1-5, required if blueprint_name is given"},
                    "slot": {"type": "string", "description": "Optional exact slot name, e.g. 'Slot05_Size4' or 'MediumHardpoint2' -- needed if the ship has more than one slot of that type, or to put the module in an empty slot"},
                    "variant": {"type": "string", "description": "Optional, only needed when several modules share the same type/class/rating -- e.g. 'gimballed', 'SCO', 'Mk II', 'guardian', or an exact symbol from a previous result's variants list"},
                },
                "required": ["module_group", "module_class", "module_rating"],
            },
        },
    },
```

And to `DISPATCH`:

```python
    "get_current_loadout": qf.get_current_loadout,
    "get_ship_info": qf.get_ship_info,
    "simulate_module_swap": qf.simulate_module_swap,
```

- [ ] **Step 4: Tell the model about `unmodeled_blueprint_effects`, `variants` and `caveat`**

`simulate_module_swap` can return two things the model must handle deliberately: a real, honestly-reported gap (a blueprint effect it couldn't quantify), and an ambiguity (`"found": False` plus a `"variants"` list, when several real modules share the requested type/class/rating). The existing `"found": false` rule would make the model tell the commander "I don't have that data" in the ambiguity case, which is wrong — the data exists, the request just needs narrowing. A third key, `"caveat"`, appears when the swap targets an explicit internal slot that isn't fitted: the journal never lists empty slots, so Ellie can't confirm that slot exists on the ship (fix round 2, S3), and the commander must hear that alongside the numbers. In `ellie_chat.py`, add three lines to `SYSTEM_PROMPT` (next to the existing `"found": false` rule), and replace that existing rule with the merged one below. `simulate_module_swap` also uses `"found": false` + `"reason"` to ask for a missing detail ("specify which slot", "a blueprint grade (1-5) is required"), which the old blanket rule would misreport as missing data -- and a separate "reason" rule alongside the old one gave a small model two conflicting instructions for the same result (Task 6 review), so it is one rule:

```python
- If a tool result has a non-empty "unmodeled_blueprint_effects" list, mention plainly that those specific effects aren't reflected in the numbers -- don't just report the numbers as if they were the whole picture.
- If a tool result has a "variants" list, the request matched several different real modules -- list them for the commander and ask which one they mean (then call the tool again with `variant`); don't say the data is missing, and don't pick one yourself.
- If a tool result has "found": false, don't guess or fall back to general knowledge. If it has a "reason", pass that reason on in plain words -- it may be asking for a detail (like which slot) rather than saying the data is missing. Otherwise, tell the commander plainly that you don't have that data.
- If a tool result has a "caveat", tell the commander it along with the numbers -- never drop it.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_ellie_chat_ship_tools.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add ellie_chat.py tests/test_ellie_chat_ship_tools.py
git commit -m "Wire loadout tools into ellie_chat.py"
```

---

## Task 7: `setup.py` step

**Files:**
- Modify: `setup.py`
- Test: manual, against the real project (this step is a thin, mechanical mirror of `setup_coriolis()`, which is already exercised every time `setup.py` runs)

**Interfaces:**
- Consumes: `ingest_ship_modules.build_ships`/`build_modules` (Task 1); `CORIOLIS_DIR` (already exists in `setup.py`).

- [ ] **Step 1: Add `setup_ship_modules()`**

In `setup.py`, add a new function mirroring `setup_coriolis()` exactly in shape:

```python
def setup_ship_modules():
    _heading("Ship & module stats (loadout calculator)")
    existing = _table_count("ship_stats")
    if existing:
        print(f"ship_stats already has {existing} rows.")
        if not _ask_yes_no("Re-ingest anyway?", default=False):
            print("Skipped.")
            return True

    if not CORIOLIS_MARKER.exists():
        print("Coriolis data isn't cloned yet -- run the Coriolis step above first.")
        return False

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_ship_modules as ism
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False

    conn = sqlite3.connect(DB_PATH)
    ism.build_schema(conn)
    ship_count = ism.build_ships(conn, str(CORIOLIS_DIR))
    module_count = ism.build_modules(conn, str(CORIOLIS_DIR))
    blueprint_count = ism.build_module_blueprints(conn, str(CORIOLIS_DIR))
    conn.close()
    print(f"ship_stats: {ship_count} rows")
    print(f"module_stats: {module_count} rows")
    print(f"module_blueprints: {blueprint_count} rows")
    return True
```

Add it to `main()`, right after `setup_coriolis()`:

```python
    setup_coriolis()
    setup_ship_modules()
    setup_edsm()
```

Add `"Ship/module stats (loadout calculator)": ["ship_stats", "module_stats", "module_blueprints"]` to the `STEP_TABLES` dict, in the same position (after the Coriolis entry).

- [ ] **Step 2: Run it for real**

Run: `python setup.py` against the real project. `ship_stats` doesn't exist yet, so it ingests without asking (the "already has N rows, re-ingest?" prompt only fires when the table is already populated).
Expected: prints real `ship_stats`/`module_stats` row counts, and the final readiness report shows the new group as `[ready]`.

- [ ] **Step 3: Re-run to confirm the safe-rerun behavior**

Run: `python setup.py` a second time, and answer `n` when asked to re-ingest.
Expected: "Skipped." — and the readiness report still shows `[ready]` (skipping doesn't blank the tables).

- [ ] **Step 4: Commit**

```bash
git add setup.py
git commit -m "Add ship/module stats to setup.py"
```

---

## Task 8: End-to-end check against the real database

Not a new file — a final real-world sanity pass before calling Phase A done, the same way Phase 5b and the chat window were both checked against real data before shipping.

- [ ] **Step 1: Run the full test suite one more time**

Run: `pytest tests/ -v`
Expected: everything passes.

- [ ] **Step 2: Ask a real question through the real chat loop**

Run: `python ellie_chat.py`, ask something like *"What's my current jump range?"* and *"What if I fit a Class 7 Rating E FSD instead?"*.
Expected: real, correct numbers come back — not `{"error": ...}`, not a hallucinated figure.

- [ ] **Step 3: Update `plan.md`**

Add a checked-off entry under a new "Ship loadout calculator (Phase A)" section, following the exact documentation style already used for every other phase in `plan.md` (what was built, what was verified, dated). Note Phases B and C as still-future backlog items, same as the design spec already frames them.

- [ ] **Step 4: Commit**

```bash
git add plan.md
git commit -m "Phase A (ship loadout calculator) complete"
```
