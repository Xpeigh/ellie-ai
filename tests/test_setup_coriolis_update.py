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
    monkeypatch.setattr(setup_script, "coriolis_refreshed", False, raising=False)
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
