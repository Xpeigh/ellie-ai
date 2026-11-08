import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.parametrize("typed, expected", [
    ("Frame Shift Drive", ["fsd"]), ("FSD", ["fsd"]), ("frame shift drive", ["fsd"]),
    ("FRAME-SHIFT DRIVE", ["fsd"]), ("  Frame   Shift Drive ", ["fsd"]),
    ("Thrusters", ["t"]), ("thruster", ["t"]), ("engines", ["t"]),
    ("Power Plant", ["pp"]), ("Power Distributor", ["pd"]), ("distributor", ["pd"]),
    ("Life Support", ["ls"]), ("Sensors", ["s"]), ("sensor", ["s"]),
    ("Shield Generator", ["sg"]), ("Armour", ["bh"]), ("bulkheads", ["bh"]),
    ("Beam Laser", ["bl"]), ("Multi-cannon", ["mc"]), ("Hull Reinforcement Package", ["hr"]), ("HRP", ["hr"]),
    ("Detailed Surface Scanner", ["ss"]), ("Manifest Scanner", ["cs"]),
    ("Collector Limpet Controller", ["cc"]), ("Cargo Rack", ["cr"]), ("Fuel Scoop", ["fs"]),
    ("Module Reinforcement Package", ["mrp"]),
    ("fsd", ["fsd"]),                                  # a raw group code
    ("bh", ["bh"]),                                    # ... also for a group with no module file (ship armour)
    ("beam lasers", ["bl"]), ("Shield Boosters", ["sb"]), ("multi-cannons", ["mc"]),   # a typed plural
    ("frame shift drives", ["fsd"]), ("Power Plants", ["pp"]),
    ("xyzzy", []), ("", []), ("   ", []), (None, []),
])
def test_a_typed_module_name_resolves_to_its_group(coriolis_db, typed, expected):
    conn = _conn(coriolis_db)
    try:
        assert qf._resolve_module_groups(conn, typed) == expected
    finally:
        conn.close()


FSD_ROWS = [
    {"fdname": "FSD_FastBoot", "upgrade_name": "Faster boot sequence"},
    {"fdname": "FSD_LongRange", "upgrade_name": "Increased range"},
    {"fdname": "FSD_Shielded", "upgrade_name": "Shielded"},
]
ENGINE_ROWS = [
    {"fdname": "Engine_Dirty", "upgrade_name": "Dirty"},
    {"fdname": "Engine_Reinforced", "upgrade_name": "Reinforced"},
    {"fdname": "Engine_Tuned", "upgrade_name": "Clean"},
]


@pytest.mark.parametrize("typed, fdname", [
    ("Increased range", "FSD_LongRange"),               # rule 1, unchanged
    ("FSD_LongRange", "FSD_LongRange"),                  # rule 2, unchanged
    ("long_range", "FSD_LongRange"),                     # rule 3, unchanged
    ("increased-range", "FSD_LongRange"),                # rule 4, unchanged
    ("Increased FSD Range", "FSD_LongRange"),            # the word rule: every word of "Increased range" is there
    ("long range", "FSD_LongRange"),                     # the word rule on the fdname's own words
    ("Faster FSD Boot Sequence", "FSD_FastBoot"),
    ("Shielded FSD", "FSD_Shielded"),
])
def test_the_word_rule_finds_in_game_names(typed, fdname):
    assert [r["fdname"] for r in qf._blueprint_name_matches(FSD_ROWS, typed)] == [fdname]


def test_the_word_rule_reads_clean_and_dirty_drive_tuning():
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Dirty Drive Tuning")] == ["Engine_Dirty"]
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Clean Drive Tuning")] == ["Engine_Tuned"]


def test_an_alias_covers_a_name_that_shares_no_words():
    assert [r["fdname"] for r in qf._blueprint_name_matches(ENGINE_ROWS, "Drive Strengthening")] == ["Engine_Reinforced"]


