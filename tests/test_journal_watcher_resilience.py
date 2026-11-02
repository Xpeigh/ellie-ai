"""Task 10a X4: the journal watcher survives a bad event and a locked database
instead of ending its loop (and, under app.py, silently leaving the tools
serving stale fuel and loadout)."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import journal_watcher as jw

RESERVOIR = {"event": "ReservoirReplenished", "FuelMain": 6.5, "FuelReservoir": 0.83}
# a real-looking jump whose FuelUsed arrived as a string: record_fsd_jump adds it to a float
MALFORMED_JUMP = {
    "event": "FSDJump", "StarSystem": "Sol", "StarPos": [0.0, 0.0, 0.0],
    "JumpDist": 20.0, "FuelUsed": "oops", "FuelLevel": 4.0,
}
CARGO = {"event": "Cargo", "Vessel": "Ship", "Count": 12}


@pytest.fixture(autouse=True)
def fresh_stats():
    jw.WATCHER_STATS.update(errors=0, last_error=None)


def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "w.db"))
    jw.build_schema(conn)
    return conn


def _state(conn):
    cur = conn.execute("SELECT * FROM ship_state WHERE id = 1")
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def _journal(path, *lines):
    path.write_bytes(b"".join((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n" for line in lines))


def test_watcher_stats_start_empty():
    assert jw.WATCHER_STATS == {"errors": 0, "last_error": None}


def test_replay_skips_a_malformed_event_and_keeps_going(tmp_path, capsys):
    conn = _conn(tmp_path)
    journal = tmp_path / "Journal.2026-10-23T000000.01.log"
    _journal(journal, RESERVOIR, MALFORMED_JUMP, CARGO)
    jw.replay_journal(conn, journal)
    state = _state(conn)
    assert state["fuel_main"] == 6.5          # the event before it
    assert state["cargo_count"] == 12         # the event after it
    assert jw.WATCHER_STATS["errors"] == 1
    err = capsys.readouterr().err
    assert err.count("journal_watcher: skipped a FSDJump event: TypeError: ") == 1
    assert jw.WATCHER_STATS["last_error"].startswith("journal_watcher: skipped a FSDJump event: TypeError: ")


def test_replay_survives_a_database_lock_on_one_event(tmp_path, monkeypatch, capsys):
    """process_event raising OperationalError('database is locked') for one
    event; its half-done write is rolled back and the next events still land."""
    conn = _conn(tmp_path)
    real = jw.process_event

    def flaky(c, event):
        if event.get("event") == "Cargo":
            c.execute("UPDATE ship_state SET last_jump_dist = 99.0 WHERE id = 1")   # an uncommitted write...
            raise sqlite3.OperationalError("database is locked")              # ...then the lock error
        return real(c, event)

    monkeypatch.setattr(jw, "process_event", flaky)
    journal = tmp_path / "Journal.2026-10-23T000000.01.log"
    _journal(journal, RESERVOIR, CARGO, {"event": "FuelScoop", "Scooped": 1.0, "Total": 7.5})
    jw.replay_journal(conn, journal)
    state = _state(conn)
    assert state["fuel_main"] == 7.5          # the later event applied
    assert state["cargo_count"] is None       # the locked event was skipped...
    # ...and its half-done write was rolled back: the NEXT event's commit would
    # otherwise have made the stray 99.0 permanent
    assert state["last_jump_dist"] is None
    assert jw.WATCHER_STATS["errors"] == 1
    assert "journal_watcher: skipped a Cargo event: OperationalError: database is locked" in capsys.readouterr().err


def test_a_line_that_is_valid_json_but_not_an_event_is_counted_not_fatal(tmp_path):
    conn = _conn(tmp_path)
    journal = tmp_path / "Journal.2026-10-23T000000.01.log"
    _journal(journal, [1, 2], RESERVOIR)
    jw.replay_journal(conn, journal)
    assert _state(conn)["fuel_main"] == 6.5
    assert jw.WATCHER_STATS["errors"] == 1
    assert "skipped a list event: AttributeError" in jw.WATCHER_STATS["last_error"]


def test_replay_reads_a_journal_with_undecodable_bytes(tmp_path):
    """A stray non-UTF-8 byte (a journal mid-write, a synced-folder glitch)
    must not end the replay: the line is garbled and skipped, the rest is read."""
    conn = _conn(tmp_path)
    journal = tmp_path / "Journal.2026-10-23T000000.01.log"
    _journal(journal, RESERVOIR, b'{"event":"Cargo","Vessel":"Sh\xff\xfe\xfdp","Count":5}', CARGO)
    jw.replay_journal(conn, journal)
    state = _state(conn)
    assert state["fuel_main"] == 6.5 and state["cargo_count"] == 12


def test_follow_survives_a_bad_event_and_undecodable_bytes(tmp_path, monkeypatch, capsys):
    """follow_journal tails the file: bad lines appended while it waits are
    skipped, good ones applied, and it still notices the next session's file."""
    conn = _conn(tmp_path)
    journal = tmp_path / "Journal.2026-10-23T000000.01.log"
    _journal(journal, {"event": "Fileheader"})
    newer = tmp_path / "Journal.2026-10-23T010000.01.log"
    pauses = []

    def fake_sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 1:                     # while waiting: more lines arrive
            with open(journal, "ab") as f:
                f.write(json.dumps(MALFORMED_JUMP).encode() + b"\n")
                f.write(b'\xff\xfe not json\n')
                f.write(json.dumps(CARGO).encode() + b"\n")
        else:                                    # then the game starts a new session
            _journal(newer, {"event": "Fileheader"})
            os.utime(newer, (journal.stat().st_mtime + 100, journal.stat().st_mtime + 100))

    monkeypatch.setattr(jw.time, "sleep", fake_sleep)
    assert jw.follow_journal(conn, journal) == newer
    assert _state(conn)["cargo_count"] == 12
    assert jw.WATCHER_STATS["errors"] == 1
    assert "journal_watcher: skipped a FSDJump event: TypeError" in capsys.readouterr().err


def test_main_opens_the_database_with_a_long_busy_timeout(tmp_path, monkeypatch):
    seen = {}
    real_connect = sqlite3.connect

    class Stop(Exception):
        pass

    def spy(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return real_connect(str(tmp_path / "main.db"))

    def no_journals():
        raise Stop

    monkeypatch.setattr(jw.sqlite3, "connect", spy)
    monkeypatch.setattr(jw, "find_latest_journal", no_journals)
    with pytest.raises(Stop):
        jw.main()
    assert seen["kwargs"].get("timeout") == 30
