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


# --- Task 10b B9a: back off while the journal folder is missing ----------------------------------

def _lines(capsys):
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("[journal_watcher]")]


def test_a_missing_folder_backs_off_5_10_20_40_then_60_and_stays_there(capsys):
    sleeps = []

    def target():
        raise FileNotFoundError("No journal files found in X")

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 8))
    assert sleeps == [5, 10, 20, 40, 60, 60, 60, 60]
    lines = _lines(capsys)
    # one line per CHANGE of delay (5, 10, 20, 40, 60), not one per retry
    assert len(lines) == 5
    assert [("retrying in %s s" % d) in line for d, line in zip((5, 10, 20, 40, 60), lines)] == [True] * 5


def test_the_delay_resets_once_the_folder_exists_and_the_return_is_announced(capsys):
    outcomes = iter([FileNotFoundError("none yet")] * 3 + [None] + [FileNotFoundError("gone again")] * 2)
    sleeps = []

    def target():
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 6))
    # 5, 10, 20 while missing; the folder appears (the watcher ran): back to the normal 5; a new streak starts at 5, 10
    assert sleeps == [5, 10, 20, 5, 5, 10]
    lines = _lines(capsys)
    assert sum("journal folder found" in line for line in lines) == 1
    assert [line for line in lines if "retrying in" in line].__len__() == 5       # 5,10,20 then 5,10 again


def test_any_other_failure_also_means_the_folder_exists(capsys):
    outcomes = iter([FileNotFoundError("x"), FileNotFoundError("x"), RuntimeError("boom"), FileNotFoundError("x")])
    sleeps = []

    def target():
        raise next(outcomes)

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 4))
    assert sleeps == [5, 10, 5, 5]


def test_a_long_healthy_run_before_a_miss_starts_the_back_off_over(capsys):
    """The watcher ran for ten minutes, then the folder vanished: that is a new
    problem, not the tenth minute of an old one."""
    now = [0.0]
    attempts = iter([("miss", 0.0), ("miss", 0.0), ("miss", 600.0)])
    sleeps = []

    def clock():
        return now[0]

    def target():
        kind, ran_for = next(attempts)
        now[0] += ran_for
        raise FileNotFoundError("x")

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 3), clock=clock)
    assert sleeps == [5, 10, 5]


def test_the_back_off_is_built_on_the_callers_delay_and_capped_at_a_minute():
    sleeps = []

    def target():
        raise FileNotFoundError("x")

    with pytest.raises(Stop):
        app.watch_forever(target, sleep=_stopping_after(sleeps, 4), delay=0.5)
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert app.WATCHER_MAX_BACKOFF_SECONDS == 60
