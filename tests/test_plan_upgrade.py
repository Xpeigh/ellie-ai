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


import ingest_excel as ie


def _hold(path, key, shown, count):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES (?,?,?,?,?)", (key, "Encoded", shown, count, "2026-11-08T00:00:00Z"))
    conn.commit()
    conn.close()


FSD_STEP_4 = {"Chemical Distillery": 1, "Eccentric Hyperspace Trajectories": 1, "Manganese": 1}
FSD_STEP_5 = {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}


def test_the_fsd_plan_from_grade_3_to_5_with_a_rank_5_engineer(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["found"] is True and plan["upgrade"] == "Increased range"
    assert (plan["from_grade"], plan["target_grade"], plan["start_source"], plan["rolls_basis"]) == (3, 5, "stated", "rank 5")
    assert [(s["grade"], s["rolls"], s["per_roll"], s["cost_known"]) for s in plan["steps"]] == [
        (4, 4, FSD_STEP_4, True), (5, 5, FSD_STEP_5, True)]
    assert all(s["rolls_up_to"] >= s["rolls"] for s in plan["steps"])
    assert plan["totals"] == {"Chemical Distillery": 4, "Eccentric Hyperspace Trajectories": 4, "Manganese": 4,
                              "Arsenic": 5, "Chemical Manipulators": 5, "Datamined Wake Exceptions": 5}
    assert plan["chosen_engineer"] == "Felicity Farseer" and plan["thin_evidence"] is False
    assert {e["name"]: e["usable"] for e in plan["engineers"]} == {"Elvira Martuuk": False, "Felicity Farseer": True, "Mel Brandon": False}
    assert plan["summary"] == (
        "Frame Shift Drive, Increased range, grade 3 to 5 (from grade 3). Felicity Farseer (unlocked, rank 5) can do it. "
        "Rolls at rank 5: grade 4: 4, grade 5: 5. "
        "You need 4 Chemical Distillery, 4 Eccentric Hyperspace Trajectories, 4 Manganese, 5 Arsenic, 5 Chemical Manipulators, 5 Datamined Wake Exceptions. "
        "You are short by: 4 Chemical Distillery (have 0), 4 Eccentric Hyperspace Trajectories (have 0), 4 Manganese (have 0), "
        "5 Arsenic (have 0), 5 Chemical Manipulators (have 0), 5 Datamined Wake Exceptions (have 0). "
        "Extra rolls for a better result will cost more.")
    json.dumps(plan)                                              # plain data only


def test_the_fitted_modules_grade_is_the_start_when_no_grade_is_given(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _fit(coriolis_db, "FrameShiftDrive", FSD, "FSD_LongRange", 4)
    plan = qf.plan_upgrade("FSD", "Increased FSD Range", 5)
    assert (plan["from_grade"], plan["start_source"]) == (4, "fitted module") and [s["grade"] for s in plan["steps"]] == [5]
    assert "your fitted module is at grade 4" in plan["summary"]


def test_below_rank_5_the_plan_uses_four_rolls_a_grade_and_stops_at_the_engineers_rank(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 3)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5)
    assert plan["rolls_basis"] == "below rank 5" and plan["chosen_engineer"] == "Felicity Farseer"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(1, 4), (2, 4), (3, 4)]
    assert plan["thin_evidence"] is True
    assert "is the best you have, but grade 5 needs rank 5, so this plan stops at grade 3." in plan["summary"]
    assert any("Grade 1 rolls below rank 5 vary" in n for n in plan["notes"])


def test_with_no_engineer_unlocked_the_plan_assumes_rank_5_and_says_so(coriolis_db):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=2)
    assert plan["chosen_engineer"] is None and plan["rolls_basis"] == "rank 5"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(3, 3), (4, 4), (5, 5)]
    assert ("None of the engineers who offer it is unlocked yet: Elvira Martuuk (not met), Felicity Farseer (not met), "
            "Mel Brandon (not met). The rolls below assume a rank-5 engineer.") in plan["summary"]


def test_an_engineer_who_is_only_invited_or_known_is_not_unlocked(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Invited")
    _engineer(coriolis_db, "Mel Brandon", "Known")
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=4)
    assert plan["chosen_engineer"] is None and "Felicity Farseer (invited)" in plan["summary"] and "Mel Brandon (known)" in plan["summary"]


def test_an_unlocked_engineer_with_no_rank_yet_assumes_rank_5_and_says_so(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked")          # the journal has not given a rank (rank_value is NULL)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["found"] is True and plan["chosen_engineer"] == "Felicity Farseer" and plan["rolls_basis"] == "rank 5"
    assert [(s["grade"], s["rolls"]) for s in plan["steps"]] == [(4, 4), (5, 5)]
    assert "The journal has not shown Felicity Farseer's rank yet, so the rolls below assume a rank-5 engineer." in plan["summary"]


def test_the_experimental_effect_is_added_once(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=4, experimental_effect="Mass Manager")
    assert plan["found"] is True and plan["experimental"]["effect_name"] == "Mass Manager"
    assert plan["totals"]["Atypical Disrupted Wake Echoes"] == 5 and plan["totals"]["Galvanising Alloys"] == 3
    assert plan["totals"]["Arsenic"] == 5 and "Includes Mass Manager once" in plan["summary"]


def test_an_effect_the_module_cannot_take_refuses_the_whole_plan(coriolis_db):
    plan = qf.plan_upgrade("Thrusters", "Dirty", 3, experimental_effect="Mass Manager")
    assert plan["found"] is False and "can't be applied to Thrusters" in plan["reason"]


def test_what_is_held_reduces_what_is_short(coriolis_db):
    _engineer(coriolis_db, "Felicity Farseer", "Unlocked", 5)
    _hold(coriolis_db, "manganese", "Manganese", 10)
    _hold(coriolis_db, "arsenic", "Arsenic", 2)
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=3)
    assert plan["short"]["Manganese"] == {"need": 4, "need_up_to": 4, "have": 10, "short_by": 0}
    assert plan["short"]["Arsenic"] == {"need": 5, "need_up_to": 5, "have": 2, "short_by": 3}
    assert "3 Arsenic (have 2)" in plan["summary"] and "Manganese (have" not in plan["summary"]


