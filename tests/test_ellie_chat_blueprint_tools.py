import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec


def _tool(name):
    return next(t["function"] for t in ec.TOOLS if t["function"]["name"] == name)


def test_get_blueprint_says_per_roll_and_covers_the_core_modules():
    tool = _tool("get_blueprint")
    assert tool["description"] == (
        "Get the materials ONE roll of an engineering blueprint costs at a given grade (1-5) for one specific ship "
        "module: a frame shift drive, thrusters, a power plant, a shield generator, or a particular "
        "weapon, utility or optional internal.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name only, without any blueprint word, e.g. 'Frame Shift Drive', 'Thrusters', 'Shield Generator'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"


def test_missing_materials_describes_one_roll_and_its_arguments():
    tool = _tool("missing_materials")
    assert tool["description"] == (
        "Check which materials, if any, are missing for ONE roll of an engineering blueprint at a given grade, "
        "compared to what the commander currently has on hand.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name only, without any blueprint word, e.g. 'Frame Shift Drive', 'Thrusters', 'Shield Generator'"
    assert props["upgrade_name"]["description"] == "Blueprint name, e.g. 'Increased range', 'Dirty', 'Overcharged'"
    assert props["grade"]["description"] == "Blueprint grade, 1 to 5"


def test_the_experimental_effect_example_is_a_module_that_can_take_it():
    tool = _tool("get_experimental_effect")
    assert tool["description"] == (
        "Get the materials an experimental effect costs each time it is applied to a module "
        "(ungraded, single cost). The effect must be one that module can take.")
    props = tool["parameters"]["properties"]
    assert props["module_name"]["description"] == "Module name, e.g. 'Frame Shift Drive', 'Cannon'"
    assert props["effect_name"]["description"] == "Experimental effect name, e.g. 'Mass Manager', 'Drag Munitions'"