def test_the_match_with_the_most_words_wins():
    rows = [{"fdname": "A_Reinforced", "upgrade_name": "Reinforced"},
            {"fdname": "A_HeavyDuty", "upgrade_name": "Heavy duty"}]
    assert [r["fdname"] for r in qf._blueprint_name_matches(rows, "heavy duty reinforced thing")] == ["A_HeavyDuty"]


@pytest.mark.parametrize("typed", ["Turbo", "", "   ", "the", "grade"])
def test_a_name_with_none_of_the_words_matches_nothing(typed):
    assert qf._blueprint_name_matches(FSD_ROWS, typed) == []


def test_an_earlier_rule_always_beats_the_word_rule():
    rows = [{"fdname": "X_Shielded", "upgrade_name": "Shielded"},
            {"fdname": "X_ShieldedPlus", "upgrade_name": "Shielded plus"}]
    assert [r["fdname"] for r in qf._blueprint_name_matches(rows, "Shielded plus")] == ["X_ShieldedPlus"]


import json

import ingest_excel as ie


def _add_sheet(path, blueprint_rows=(), cost_rows=()):
    """Create the spreadsheet tables in a test database and fill them."""
    conn = sqlite3.connect(path)
    ie.build_schema(conn)
    for row in blueprint_rows:
        conn.execute("INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)", row)
    for row in cost_rows:
        conn.execute("INSERT INTO ungraded_costs VALUES (?,?,?,?,?)", row)
    conn.commit()
    conn.close()


def test_a_database_with_no_spreadsheet_tables_still_answers_from_coriolis(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 5)
    assert r["found"] is True and r["source"] == "coriolis"
    assert r["materials"] == {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}
    assert r["materials_per"] == "roll" and r["cost_known"] is True and r["grade"] == 5
    assert r["summary"] == ("Frame Shift Drive, Increased range, grade 5, per roll: "
                            "1 Arsenic, 1 Chemical Manipulators, 1 Datamined Wake Exceptions.")


def test_the_player_s_words_work_for_the_module_and_the_blueprint(coriolis_db):
    a = qf.get_blueprint("FSD", "Increased FSD Range", 5)
    b = qf.get_blueprint("frame shift drive", "FSD_LongRange", "5")
    assert a["materials"] == b["materials"] == {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}
    t = qf.get_blueprint("Thrusters", "Dirty Drive Tuning", 3)
    assert t["materials"] == {"Chromium": 1, "Mechanical Components": 1, "Specialised Legacy Firmware": 1}
    assert qf.get_blueprint("Thrusters", "Drive Strengthening", 1)["upgrade"] == "Reinforced"


def test_an_unrecorded_cost_is_not_reported_as_free(coriolis_db):
    r = qf.get_blueprint("Cargo Rack", "Expanded Capacity", 5)
    assert r["found"] is True and r["materials"] == {} and r["cost_known"] is False
    assert r["summary"] == "Cargo Rack, Expanded Capacity, grade 5: no materials are recorded for this upgrade, so its cost is not known."


def test_a_grade_the_blueprint_does_not_have_names_the_grades_it_does(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 6)
    assert r["found"] is False and r["available_grades"] == [1, 2, 3, 4, 5]
    assert r["reason"] == "Increased range for Frame Shift Drive is offered at grades 1-5, not grade 6."
    assert "offered at grades 1-4, not grade 5" in qf.get_blueprint("Shield Cell Bank", "Rapid charge", 5)["reason"]
    assert qf.get_blueprint("Frame Shift Drive", "Increased range", 0)["reason"].endswith("not grade 0.")


@pytest.mark.parametrize("grade", ["five", None, "", [], {}])
def test_a_grade_that_is_not_a_number_gets_a_plain_reply(coriolis_db, grade):
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", grade)
    assert r["found"] is False and r["reason"] == "The grade must be a number from 1 to 5."


def test_a_blueprint_that_belongs_to_another_module_says_where_it_applies(coriolis_db):
    r = qf.get_blueprint("Thrusters", "Increased range", 3)
    assert r["found"] is False
    assert r["reason"] == ("'Increased range' doesn't apply to Thrusters; it applies to Frame Shift Drive. "
                           "Blueprints available for Thrusters: Clean, Dirty, Reinforced.")
    assert r["available_upgrades"] == ["Clean", "Dirty", "Reinforced"]


