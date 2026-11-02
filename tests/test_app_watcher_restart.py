"""Task 10a X4: app.py keeps the journal watcher alive -- if it raises or
returns, it is restarted after a pause, for as long as the app runs."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app


class Stop(Exception):
    """Raised by the fake sleep to break the otherwise endless loop."""


def _stopping_after(sleeps, count):
    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= count:
            raise Stop
    return sleep


def test_the_restart_pause_is_five_seconds():
    assert app.WATCHER_RESTART_SECONDS == 5


def test_a_target_that_raises_twice_then_returns_is_restarted_each_time(capsys):
    calls, sleeps = [], []

    def target():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError(f"boom {len(calls)}")
        return None                      # the third run just returns

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 3))
    assert len(calls) == 3                       # restarted after the raise, the raise, and the return
    assert sleeps == [app.WATCHER_RESTART_SECONDS] * 3
    err = capsys.readouterr().err
    assert "RuntimeError: boom 1" in err and "RuntimeError: boom 2" in err
    assert "returned unexpectedly" in err


def test_the_pause_can_be_shortened_by_the_caller(capsys):
    sleeps = []
    with pytest.raises(Stop):
        app.watch_forever(lambda: None, sleep=_stopping_after(sleeps, 2), delay=0.01)
    assert sleeps == [0.01, 0.01]


def test_a_missing_journal_folder_is_one_line_not_a_traceback_every_retry(capsys):
    sleeps = []

    def target():
        raise FileNotFoundError("No journal files found in X")

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 1))
    err = capsys.readouterr().err
    assert "No journal files found in X" in err
    assert "Traceback" not in err


def test_the_apps_watcher_thread_runs_the_watcher_under_the_restart_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "watch_forever", lambda target, *a, **k: seen.append(target))
    app._run_watcher()
    assert seen == [app.journal_watcher.main]
