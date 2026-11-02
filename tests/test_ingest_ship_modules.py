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

    # Regression: int_fueltank_size7_class3 appears in both standard/ and internal/
    # with different mass values (0 vs None). Must resolve to the correct base-game
    # value (mass=0 from standard fuel_tank.json, not mass=None from internal).
    fuel_tank = conn.execute(
        "SELECT mass FROM module_stats WHERE symbol = ?",
        ("int_fueltank_size7_class3",),
    ).fetchone()
    assert fuel_tank is not None, "int_fueltank_size7_class3 missing"
    assert fuel_tank[0] == 0, f"int_fueltank_size7_class3 mass should be 0, got {fuel_tank[0]}"

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


def test_module_blueprints_allow_list_matches_coriolis(tmp_path):
    """S4: module_blueprints is a per-grp ALLOW-LIST (which fdnames a grp
    can really take), not the coarse applies_to category text -- verified
    against modifications/modules.json directly. tp only really has
    LightWeight/Sturdy (not Long range); mc and bl both can take an
    "Overcharged" blueprint, but under two different, differently-featured
    fdnames (this is exactly what let round 1's category-text match pick
    the wrong one silently)."""
    conn = _fresh_db(tmp_path)
    count = ism.build_module_blueprints(conn, str(REPO))
    conn.commit()
    assert count > 0

    def fdnames(grp):
        rows = conn.execute("SELECT fdname FROM module_blueprints WHERE grp = ?", (grp,)).fetchall()
        return {r[0] for r in rows}

    assert fdnames("tp") == {"Weapon_LightWeight", "Weapon_Sturdy"}
    assert "MC_Overcharged" in fdnames("mc")
    assert "Weapon_Overcharged" not in fdnames("mc")
    assert "Weapon_Overcharged" in fdnames("bl")
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


# ---------------------------------------------------------------------------
# Task 10a X5: ingest is atomic, and inputs are checked before the database is touched
# ---------------------------------------------------------------------------

import shutil

import pytest

TABLES = ("ship_stats", "module_stats", "module_blueprints")


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def _copy_tree(src, dst):
    """Plain files and folders only -- no copied attributes. (copytree also copies
    a folder's read-only flag, which on Windows then makes the copy impossible to
    delete again.)"""
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copyfile(path, target)


def _repo_copy(tmp_path, drop=None):
    """A private copy of the parts of coriolis-data this ingest reads, so a test
    can take one input away."""
    dst = tmp_path / "repo"
    _copy_tree(REPO / "ships", dst / "ships")
    _copy_tree(REPO / "modules", dst / "modules")
    (dst / "modifications").mkdir()
    shutil.copyfile(REPO / "modifications" / "modules.json", dst / "modifications" / "modules.json")
    if drop:
        target = dst / drop
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    return dst


def _ingested_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "atomic.db"))
    ism.ingest_all(conn, str(REPO))
    counts = _counts(conn)
    assert all(n > 0 for n in counts.values()), counts
    return conn, counts


def test_ingest_all_fills_all_three_tables_and_reports_the_counts(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "all.db"))
    ships, modules, blueprints = ism.ingest_all(conn, str(REPO))
    assert (ships, modules, blueprints) == tuple(_counts(conn)[t] for t in TABLES)
    assert ships > 30 and modules > 100 and blueprints > 0
    conn.close()


def test_a_missing_blueprint_allow_list_raises_before_any_table_is_dropped(tmp_path):
    conn, before = _ingested_db(tmp_path)
    repo = _repo_copy(tmp_path, drop="modifications/modules.json")
    with pytest.raises(FileNotFoundError, match=r"modules\.json"):
        ism.ingest_all(conn, str(repo))
    assert _counts(conn) == before
    conn.close()


@pytest.mark.parametrize("missing", ["ships", "modules/internal", "modules/hardpoints", "modules/standard"])
def test_a_missing_input_folder_raises_naming_the_path_and_leaves_the_tables(tmp_path, missing):
    conn, before = _ingested_db(tmp_path)
    repo = _repo_copy(tmp_path, drop=missing)
    with pytest.raises(FileNotFoundError) as caught:
        ism.ingest_all(conn, str(repo))
    assert str(Path(*missing.split("/"))) in str(caught.value)    # the path, in the OS's own spelling
    assert _counts(conn) == before
    conn.close()


def test_an_input_folder_with_no_json_files_raises_too(tmp_path):
    conn, before = _ingested_db(tmp_path)
    repo = _repo_copy(tmp_path)
    for f in (repo / "ships").glob("*.json"):
        f.unlink()
    with pytest.raises(FileNotFoundError, match="ships"):
        ism.ingest_all(conn, str(repo))
    assert _counts(conn) == before
    conn.close()


def test_main_checks_the_inputs_before_it_even_opens_the_database(tmp_path, monkeypatch):
    repo = _repo_copy(tmp_path, drop="modifications/modules.json")
    db = tmp_path / "never_created.db"
    monkeypatch.setattr(ism, "REPO_PATH", str(repo))
    monkeypatch.setattr(ism, "DB_PATH", str(db))
    with pytest.raises(FileNotFoundError, match=r"modules\.json"):
        ism.main()
    assert not db.exists()


def test_the_individual_builders_raise_instead_of_returning_zero(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    ism.build_schema(conn)
    empty = tmp_path / "empty_repo"
    empty.mkdir()
    for build in (ism.build_ships, ism.build_modules, ism.build_module_blueprints):
        with pytest.raises(FileNotFoundError):
            build(conn, str(empty))
    conn.close()


def test_a_failure_part_way_through_leaves_the_previous_tables_with_their_old_rows(tmp_path, monkeypatch):
    conn, before = _ingested_db(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("disk full, say")

    monkeypatch.setattr(ism, "build_modules", explode)
    with pytest.raises(RuntimeError, match="disk full"):
        ism.ingest_all(conn, str(REPO))     # build_schema and build_ships already ran inside the transaction
    assert _counts(conn) == before
    assert conn.execute("SELECT name FROM ship_stats WHERE ship_key = 'python'").fetchone() == ("Python",)
    conn.close()


def test_a_failure_in_the_blueprint_step_rolls_back_the_ships_and_modules_too(tmp_path, monkeypatch):
    conn, before = _ingested_db(tmp_path)
    monkeypatch.setattr(ism, "build_module_blueprints", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad json")))
    with pytest.raises(ValueError):
        ism.ingest_all(conn, str(REPO))
    assert _counts(conn) == before
    conn.close()


def test_the_rolled_back_state_is_what_another_connection_sees(tmp_path, monkeypatch):
    conn, before = _ingested_db(tmp_path)
    monkeypatch.setattr(ism, "build_modules", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        ism.ingest_all(conn, str(REPO))
    other = sqlite3.connect(str(tmp_path / "atomic.db"))
    assert _counts(other) == before
    other.close()
    conn.close()


def test_build_schema_alone_still_commits_for_existing_callers(tmp_path):
    """The other tests (and test helpers) call build_schema/build_ships/... one by
    one and expect each to be saved; only ingest_all defers the commit."""
    path = str(tmp_path / "standalone.db")
    conn = sqlite3.connect(path)
    ism.build_schema(conn)
    ism.build_ships(conn, str(REPO))
    conn.close()
    again = sqlite3.connect(path)
    assert again.execute("SELECT COUNT(*) FROM ship_stats").fetchone()[0] > 30
    again.close()
