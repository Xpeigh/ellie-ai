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