def test_a_module_already_at_the_target_has_nothing_to_collect(coriolis_db):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 4, from_grade=4)
    assert plan["found"] is True and plan["steps"] == [] and plan["totals"] == {}
    assert plan["summary"].startswith("Frame Shift Drive, Increased range: your module is already at grade 4")


@pytest.mark.parametrize("target", ["five", None, 0, 6, "", []])
def test_a_target_grade_that_is_not_1_to_5_gets_a_plain_reply(coriolis_db, target):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", target)
    assert plan["found"] is False and plan["reason"] == "The target grade must be a number from 1 to 5."


@pytest.mark.parametrize("start", ["x", 7, -1, []])
def test_a_starting_grade_that_is_not_0_to_4_gets_a_plain_reply(coriolis_db, start):
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 5, from_grade=start)
    assert plan["found"] is False and plan["reason"] == "The starting grade must be a number from 0 to 4."


def test_an_unknown_module_or_blueprint_is_a_miss_that_lists_the_real_options(coriolis_db):
    plan = qf.plan_upgrade("xyzzy", "Dirty", 3)
    assert plan["found"] is False and plan["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
    plan = qf.plan_upgrade("Frame Shift Drive", "Turbo", 3)
    assert plan["found"] is False and plan["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    for blank in (("", "", 3), (None, None, 3), ("Thrusters", "", 3)):
        assert qf.plan_upgrade(*blank)["found"] is False


def test_a_grade_the_blueprint_does_not_have_is_named(coriolis_db):
    plan = qf.plan_upgrade("Shield Cell Bank", "Rapid charge", 5)
    assert plan["found"] is False and "offered at grades 1-4, not grade 5" in plan["reason"]


def test_a_blueprint_with_no_engineer_and_no_recorded_cost_says_so_and_invents_nothing(coriolis_db):
    plan = qf.plan_upgrade("Cargo Rack", "Expanded Capacity", 5)
    assert plan["found"] is True and plan["engineers"] == [] and plan["chosen_engineer"] is None
    assert [(s["grade"], s["cost_known"]) for s in plan["steps"]] == [(5, False)] and plan["totals"] == {}
    assert "No engineer in Coriolis's data offers it at grade 5." in plan["summary"]
    assert any("no recorded materials" in n for n in plan["notes"])


def test_the_spreadsheet_cost_of_a_grade_wins_as_in_part_one(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    ie.build_schema(conn)
    conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)",
                 ("core_internal", "Frame Shift Drive", "Increased range", 4, json.dumps([{"qty": 2, "item": "Arsenic"}]), None))
    conn.commit()
    conn.close()
    plan = qf.plan_upgrade("Frame Shift Drive", "Increased range", 4, from_grade=3)
    assert plan["steps"][0]["per_roll"] == {"Arsenic": 2}


def test_every_coriolis_blueprint_can_be_planned_to_every_grade(coriolis_db):
    """The reachability pattern of part one, through plan_upgrade: with nobody unlocked the rolls are the
    rank-5 rule (N for grade N), the totals are exactly the steps added up, and every blueprint that has an
    engineer lists one."""
    conn = _conn(coriolis_db)
    failures, checked = [], 0
    try:
        for grp in [r[0] for r in conn.execute("SELECT DISTINCT grp FROM module_blueprints ORDER BY grp")]:
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (grp,)).fetchone()
                      or {"name": {"bh": "armour"}.get(grp, grp)})["name"]
            for (fdname,) in conn.execute("SELECT fdname FROM module_blueprints WHERE grp = ?", (grp,)).fetchall():
                upgrade = conn.execute("SELECT upgrade_name FROM coriolis_blueprints WHERE fdname = ? LIMIT 1", (fdname,)).fetchone()[0]
                for grade in [r[0] for r in conn.execute("SELECT grade FROM coriolis_blueprints WHERE fdname = ?", (fdname,))]:
                    checked += 1
                    plan = qf.plan_upgrade(module, upgrade, grade, from_grade=0)
                    expected = {}
                    for step in plan.get("steps", []):
                        for name, qty in step["per_roll"].items():
                            expected[name] = expected.get(name, 0) + qty * step["rolls"]
                    if (not plan["found"] or plan["totals"] != expected or any(s["rolls"] != s["grade"] for s in plan["steps"])
                            or (not plan["engineers"] and (fdname, grade) != ("CargoRack_IncreasedCapacity", 5))):
                        failures.append((grp, fdname, grade, plan.get("reason")))
    finally:
        conn.close()
    assert checked > 800
    assert failures == []
