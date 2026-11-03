import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def test_new_tools_are_dispatched():
    for name in ("get_current_loadout", "get_ship_info", "simulate_module_swap"):
        assert name in ec.DISPATCH, f"{name} missing from DISPATCH"
        tool_names = [t["function"]["name"] for t in ec.TOOLS]
        assert name in tool_names, f"{name} missing from TOOLS"


def test_call_tool_reaches_get_ship_info(monkeypatch):
    calls = []
    monkeypatch.setitem(ec.DISPATCH, "get_ship_info", lambda ship_name: calls.append(ship_name) or {"found": False})
    ec.call_tool("get_ship_info", {"ship_name": "Python"})
    assert calls == ["Python"]


def test_call_tool_bad_args_for_simulate_module_swap_is_clean_error():
    result = ec.call_tool("simulate_module_swap", {"wrong_kw": 1})
    assert "error" in result


def _description(tool_name):
    return next(t["function"]["description"] for t in ec.TOOLS if t["function"]["name"] == tool_name)


def test_get_current_loadout_description_names_fuel_cargo_and_the_range_right_now():
    # Task 10b B1 (replaces the 10a wording): fuel and cargo questions must find this tool
    assert _description("get_current_loadout") == (
        "Get the commander's current ship: fuel on board, cargo carried, jump range right now "
        "(with that fuel and cargo), the empty-ship maximum jump range, mass, and every fitted module "
        "with its class, rating and engineering."
    )


def test_player_state_description_sends_fuel_cargo_and_range_questions_elsewhere():
    assert _description("player_state") == (
        "Get the commander's rank, credits, current system and station. It does NOT know fuel, cargo or "
        "jump range: use get_current_loadout for those."
    )
    tool = next(t for t in ec.TOOLS if t["function"]["name"] == "player_state")
    assert tool["function"]["parameters"] == {"type": "object", "properties": {}}      # parameters unchanged


def test_simulate_module_swap_description_names_both_jump_ranges():
    description = _description("simulate_module_swap")
    assert "jump range now (current fuel and cargo) and empty-ship maximum jump range" in description
    assert "max jump range (unladen, one jump's fuel)" not in description
    assert description.startswith("'What if I fit a different module'")
    assert description.endswith("and power changes, or whether it's compatible at all.")


def test_system_prompt_tells_the_model_to_rely_on_summary():
    assert (
        '- If a tool result has a "summary", base your answer on it: you may rephrase, '
        "but keep every number and unit exactly as written."
    ) in ec.SYSTEM_PROMPT


def test_system_prompt_says_which_jump_range_to_give():
    assert (
        '- When asked for the "jump range" without more detail, give jump_range_now '
        "(the range with the current fuel and cargo); mention jump_range_max only as "
        "the empty-ship maximum, never as the current range."
    ) in ec.SYSTEM_PROMPT


def test_system_prompt_says_to_call_again_and_never_reuse_numbers_from_earlier_replies():
    assert (
        "- Fuel, cargo, location and jump range change all the time: call the tool again for every "
        "question about them, and never reuse a number from an earlier reply."
    ) in ec.SYSTEM_PROMPT


def test_summary_then_freshness_then_jump_range_rules_follow_the_caveat_rule():
    lines = ec.SYSTEM_PROMPT.splitlines()
    caveat = next(i for i, line in enumerate(lines) if '"caveat"' in line)
    assert lines[caveat + 1].startswith('- If a tool result has a "summary"')
    assert lines[caveat + 2].startswith("- Fuel, cargo, location and jump range change all the time")   # right after "summary"
    assert lines[caveat + 3].startswith('- When asked for the "jump range" without more detail')