def test_an_unknown_blueprint_for_a_known_module_lists_the_real_ones(coriolis_db):
    r = qf.get_blueprint("Frame Shift Drive", "Turbo", 2)
    assert r["found"] is False and r["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    assert r["reason"] == ("Ellie doesn't know a blueprint called 'Turbo'. "
                           "Blueprints available for Frame Shift Drive: Faster boot sequence, Increased range, Shielded.")


def test_an_unknown_module_and_a_module_with_no_blueprints(coriolis_db):
    r = qf.get_blueprint("xyzzy", "Dirty", 1)
    assert r["found"] is False and r["reason"].startswith("Ellie doesn't know a module called 'xyzzy'. Core modules she knows:")
    assert "burst laser" in r["reason"] and "weapons, utilities" not in r["reason"]    # name real weapons, not the word "weapons"
    r = qf.get_blueprint("Module Reinforcement Package", "Dirty", 1)
    assert r["found"] is False and r["reason"] == "Module Reinforcement Package has no engineering blueprints in Ellie's data."


@pytest.mark.parametrize("module, upgrade", [("", ""), ("   ", "Dirty"), ("Thrusters", ""), (None, None), (5, 7)])
def test_blank_or_odd_arguments_give_a_clean_reply_not_an_exception(coriolis_db, module, upgrade):
    r = qf.get_blueprint(module, upgrade, 1)
    assert r["found"] is False and isinstance(r["reason"], str) and r["reason"]


def test_every_coriolis_blueprint_and_grade_is_reachable_by_name(coriolis_db):
    """The reachability test from the spec: every (group, blueprint, grade) in Coriolis is found
    by the module's own name, three ways of typing the blueprint, and has Coriolis's cost."""
    conn = _conn(coriolis_db)
    failures = []
    checked = 0
    try:
        groups = [r[0] for r in conn.execute("SELECT DISTINCT grp FROM module_blueprints ORDER BY grp")]
        for grp in groups:
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (grp,)).fetchone()
                      or {"name": {"bh": "armour"}.get(grp, grp)})["name"]
            for (fdname,) in conn.execute("SELECT fdname FROM module_blueprints WHERE grp = ?", (grp,)).fetchall():
                rows = conn.execute("SELECT upgrade_name, grade, materials_json FROM coriolis_blueprints WHERE fdname = ?", (fdname,)).fetchall()
                if not rows:
                    failures.append((grp, fdname, "in modules.json but not in blueprints.json"))
                    continue
                for row in rows:
                    for typed in (row["upgrade_name"], f"{row['upgrade_name']} {module}", fdname):
                        checked += 1
                        got = qf.get_blueprint(module, typed, row["grade"])
                        want = json.loads(row["materials_json"])
                        if not got["found"] or got["materials"] != want:
                            failures.append((grp, fdname, row["grade"], typed, got.get("reason") or got.get("materials")))
    finally:
        conn.close()
    assert checked > 1500
    assert failures == []


def test_the_spreadsheet_row_wins_when_it_has_the_module_upgrade_and_grade(coriolis_db):
    _add_sheet(coriolis_db, blueprint_rows=[(
        "core_internal", "Frame Shift Drive", "Increased range", 5,
        json.dumps([{"qty": 2, "item": "Arsenic"}]), "More jump range")])
    r = qf.get_blueprint("FSD", "Increased FSD Range", 5)         # found through a different spelling of both names
    assert r["source"] == "excel" and r["materials"] == {"Arsenic": 2} and r["effect"] == "More jump range"
    assert qf.get_blueprint("FSD", "Increased range", 4)["source"] == "coriolis"      # no sheet row for grade 4


