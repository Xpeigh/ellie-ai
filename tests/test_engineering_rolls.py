import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engineering_rolls as er
import rolls_from_journal as rj

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "observed_rolls.json"


def _craft(engineer, module, blueprint, grade, effect=None):
    event = {"event": "EngineerCraft", "Engineer": engineer, "Module": module, "BlueprintName": blueprint, "Level": grade}
    if effect:
        event["ApplyExperimentalEffect"] = effect
    return json.dumps(event)


def _progress(engineer, rank):
    return json.dumps({"event": "EngineerProgress", "Engineer": engineer, "Rank": rank})


def _segments(tmp_path, *lines):
    (tmp_path / "Journal.2026-11-08T000000.01.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rj.finished_segments(rj.read_craft_events(tmp_path))


def test_a_rank_5_engineer_run_gives_n_rolls_per_finished_grade(tmp_path):
    lines = [_progress("Felicity Farseer", 5)] + [_craft("Felicity Farseer", "m1", "FSD_LongRange", g) for g in (1, 2, 2, 3, 3, 3)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 1, "rolls": 1}, {"rank5": True, "grade": 2, "rolls": 2}]
    # grade 3 is the last step and below grade 5, so it may be unfinished: not counted


def test_a_lower_rank_engineer_is_recorded_as_not_rank_5(tmp_path):
    lines = [_progress("Mel Brandon", 3)] + [_craft("Mel Brandon", "m1", "Engine_Dirty", g) for g in (1, 1, 1, 1, 2, 2, 2, 2, 3)]
    assert _segments(tmp_path, *lines) == [{"rank5": False, "grade": 1, "rolls": 4}, {"rank5": False, "grade": 2, "rolls": 4}]


def test_the_last_grade_5_step_counts_because_nothing_can_follow_it(tmp_path):
    lines = [_progress("Felicity Farseer", 5)] + [_craft("Felicity Farseer", "m1", "FSD_LongRange", g) for g in (4, 4, 4, 4, 5, 5, 5, 5, 5)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 4, "rolls": 4}, {"rank5": True, "grade": 5, "rolls": 5}]


def test_experimental_crafts_other_events_and_bad_lines_are_ignored(tmp_path):
    lines = [_progress("Felicity Farseer", 5), "not json", '{"event":"Music"}',
             _craft("Felicity Farseer", "m1", "FSD_LongRange", 1), _craft("Felicity Farseer", "m1", "FSD_LongRange", 1, effect="special_fsd_heavy"),
             _craft("Felicity Farseer", "m1", "FSD_LongRange", 2), _craft("Felicity Farseer", "m1", "FSD_LongRange", 2)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 1, "rolls": 1}]


def test_a_new_module_or_engineer_starts_a_new_run(tmp_path):
    lines = [_progress("Felicity Farseer", 5), _craft("Felicity Farseer", "m1", "FSD_LongRange", 1), _craft("Felicity Farseer", "m2", "FSD_LongRange", 2)]
    assert _segments(tmp_path, *lines) == []                  # grade 1 on m1 was never followed by grade 2 on m1


def test_a_craft_with_no_known_rank_is_skipped(tmp_path):
    lines = [_craft("Nobody", "m1", "X_Y", 1), _craft("Nobody", "m1", "X_Y", 2)]
    assert _segments(tmp_path, *lines) == []


def test_the_startup_engineers_list_gives_each_unlocked_engineers_rank(tmp_path):
    startup = json.dumps({"event": "EngineerProgress", "Engineers": [
        {"Engineer": "Felicity Farseer", "Progress": "Unlocked", "Rank": 5},
        {"Engineer": "Mel Brandon", "Progress": "Known"}]})                      # Known: no Rank key at all
    lines = [startup] + [_craft("Felicity Farseer", "m1", "FSD_LongRange", g) for g in (1, 2, 2)]
    assert _segments(tmp_path, *lines) == [{"rank5": True, "grade": 1, "rolls": 1}]


