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
