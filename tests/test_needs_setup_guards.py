"""Task 10c: every tool that reads tables a half-finished setup may not have
built must answer with the plain "run python setup.py" reason instead of
raising -- and the decorator that does it must sit on the tool, not on a
helper that happens to be defined next to it."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec
import journal_watcher as jw
import query_functions as qf

LOADOUT = {
    "timestamp": "2026-10-23T09:28:30Z", "event": "Loadout", "Ship": "type9", "ShipID": 43,
    "UnladenMass": 1122.0, "CargoCapacity": 348, "MaxJumpRange": 9.164523,
    "FuelCapacity": {"Main": 64.0, "Reserve": 0.77},
    "Modules": [
        {"Slot": "LifeSupport", "Item": "int_lifesupport_size5_class1", "On": True, "Priority": 1, "Health": 1.0},
    ],
}


def loadout_without_module_data(tmp_path):
    """A Loadout was captured, but ingest_ship_modules never ran: no ship_stats,
    module_stats or module_blueprints."""
    path = str(tmp_path / "half_setup.db")
    conn = sqlite3.connect(path)
    jw.build_schema(conn)
    jw.process_event(conn, LOADOUT)
    conn.close()
    qf.DB_PATH = path


def test_a_swap_with_no_module_data_gives_the_setup_reason(tmp_path):
    loadout_without_module_data(tmp_path)
    result = qf.simulate_module_swap("life support", 5, "D")
    assert result["found"] is False
    assert result["reason"].endswith("Run `python setup.py`.")
    assert result["reason"].startswith("Ellie's database isn't fully set up yet (no such table: module_stats)")


def test_it_is_the_same_reason_get_current_loadout_gives(tmp_path):
    loadout_without_module_data(tmp_path)
    assert qf.simulate_module_swap("life support", 5, "D") == qf.get_current_loadout()


def test_a_swap_with_junk_arguments_still_gets_the_setup_reason(tmp_path):
    loadout_without_module_data(tmp_path)
    result = qf.simulate_module_swap("life support", 5, "D", slot="None", variant="null", blueprint_name="")
    assert result["found"] is False and "Run `python setup.py`." in result["reason"]


@pytest.mark.parametrize("name", sorted(ec.DISPATCH))
def test_every_tool_is_guarded_by_needs_setup(name):
    func = ec.DISPATCH[name]
    if name == "player_state":
        return          # has its own try/except: it can't share the decorator (see its docstring)
    assert hasattr(func, "__wrapped__"), f"{name} is not wrapped by @_needs_setup"
    assert func.__name__ == name


def test_helpers_are_not_wrapped_by_the_decorator():
    """The bug this guards against: `def _unset` inserted between the decorator
    and the tool, so the helper was wrapped and the tool wasn't."""
    for name in ("_unset", "_which_slot_reason", "_readable_modules", "_match_ship", "_swap_summary", "_capacity_note"):
        assert not hasattr(getattr(qf, name), "__wrapped__"), f"{name} is wrapped"


def test_each_needs_setup_decorator_sits_directly_above_a_public_tool():
    lines = Path(qf.__file__).read_text(encoding="utf-8").splitlines()
    decorated = [lines[i + 1] for i, line in enumerate(lines) if line.strip() == "@_needs_setup"]
    names = [text.split("(")[0].replace("def ", "").strip() for text in decorated]
    assert all(text.startswith("def ") for text in decorated)
    assert sorted(names) == sorted(n for n in ec.DISPATCH if n != "player_state")