def test_sheet_annotations_never_show_and_a_line_without_a_quantity_is_reported_apart(coriolis_db):
    _add_sheet(coriolis_db, blueprint_rows=[(
        "optional_internal", "Fuel Scoop", "Shielded", 3,
        json.dumps([{"qty": 1, "item": "Conductive Polymers (0)"}, {"qty": 5, "item": "Merc Coin"},
                    {"qty": None, "item": "Needs personal research"}]), None)])
    r = qf.get_blueprint("Fuel Scoop", "Shielded", 3)
    assert r["source"] == "excel" and r["materials"] == {"Conductive Polymers": 1, "Merc Coin": 5}
    assert r["other_requirements"] == ["Needs personal research"]
    assert r["summary"] == ("Fuel Scoop, Shielded, grade 3, per roll: 1 Conductive Polymers, 5 Merc Coin; "
                            "also listed: Needs personal research.")


def test_a_database_from_before_this_change_gets_the_setup_message(coriolis_db):
    conn = sqlite3.connect(coriolis_db)
    conn.execute("DROP TABLE module_group_names")
    conn.commit()
    conn.close()
    r = qf.get_blueprint("Frame Shift Drive", "Increased range", 5)
    assert r["found"] is False and "python setup.py" in r["reason"]


def _hold(path, key, shown, count, category="Encoded"):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO materials_inventory VALUES (?,?,?,?,?)", (key, category, shown, count, "2026-11-06T00:00:00Z"))
    conn.commit()
    conn.close()


def test_missing_materials_sees_what_the_commander_holds_under_the_game_s_name(coriolis_db):
    _hold(coriolis_db, "disruptedwakeechoes", "Atypical Disrupted Wake Echoes", 64)       # the bug: this used to read as 0
    r = qf.missing_materials("Frame Shift Drive", "Increased range", 1)
    assert r["found"] is True and r["have_everything"] is True and r["missing"] == {}
    assert r["summary"] == "You have everything for Frame Shift Drive, Increased range, grade 1 (per roll): 1 Atypical Disrupted Wake Echoes."


def test_missing_materials_names_what_is_short_and_how_much(coriolis_db):
    _hold(coriolis_db, "chemicalprocessors", "Chemical Processors", 0, "Manufactured")
    r = qf.missing_materials("Frame Shift Drive", "Increased range", 2)               # 1 Atypical Disrupted Wake Echoes + 1 Chemical Processors
    assert r["have_everything"] is False
    assert r["missing"] == {"Atypical Disrupted Wake Echoes": {"need": 1, "have": 0, "short_by": 1},
                            "Chemical Processors": {"need": 1, "have": 0, "short_by": 1}}
    assert r["summary"] == ("For Frame Shift Drive, Increased range, grade 2 (per roll) you need "
                            "1 Atypical Disrupted Wake Echoes, 1 Chemical Processors. You are short by: "
                            "1 Atypical Disrupted Wake Echoes (have 0), 1 Chemical Processors (have 0).")


def test_missing_materials_does_not_call_an_unrecorded_cost_nothing_missing(coriolis_db):
    r = qf.missing_materials("Cargo Rack", "Expanded Capacity", 5)
    assert r["found"] is True and r["cost_known"] is False and r["have_everything"] is None and r["missing"] == {}
    assert "no materials are recorded" in r["summary"]


