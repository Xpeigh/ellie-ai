import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def _tool():
    return next(t["function"] for t in ec.TOOLS if t["function"]["name"] == "plan_upgrade")


def test_plan_upgrade_is_registered_and_dispatched():
    assert "plan_upgrade" in ec.DISPATCH and _tool()


def test_the_description_has_no_category_words_and_names_what_comes_back():
    tool = _tool()
    assert tool["description"] == (
        "Plan an engineering upgrade of one specific module up to a grade (1-5): rolls per grade, total materials, "
        "what is short, and which engineers can do it.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name as the commander said it, e.g. 'Frame Shift Drive', 'Thrusters', 'Shield Generator'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"
    assert props["target_grade"]["description"] == "Grade to reach, 1 to 5"
    assert props["from_grade"]["description"] == "Grade the module is at now (0 if unengineered); only when the commander says"
    assert props["experimental_effect"]["description"] == "Optional experimental effect to include, e.g. 'Mass Manager'"
    assert tool["parameters"]["required"] == ["module_name", "upgrade_name", "target_grade"]


def test_call_tool_reaches_the_planner(monkeypatch):
    calls = []
    monkeypatch.setitem(ec.DISPATCH, "plan_upgrade", lambda **kw: calls.append(kw) or {"found": False})
    ec.call_tool("plan_upgrade", {"module_name": "FSD", "upgrade_name": "Increased range", "target_grade": 5})
    assert calls == [{"module_name": "FSD", "upgrade_name": "Increased range", "target_grade": 5}]


def test_bad_arguments_are_a_clean_error_not_a_crash():
    assert "bad arguments for 'plan_upgrade'" in ec.call_tool("plan_upgrade", {"wrong_kw": 1})["error"]
