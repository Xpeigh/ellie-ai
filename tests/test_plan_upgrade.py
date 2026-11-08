import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm
import query_functions as qf

# The 25 ship-module engineers, spelled the way the game's journal spells them.
JOURNAL_SHIP_ENGINEERS = [
    "Bill Turner", "Broo Tarquin", "Chloe Sedesi", "Colonel Bris Dekker", "Didi Vatermann", "Elvira Martuuk",
    "Etienne Dorn", "Felicity Farseer", "Hera Tani", "Juri Ishmaak", "Lei Cheung", "Liz Ryder", "Lori Jameson",
    "Marco Qwent", "Marsha Hicks", "Mel Brandon", "Petra Olmanova", "Professor Palin", "Ram Tah", "Selene Jean",
    "The Dweller", "The Sarge", "Tiana Fortune", "Tod 'The Blaster' McQuinn", "Zacariah Nemo",
]


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _engineer(path, name, stage, rank=None):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO engineer_progress VALUES (?,?,?,?)", (name, stage, rank, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


def _offers(path, fdname, grade):
    conn = _conn(path)
    try:
        return qf._engineers_offering(conn, fdname, grade)
    finally:
        conn.close()


def test_every_coriolis_engineer_name_resolves_to_a_name_the_journal_uses(coriolis_db):
    conn = _conn(coriolis_db)
    try:
        coriolis = {bm.engineer_key(r[0]) for r in conn.execute("SELECT DISTINCT engineer FROM blueprint_engineers")}
    finally:
        conn.close()
    assert coriolis == {bm.engineer_key(n) for n in JOURNAL_SHIP_ENGINEERS}      # a new misspelling fails here


def test_felicity_is_listed_for_dirty_drives_despite_coriolis_misspelling_her(coriolis_db):
    names = [o["name"] for o in _offers(coriolis_db, "Engine_Dirty", 3)]
    assert "Felicity Farseer" in names and "Felicty Farseer" not in names
    assert names == sorted(names) and len(names) == len(set(names)) == 4


def test_status_and_rank_come_from_the_journal_table(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _engineer(coriolis_db, "Elvira Martuuk", "Invited")
    _engineer(coriolis_db, "Tod 'The Blaster' McQuinn", "Unlocked", 5)           # not an FSD engineer: must not appear
    offers = {o["name"]: o for o in _offers(coriolis_db, "FSD_LongRange", 5)}
    assert offers["Felicity Farseer"] == {"name": "Felicity Farseer", "status": "unlocked", "rank": 5, "usable": True}
    assert offers["Elvira Martuuk"] == {"name": "Elvira Martuuk", "status": "invited", "rank": None, "usable": False}
    assert offers["Mel Brandon"] == {"name": "Mel Brandon", "status": "not met", "rank": None, "usable": False}
    assert "Tod 'The Blaster' McQuinn" not in offers


def test_an_unlocked_engineer_below_the_target_rank_is_not_usable(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 3)
    assert {o["name"]: o["usable"] for o in _offers(coriolis_db, "FSD_LongRange", 3)}["Felicity Farseer"] is True
    assert {o["name"]: o["usable"] for o in _offers(coriolis_db, "FSD_LongRange", 4)}["Felicity Farseer"] is False


def test_an_empty_engineer_table_gives_everyone_as_not_met(coriolis_db):
    offers = _offers(coriolis_db, "FSD_LongRange", 5)
    assert [o["status"] for o in offers] == ["not met"] * 3 and not any(o["usable"] for o in offers)


def test_the_chosen_engineer():
    def offer(name, status, rank, usable):
        return {"name": name, "status": status, "rank": rank, "usable": usable}

    best, mode = qf._choose_engineer([offer("B", "unlocked", 5, True), offer("A", "unlocked", 5, True), offer("C", "unlocked", 3, False)], 5)
    assert (best["name"], mode) == ("A", "usable")                                  # equal rank: first by name
    best, mode = qf._choose_engineer([offer("C", "unlocked", 3, False), offer("D", "unlocked", 2, False), offer("E", "known", None, False)], 5)
    assert (best["name"], mode) == ("C", "low_rank")                                # the best unlocked one, below the target
    best, mode = qf._choose_engineer([offer("F", "unlocked", None, False), offer("C", "unlocked", 3, False)], 5)
    assert (best["name"], mode) == ("C", "low_rank")                                # a known rank beats an unknown one
    best, mode = qf._choose_engineer([offer("F", "unlocked", None, False), offer("E", "known", None, False)], 5)
    assert (best["name"], mode) == ("F", "rank_unknown")                            # unlocked, but the journal gave no rank
    assert qf._choose_engineer([offer("E", "known", None, False)], 5) == (None, "none_unlocked")
    assert qf._choose_engineer([], 5) == (None, "no_engineer")


def _fit(path, slot, item, blueprint=None, level=None):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO current_loadout_modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (slot, item, 1, 1, 1.0, 0, blueprint, level, None, None, None, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


FSD = "int_hyperdrive_overcharge_size5_class5"
LASER = "hpt_pulselaser_gimbal_medium"


def _start(path, module, fdname, stated=None):
    conn = _conn(path)
    try:
        return qf._start_grade(conn, qf._resolve_module_groups(conn, module), fdname, stated)
    finally:
        conn.close()


def test_a_stated_grade_wins_over_the_fitted_one(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 3)
    assert _start(coriolis_db, "FSD", "FSD_LongRange", 1) == (1, "stated", None)
    assert _start(coriolis_db, "FSD", "FSD_LongRange", 0) == (0, "stated", None)


def test_the_fitted_modules_with_this_modification_gives_its_level(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 3)
    assert _start(coriolis_db, "FSD", "FSD_LongRange") == (3, "fitted module", None)
    assert _start(coriolis_db, "FSD", "fsd_longrange") == (3, "fitted module", None)       # the journal's casing is not trusted


def test_a_fitted_module_with_another_modification_starts_from_scratch_and_says_so(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_FastBoot", 2)
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "from scratch") and "replaces it" in note


def test_a_fitted_unengineered_module_starts_at_grade_0_and_says_so(coriolis_db):
    _fit(coriolis_db, "FrameShiftDrive", FSD)
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "fitted module") and "unengineered" in note


def test_several_fitted_modules_start_from_scratch_and_name_the_slots(coriolis_db):
    _fit(coriolis_db, "MediumHardpoint1", LASER, "Weapon_Efficient", 2)
    _fit(coriolis_db, "MediumHardpoint2", LASER)
    grade, source, note = _start(coriolis_db, "pulse laser", "Weapon_Efficient")
    assert (grade, source) == (0, "from scratch")
    assert "MediumHardpoint1, MediumHardpoint2" in note and "from grade N" in note


def test_nothing_fitted_or_no_loadout_starts_from_scratch(coriolis_db):
    grade, source, note = _start(coriolis_db, "FSD", "FSD_LongRange")
    assert (grade, source) == (0, "from scratch") and "no loadout has been captured" in note


def test_an_install_without_the_loadout_tables_still_starts_from_scratch(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    conn.execute("DROP TABLE current_loadout_modules")
    conn.commit()
    conn.close()
    assert _start(coriolis_db, "FSD", "FSD_LongRange")[:2] == (0, "from scratch")
