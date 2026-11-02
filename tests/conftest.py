"""Shared pytest setup.

The ship-loadout tests read Coriolis's real data (ships, modules, blueprints)
from the git-ignored `coriolis-data` folder that `python setup.py` clones. On a
fresh clone that folder isn't there, and those modules would fail hundreds of
tests with FileNotFoundError, which tells a newcomer nothing. Skip them with the
reason instead; every test that needs no external data still runs."""
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
}


def pytest_collection_modifyitems(config, items):
    if DATA_MARKER.exists():
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if Path(str(item.fspath)).name in NEEDS_CORIOLIS_DATA:
            item.add_marker(skip)
