import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import query_functions as qf
import verify_blueprints as vb

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "observed_crafts.json"

# Differences between Coriolis and the owner's own crafts that are understood and recorded in
# docs/blueprint-data.md. A new difference fails the test; so does one that goes away (then
# remove it from this set).
KNOWN_DIFFERENCES = {"special_weapon_lightweight", "special_drag_munitions"}


def _craft(blueprint, level, ingredients, effect=None, when="2026-01-01T00:00:00Z"):
    event = {"timestamp": when, "event": "EngineerCraft", "Slot": "FrameShiftDrive", "Module": "int_hyperdrive_size5_class5",
             "Engineer": "Felicity Farseer", "BlueprintName": blueprint, "Level": level,
             "Ingredients": [{"Name": n.lower().replace(" ", ""), "Name_Localised": n, "Count": c} for n, c in ingredients.items()]}
    if effect:
        event["ApplyExperimentalEffect"] = effect
    return json.dumps(event)


def _journal(tmp_path, *lines):
    (tmp_path / "Journal.2026-01-01T000000.01.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_read_crafts_takes_only_engineer_craft_events_and_the_displayed_names(tmp_path):
    folder = _journal(tmp_path,
                      '{"event":"Music","MusicTrack":"x"}',
                      "this is not json",
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      '{"event":"EngineerCraft","BlueprintName":"Armour_Advanced","Level":1,"Ingredients":[{"Name":"iron","Count":1}]}')
    crafts = vb.read_crafts(folder)
    assert [c["blueprint"] for c in crafts] == ["FSD_LongRange", "Armour_Advanced"]
    assert crafts[0]["ingredients"] == {"Atypical Disrupted Wake Echoes": 1}
    assert crafts[1]["ingredients"] == {"iron": 1}                    # no localised name for raw elements


def test_summarise_counts_repeats_and_keeps_experimental_crafts_apart(tmp_path):
    folder = _journal(tmp_path,
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 1}),
                      _craft("FSD_LongRange", 1, {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1},
                             effect="special_fsd_heavy"))
    summary = vb.summarise(vb.read_crafts(folder))
    assert summary["grade_crafts"] == [{"blueprint": "FSD_LongRange", "grade": 1, "ingredients": {"Atypical Disrupted Wake Echoes": 1}, "crafts": 2}]
    assert summary["experimental_crafts"] == [{"effect": "special_fsd_heavy", "crafts": 1,
                                               "ingredients": {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}}]


def test_two_spellings_of_one_material_are_one_ingredient(tmp_path):
    folder = _journal(tmp_path,
                      '{"event":"EngineerCraft","BlueprintName":"Armour_Advanced","Level":1,"Ingredients":[{"Name":"iron","Count":1}]}',
                      '{"event":"EngineerCraft","BlueprintName":"Armour_Advanced","Level":1,"Ingredients":[{"Name":"iron","Name_Localised":"Iron","Count":1}]}')
    summary = vb.summarise(vb.read_crafts(folder))
    assert summary["grade_crafts"] == [{"blueprint": "Armour_Advanced", "grade": 1, "ingredients": {"Iron": 1}, "crafts": 2}]


def test_compare_reports_match_mismatch_and_unknown(coriolis_db):
    summary = {
        "grade_crafts": [
            {"blueprint": "FSD_LongRange", "grade": 5, "crafts": 3,
             "ingredients": {"Arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}},
            {"blueprint": "FSD_LongRange", "grade": 4, "crafts": 1, "ingredients": {"Arsenic": 9}},
            {"blueprint": "No_Such_Blueprint", "grade": 1, "crafts": 1, "ingredients": {"Carbon": 1}},
        ],
        "experimental_crafts": [
            {"effect": "special_fsd_heavy", "crafts": 1,
             "ingredients": {"Atypical Disrupted Wake Echoes": 5, "Galvanising Alloys": 3, "Eccentric Hyperspace Trajectories": 1}},
        ],
    }
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        results = vb.compare(conn, summary)
    finally:
        conn.close()
    assert [(r["id"], r["status"]) for r in results] == [
        ("FSD_LongRange", "match"), ("FSD_LongRange", "mismatch"), ("No_Such_Blueprint", "unknown"), ("special_fsd_heavy", "match")]
    assert results[1]["expected"] == {"Chemical Distillery": 1, "Eccentric Hyperspace Trajectories": 1, "Manganese": 1}


def test_a_name_the_journal_stores_differently_still_matches(coriolis_db):
    """The journal names raw elements in lower case ('arsenic'); Coriolis capitalises them."""
    summary = {"grade_crafts": [{"blueprint": "FSD_LongRange", "grade": 5, "crafts": 1,
                                 "ingredients": {"arsenic": 1, "Chemical Manipulators": 1, "Datamined Wake Exceptions": 1}}],
               "experimental_crafts": []}
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        assert vb.compare(conn, summary)[0]["status"] == "match"
    finally:
        conn.close()


def test_the_owner_s_real_crafts_match_coriolis_except_the_recorded_differences(coriolis_db):
    summary = json.load(open(FIXTURE, encoding="utf-8"))
    assert len(summary["grade_crafts"]) == 48 and len(summary["experimental_crafts"]) == 9
    conn = sqlite3.connect(coriolis_db)
    conn.row_factory = sqlite3.Row
    try:
        results = vb.compare(conn, summary)
    finally:
        conn.close()
    assert [r for r in results if r["status"] == "unknown"] == []
    assert {r["id"] for r in results if r["status"] == "mismatch"} == KNOWN_DIFFERENCES
