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