def test_build_table_makes_rank_5_a_rule_and_below_5_the_most_common_value():
    segments = ([{"rank5": True, "grade": 1, "rolls": 1}] * 3 + [{"rank5": True, "grade": 3, "rolls": 2}]
                + [{"rank5": False, "grade": 1, "rolls": r} for r in (3, 4, 4, 4, 5)]
                + [{"rank5": False, "grade": 2, "rolls": 4}] * 6)
    table = rj.build_table(segments, "test basis")
    assert table["basis"] == "test basis"
    assert table["rank5"]["1"] == {"typical": 1, "up_to": 1, "samples": 3, "source": "rule"}
    assert table["rank5"]["3"] == {"typical": 3, "up_to": 3, "samples": 1, "source": "rule"}      # an early finish never lowers the rule
    assert table["rank5"]["5"] == {"typical": 5, "up_to": 5, "samples": 0, "source": "rule"}
    assert table["below5"]["1"] == {"typical": 4, "up_to": 5, "samples": 5, "source": "observed", "share": 0.6}
    assert table["below5"]["2"] == {"typical": 4, "up_to": 4, "samples": 6, "source": "observed", "share": 1.0}
    assert "3" not in table["below5"] and "5" not in table["below5"]


def test_a_tie_between_two_values_takes_the_smaller():
    table = rj.build_table([{"rank5": False, "grade": 1, "rolls": r} for r in (3, 3, 5, 5)], "b")
    assert table["below5"]["1"]["typical"] == 3


def test_only_observed_entries_can_be_thin():
    assert er.is_thin({"source": "rule", "samples": 0}) is False
    assert er.is_thin({"source": "observed", "samples": 4, "share": 1.0}) is True        # too few
    assert er.is_thin({"source": "observed", "samples": 9, "share": 0.56}) is True       # varies too much
    assert er.is_thin({"source": "observed", "samples": 7, "share": 1.0}) is False


def test_thin_boundaries_are_inclusive_of_the_good_side():
    assert er.is_thin({"source": "observed", "samples": 5, "share": 0.75}) is False        # exactly 5 samples, exactly 3 in 4
    assert er.is_thin({"source": "observed", "samples": 5, "share": 0.74}) is True


def test_rolls_for_picks_the_rank_table_and_has_no_grade_5_below_rank_5():
    table = rj.build_table([{"rank5": False, "grade": 1, "rolls": 4}] * 6, "b")
    assert er.rolls_for(table, True, 5)["typical"] == 5 and er.rolls_for(table, True, 5)["thin"] is False
    assert er.rolls_for(table, False, 1)["typical"] == 4 and er.rolls_for(table, False, 1)["thin"] is False
    assert er.rolls_for(table, False, 5) is None and er.rolls_for(table, False, 2) is None


def test_the_shipped_table_is_exactly_what_the_fixture_of_real_runs_produces():
    segments = json.load(open(FIXTURE, encoding="utf-8"))
    built = rj.build_table(segments, "x")
    shipped = er.load_rolls()
    assert built["rank5"] == shipped["rank5"] and built["below5"] == shipped["below5"]
    assert shipped["basis"].strip()


def test_the_shipped_table_says_what_the_owner_and_the_journal_found():
    shipped = er.load_rolls()
    for grade in range(1, 6):
        assert er.rolls_for(shipped, True, grade)["typical"] == grade                    # N rolls for grade N at rank 5
    assert [er.rolls_for(shipped, False, g)["typical"] for g in (2, 3, 4)] == [4, 4, 4]  # about 4 each below rank 5
    assert er.rolls_for(shipped, False, 1)["thin"] is True                               # grade 1 varies (3, 4 or 5)
    assert er.rolls_for(shipped, False, 2)["thin"] is False
    assert er.rolls_for(shipped, False, 5) is None
