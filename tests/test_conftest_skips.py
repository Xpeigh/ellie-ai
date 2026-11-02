"""Task 10a X9: on a fresh clone (no coriolis-data) the data-dependent test
modules skip with a clear reason, and everything else still runs."""
import subprocess
import sys
from pathlib import Path

import conftest

ROOT = Path(__file__).resolve().parent.parent


class _Item:
    def __init__(self, filename):
        self.fspath = Path("tests") / filename
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def _collect(monkeypatch, tmp_path, data_present):
    marker = tmp_path / "modules.json"
    if data_present:
        marker.write_text("{}")
    monkeypatch.setattr(conftest, "DATA_MARKER", marker)
    items = [_Item(name) for name in sorted(conftest.NEEDS_CORIOLIS_DATA)] + [
        _Item("test_loadout_calc.py"), _Item("test_journal_watcher_ship_state.py"), _Item("test_ellie_chat_ship_tools.py"),
    ]
    conftest.pytest_collection_modifyitems(None, items)
    return items


def test_without_the_data_the_data_dependent_modules_skip_with_the_reason(monkeypatch, tmp_path):
    items = _collect(monkeypatch, tmp_path, data_present=False)
    skipped = {i.fspath.name for i in items if i.markers}
    assert skipped == conftest.NEEDS_CORIOLIS_DATA
    for item in items:
        for marker in item.markers:
            assert marker.name == "skip"
            assert marker.kwargs["reason"] == "run python setup.py first (it clones coriolis-data)"


def test_with_the_data_nothing_is_skipped(monkeypatch, tmp_path):
    assert not any(i.markers for i in _collect(monkeypatch, tmp_path, data_present=True))


def test_the_list_names_every_test_module_that_reads_coriolis_data():
    """A module that reads the clone but isn't listed would error on a fresh
    clone instead of skipping."""
    tests_dir = Path(__file__).resolve().parent
    reading = {
        p.name for p in tests_dir.glob("test_*.py")
        if "coriolis-data" in p.read_text(encoding="utf-8") or "ism.ingest_all" in p.read_text(encoding="utf-8")
    } - {Path(__file__).name}
    assert reading <= conftest.NEEDS_CORIOLIS_DATA, reading - conftest.NEEDS_CORIOLIS_DATA


def test_a_real_run_without_the_clone_skips_instead_of_failing(tmp_path):
    """End to end: a copy of the project WITHOUT coriolis-data, run with pytest."""
    project = tmp_path / "project"
    (project / "tests" / "fixtures").mkdir(parents=True)
    for py in ROOT.glob("*.py"):
        (project / py.name).write_bytes(py.read_bytes())
    for f in (ROOT / "tests").glob("*.py"):
        (project / "tests" / f.name).write_bytes(f.read_bytes())
    for f in (ROOT / "tests" / "fixtures").glob("*"):
        (project / "tests" / "fixtures" / f.name).write_bytes(f.read_bytes())
    assert not (project / "coriolis-data").exists()
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ingest_ship_modules.py", "tests/test_loadout_calc.py",
         "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=project, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "run python setup.py first (it clones coriolis-data)" in done.stdout
    assert " skipped" in done.stdout and " passed" in done.stdout and "failed" not in done.stdout
