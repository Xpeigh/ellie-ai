"""Shared pytest setup.

The ship-loadout tests read Coriolis's real data (ships, modules, blueprints)
from the git-ignored `coriolis-data` folder that `python setup.py` clones. On a
fresh clone that folder isn't there, and those modules would fail hundreds of
tests with FileNotFoundError, which tells a newcomer nothing. Skip them with the
reason instead; every test that needs no external data still runs."""
import shutil
from pathlib import Path

import pytest

DATA_MARKER = Path(__file__).resolve().parent.parent / "coriolis-data" / "modifications" / "modules.json"
SKIP_REASON = "run python setup.py first (it clones coriolis-data)"

# Test modules that read the cloned coriolis-data folder
NEEDS_CORIOLIS_DATA = {
    "test_simulate_module_swap.py",
    "test_ship_loadout_queries.py",
    "test_ingest_ship_modules.py",
    "test_query_snapshot.py",
    "test_setup_ship_modules.py",
    "test_blueprint_lookup.py",
    "test_cross_check.py",
}


def pytest_collection_modifyitems(config, items):
    if DATA_MARKER.exists():
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if Path(str(item.fspath)).name in NEEDS_CORIOLIS_DATA:
            item.add_marker(skip)


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
