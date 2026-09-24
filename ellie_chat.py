"""
Wires the chat model (via Ollama's native tool-calling) to the
query_functions.py functions -- this is the trust boundary in action.
Ellie must never state a material count, distance, or blueprint
requirement from her own "knowledge," only from what a tool returns.

Needs a running Ollama (usually automatic once installed) with the model
pulled: `ollama pull granite4.1:3b`. Pass another model name as the first
argument to try a different one: `python ellie_chat.py qwen3:4b`.

The message/tool_call handling follows Ollama's official examples
(ollama-python examples/tools.py, examples/multi-tool.py). How reliably a
given model picks the right tool and fills in arguments is model-dependent
-- granite4.1:3b was chosen from a head-to-head test, see plan.md.
"""
import json

from ollama import chat

import query_functions as qf

import sys

MODEL = sys.argv[1] if len(sys.argv) > 1 else "granite4.1:3b"
MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """You are Ellie, a personal Elite Dangerous engineering assistant running on the commander's own machine.

RULES:
- NEVER state a material count, blueprint requirement, distance, or any other specific number from your own knowledge or training data. ALWAYS call the matching tool and report only what it returns.
- When reporting numbers, names, or lists from a tool result, use them EXACTLY as given. Never round, approximate, or paraphrase into "selected examples" -- if something is too long to repeat in full, say so plainly and offer to narrow it down, rather than inventing a partial illustrative sample.
- If a tool result has "found": false, don't guess or fall back to general knowledge. If it has a "reason", pass that reason on in plain words -- it may be asking for a detail (like which slot) rather than saying the data is missing. Otherwise, tell the commander plainly that you don't have that data.
- If a tool result has a non-empty "unmodeled_blueprint_effects" list, mention plainly that those specific effects aren't reflected in the numbers -- don't just report the numbers as if they were the whole picture.
- If a tool result has a "variants" list, the request matched several different real modules -- list them for the commander and ask which one they mean (then call the tool again with `variant`); don't say the data is missing, and don't pick one yourself.
- If a tool result has a "caveat", tell the commander it along with the numbers -- never drop it.
- Once you have enough information to call a tool, call it immediately. Don't ask the commander to confirm an obvious detail (like a system name they already gave you) before proceeding.
- Keep answers short and conversational, like a ship's computer, not a report.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_blueprint",
            "description": "Get the materials required for a specific ship module blueprint upgrade at a given grade (1-5).",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name, e.g. 'Beam Laser', 'Power Plant'"},
                    "upgrade_name": {"type": "string", "description": "Blueprint/upgrade name, e.g. 'Efficient', 'Overcharged'"},
                    "grade": {"type": "integer", "description": "Blueprint grade, 1 to 5"},
                },
                "required": ["module_name", "upgrade_name", "grade"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_experimental_effect",
            "description": "Get the materials required for an experimental effect (ungraded, single-cost modification) on a module.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Module name, e.g. 'Beam Laser'"},
                    "effect_name": {"type": "string", "description": "Experimental effect name, e.g. 'Drag Munitions', 'Auto Loader'"},
                },
                "required": ["module_name", "effect_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "missing_materials",
            "description": "Check which materials, if any, are missing to apply a blueprint upgrade at a given grade, compared to what the commander currently has on hand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string"},
                    "upgrade_name": {"type": "string"},
                    "grade": {"type": "integer"},
                },
                "required": ["module_name", "upgrade_name", "grade"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_engineer",
            "description": "Get an engineer's location, prerequisites to unlock, and what blueprints/upgrades they offer.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Engineer's name, e.g. 'Bill Turner'"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_material_count",
            "description": "Get the exact on-hand count for one specific material by name.",
            "parameters": {
                "type": "object",
                "properties": {"material_name": {"type": "string", "description": "Exact material name, e.g. 'Chromium', 'Heat Exchangers'"}},
                "required": ["material_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "player_state",
            "description": "Get the commander's current status (ship, system, station, ranks, credits) and a per-category summary of materials currently on hand.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distance_to_system",
            "description": "Get the straight-line distance in light years from the commander's current system to a named star system.",
            "parameters": {
                "type": "object",
                "properties": {"system_name": {"type": "string", "description": "Name of the destination star system"}},
                "required": ["system_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_loadout",
            "description": "Get the commander's current ship, its real mass and jump range as reported by the game, and every fitted module including real engineering modifiers.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ship_info",
            "description": "Get a named ship's base stats and slot layout.",
            "parameters": {
                "type": "object",
                "properties": {"ship_name": {"type": "string", "description": "Ship name, e.g. 'Python'"}},
                "required": ["ship_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_module_swap",
            "description": "'What if I fit a different module' -- swaps one module in the commander's CURRENT ship for a hypothetical one and reports the real resulting mass, max jump range (unladen, one jump's fuel), and power changes, or whether it's compatible at all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_group": {"type": "string", "description": "e.g. 'frame shift drive', 'power plant', 'shield booster'"},
                    "module_class": {"type": "integer", "description": "Module class/size, 0-8 (utility mounts such as shield boosters are class 0)"},
                    "module_rating": {"type": "string", "description": "Module rating letter, e.g. 'A'"},
                    "blueprint_name": {"type": "string", "description": "Optional engineering blueprint to apply to the hypothetical module, e.g. 'Increased range'"},
                    "grade": {"type": "integer", "description": "Blueprint grade 1-5, required if blueprint_name is given"},
                    "slot": {"type": "string", "description": "Optional exact slot name, e.g. 'Slot05_Size4' or 'MediumHardpoint2' -- needed if the ship has more than one slot of that type, or to put the module in an empty slot"},
                    "variant": {"type": "string", "description": "Optional, only needed when several modules share the same type/class/rating -- e.g. 'gimballed', 'SCO', 'Mk II', 'guardian', or an exact symbol from a previous result's variants list"},
                },
                "required": ["module_group", "module_class", "module_rating"],
            },
        },
    },
]

DISPATCH = {
    "get_blueprint": qf.get_blueprint,
    "get_experimental_effect": qf.get_experimental_effect,
    "missing_materials": qf.missing_materials,
    "get_engineer": qf.get_engineer,
    "player_state": qf.player_state,
    "distance_to_system": qf.distance_to_system,
    "get_material_count": qf.get_material_count,
    "get_current_loadout": qf.get_current_loadout,
    "get_ship_info": qf.get_ship_info,
    "simulate_module_swap": qf.simulate_module_swap,
}


def call_tool(name, arguments):
    """Executes a tool call and returns a JSON-serializable result. Never
    raises -- an unknown tool name, bad arguments, or an unexpected failure
    inside the tool itself all become a clear error dict the model can
    relay to the user, not a crash of the whole chat loop. query_functions
    already turns "database isn't set up yet" into a normal {"found":
    False} result; the broad except below is a last-resort backstop for
    anything that isn't that (a real bug), not the primary handling for it."""
    func = DISPATCH.get(name)
    if func is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return func(**arguments)
    except TypeError as e:
        return {"error": f"bad arguments for '{name}': {e}"}
    except Exception as e:
        return {"error": f"'{name}' failed unexpectedly: {e}"}


def ask_ellie(user_message, history):
    """history is a list of message dicts starting with the system
    prompt; mutated and returned so a caller can keep a conversation
    going across calls."""
    history.append({"role": "user", "content": user_message})

    for _ in range(MAX_TOOL_ROUNDS):
        response = chat(model=MODEL, messages=history, tools=TOOLS)
        msg = response.message
        history.append(msg)

        if not msg.tool_calls:
            return msg.content, history

        for call in msg.tool_calls:
            result = call_tool(call.function.name, call.function.arguments)
            history.append(
                {"role": "tool", "tool_name": call.function.name, "content": json.dumps(result)}
            )

    return "Sorry, I got stuck trying to answer that -- too many tool calls in a row.", history


def main():
    print(f"Ellie is online, running {MODEL}. Ask about blueprints, materials, engineers, or your status. Type 'quit' to exit.")
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("quit", "exit"):
            break
        if not user_input:
            continue
        answer, history = ask_ellie(user_input, history)
        print(f"\nEllie: {answer}")


if __name__ == "__main__":
    main()