def test_missing_materials_carries_a_blueprint_miss_through(coriolis_db):
    r = qf.missing_materials("Frame Shift Drive", "Turbo", 2)
    assert r["found"] is False and r["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
    assert r["module"] == "Frame Shift Drive" and r["upgrade"] == "Turbo" and r["grade"] == 2
    assert qf.missing_materials("Frame Shift Drive", "Increased range", "five")["reason"] == "The grade must be a number from 1 to 5."


def test_an_effect_that_fits_the_module_returns_its_cost_per_application(coriolis_db):
    r = qf.get_experimental_effect("Frame Shift Drive", "Mass Manager")
    assert r["found"] is True and r["source"] == "coriolis"
    assert r["materials"] == {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}
    assert r["materials_per"] == "application" and r["cost_known"] is True
    assert r["summary"] == ("Frame Shift Drive, Mass Manager, each time it is applied: "
                            "5 Atypical Disrupted Wake Echoes, 3 Galvanising Alloys, 1 Eccentric Hyperspace Trajectories.")


def test_an_effect_the_module_cannot_take_is_refused_and_says_where_it_fits(coriolis_db):
    r = qf.get_experimental_effect("Thrusters", "Mass Manager")
    assert r["found"] is False
    assert r["reason"] == ("'Mass Manager' can't be applied to Thrusters; it applies to Frame Shift Drive. "
                           "Experimental effects available for Thrusters: "
                           "Double Braced, Drag Drives, Drive Distributors, Stripped Down, Thermal Spread.")
    assert r["available_effects"] == ["Double Braced", "Drag Drives", "Drive Distributors", "Stripped Down", "Thermal Spread"]


def test_the_same_effect_name_means_the_right_effect_for_each_module(coriolis_db):
    weapon = qf.get_experimental_effect("Cannon", "Stripped Down")
    engine = qf.get_experimental_effect("Thrusters", "Stripped Down")
    assert weapon["found"] and engine["found"] and weapon["materials"] != engine["materials"]


def test_an_unknown_effect_unknown_module_and_a_module_without_effects(coriolis_db):
    r = qf.get_experimental_effect("Frame Shift Drive", "Turbo")
    assert r["found"] is False and r["reason"].startswith("Ellie doesn't know an experimental effect called 'Turbo'. Experimental effects available for Frame Shift Drive:")
    assert qf.get_experimental_effect("xyzzy", "Mass Manager")["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
    assert qf.get_experimental_effect("Module Reinforcement Package", "Mass Manager")["reason"] == "Module Reinforcement Package has no experimental effects in Ellie's data."
    assert qf.get_experimental_effect("", "")["found"] is False


def test_every_effect_coriolis_lists_for_a_group_is_reachable_by_name(coriolis_db):
    conn = _conn(coriolis_db)
    failures, checked = [], 0
    try:
        for row in conn.execute("SELECT grp, edname FROM module_specials ORDER BY grp, edname").fetchall():
            effect = conn.execute("SELECT name, materials_json FROM coriolis_experimental_effects WHERE lower(edname) = lower(?)", (row["edname"],)).fetchone()
            module = (conn.execute("SELECT name FROM module_group_names WHERE grp = ? ORDER BY length(name) DESC", (row["grp"],)).fetchone() or {"name": row["grp"]})["name"]
            if effect is None:
                failures.append((row["grp"], row["edname"], "listed for the group but missing from specials.json"))
                continue
            checked += 1
            got = qf.get_experimental_effect(module, effect["name"])
            if not got["found"] or got["materials"] != json.loads(effect["materials_json"]):
                failures.append((row["grp"], row["edname"], got.get("reason") or got.get("materials")))
    finally:
        conn.close()
    assert checked > 100
    assert failures == []


def test_a_spreadsheet_effect_row_is_used_first_and_its_annotations_are_cleaned(coriolis_db):
    _add_sheet(coriolis_db, cost_rows=[(
        "core_internal_experimental", "Frame Shift Drive", "Mass Manager",
        json.dumps([{"qty": 5, "item": "Atypical Disrupted Wake Echoes (3)"}]), "Heavier ship")])
    r = qf.get_experimental_effect("FSD", "mass manager")
    assert r["source"] == "excel" and r["materials"] == {"Atypical Disrupted Wake Echoes": 5} and r["description"] == "Heavier ship"


def test_effects_the_commander_applied_to_a_missile_rack_are_not_refused(coriolis_db):
    drag = qf.get_experimental_effect("Missile Rack", "Drag Munitions")
    assert drag["found"] is True and drag["materials"] == {"Carbon": 5, "Grid Resistors": 5, "Molybdenum": 2}
    cascade = qf.get_experimental_effect("Missile Rack", "Thermal Cascade")
    assert cascade["found"] is True and cascade["materials"]["Phosphorus"] == 5


def test_stripped_down_on_weapons_uses_the_cost_the_game_charged(coriolis_db):
    assert qf.get_experimental_effect("Pulse Laser", "Stripped Down")["materials"] == {"Salvaged Alloys": 5, "Carbon": 5, "Tin": 1}


def test_target_lock_breaker_uses_the_games_spelling_of_its_material(coriolis_db):
    r = qf.get_experimental_effect("Plasma Accelerator", "Target lock breaker")
    assert r["found"] is True and "Adaptive Encryptors Capture" in r["materials"]
    assert not any("Encyptors" in name for name in r["materials"])


# --- final review, finding 1: a name that only shares a word must be a miss, never a different blueprint ---

@pytest.mark.parametrize("module, name", [
    ("pulse laser", "Efficient Weapon"), ("pulse laser", "Lightweight weapon"), ("armour", "Kinetic Resistant Armour"),
])
def test_an_effect_name_that_only_shares_a_word_is_a_miss_not_a_wrong_cost(coriolis_db, module, name):
    r = qf.get_experimental_effect(module, name)
    assert r["found"] is False, r


@pytest.mark.parametrize("module, name", [
    ("pulse laser", "Engine Focused"), ("burst laser", "System Focused"), ("plasma accelerator", "Engine Focused"),
    ("shield cell bank", "Rapid fire"), ("sensors", "Expanded Capacity"),
    ("frame shift drive interdictor", "Expanded Capacity"), ("thrusters", "Reinforced Shields"),
])
def test_a_blueprint_name_with_foreign_words_is_a_miss_not_a_different_blueprint(coriolis_db, module, name):
    r = qf.get_blueprint(module, name, 1)
    assert r["found"] is False, r


def test_the_swap_simulator_gets_the_same_protection(coriolis_db):
    conn = _conn(coriolis_db)
    try:
        row, miss = qf._find_blueprint(conn, "pl", 1, "Engine Focused", "pulse laser")
        assert row is None and miss["found"] is False
        row, miss = qf._find_blueprint(conn, "pl", 1, "Focused", "pulse laser")
        assert miss is None and row["fdname"] == "Weapon_Focused"
        row, miss = qf._find_blueprint(conn, "pl", 1, "Focused weapon", "pulse laser")     # 'weapon' is a generic word: still fine
        assert miss is None and row["fdname"] == "Weapon_Focused"
    finally:
        conn.close()


def test_the_modules_own_words_are_still_welcome_around_a_blueprint_name(coriolis_db):
    for module, name, upgrade in [
        ("thrusters", "Dirty Drive Tuning", "Dirty"), ("frame shift drive", "Shielded FSD", "Shielded"),
        ("power plant", "Low Emissions Power Plant", "Low emissions"), ("sensors", "Long Range Sensors", "Long range"),
        ("armour", "Heavy Duty Armour", "Heavy duty"), ("beam laser", "Efficient Weapon", "Efficient"),
    ]:
        r = qf.get_blueprint(module, name, 1)
        assert r["found"] is True and r["upgrade"] == upgrade, (module, name, r.get("reason"))


def test_resolve_blueprint_gives_the_one_blueprint_or_a_miss(coriolis_db):
    conn = _conn(coriolis_db)
    try:
        groups = qf._resolve_module_groups(conn, "FSD")
        resolved, miss = qf._resolve_blueprint(conn, "FSD", "Increased FSD Range", groups)
        assert miss is None
        assert resolved == {"fdname": "FSD_LongRange", "upgrade_name": "Increased range", "grades": [1, 2, 3, 4, 5]}
        resolved, miss = qf._resolve_blueprint(conn, "FSD", "Turbo", groups)
        assert resolved is None and miss["available_upgrades"] == ["Faster boot sequence", "Increased range", "Shielded"]
        resolved, miss = qf._resolve_blueprint(conn, "xyzzy", "Dirty", [])
        assert resolved is None and miss["reason"].startswith("Ellie doesn't know a module called 'xyzzy'.")
        groups = qf._resolve_module_groups(conn, "Module Reinforcement Package")
        resolved, miss = qf._resolve_blueprint(conn, "Module Reinforcement Package", "Dirty", groups)
        assert resolved is None and miss["reason"] == "Module Reinforcement Package has no engineering blueprints in Ellie's data."
    finally:
        conn.close()
