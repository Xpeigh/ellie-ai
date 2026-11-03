"""
Wires the chat model (via Ollama's native tool-calling) to the
query_functions.py functions -- this is the trust boundary in action.
Ellie must never state a material count, distance, or blueprint
requirement from her own "knowledge," only from what a tool returns.

Needs a running Ollama (usually automatic once installed) with the model
pulled: `ollama pull bonsai-2:4b`. Pass another model name as the first
argument to try a different one: `python ellie_chat.py granite4.1:3b`.

The message/tool_call handling follows Ollama's official examples
(ollama-python examples/tools.py, examples/multi-tool.py). How reliably a
given model picks the right tool and fills in arguments is model-dependent
-- bonsai-2:4b is the default after a benchmark of this very chat loop;
granite4.1:3b, the earlier choice (see plan.md), remains the tested
alternative.

Three things here exist because of how small local models behave:
  - a context window big enough for the tool schemas plus a real answer
    (Ollama's own default, 4,096 tokens, silently truncates the prompt), with
    the oldest finished turns dropped before the prompt would overflow it;
  - old tool results shrunk to their summary once their turn is over, so a
    figure from an earlier reply (fuel, cargo, location) is never presented as
    current and the history doesn't fill the window;
  - thinking off by default, and a retry plus a fixed apology when the model
    returns nothing.
"""
import json
import os
import sys

from ollama import chat

import query_functions as qf

DEFAULT_MODEL = "bonsai-2:4b"
MODEL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
MAX_TOOL_ROUNDS = 5

DEFAULT_NUM_CTX = 8192


def _parse_num_ctx(value):
    """ELLIE_NUM_CTX as a positive integer, else the default."""
    try:
        n = int(str(value).strip())
    except ValueError:
        return DEFAULT_NUM_CTX
    return n if n > 0 else DEFAULT_NUM_CTX


NUM_CTX = _parse_num_ctx(os.environ.get("ELLIE_NUM_CTX"))

EMPTY_ANSWER_FALLBACK = "I couldn't produce an answer that time. Please ask again, or try a shorter question."
STALE_PREFIX = "[earlier result, may be out of date] "
MAX_OPTIONS = 6                # variants kept when a "which one?" reply is compacted
MAX_OPTION_CHARS = 60
CHARS_PER_TOKEN = 3.3          # measured on this project's prompts; deliberately on the cautious side
PROMPT_BUDGET = 0.75           # of the window; the rest is for the answer

SYSTEM_PROMPT = """You are Ellie, a personal Elite Dangerous engineering assistant running on the commander's own machine.

RULES:
- NEVER state a material count, blueprint requirement, distance, or any other specific number from your own knowledge or training data. ALWAYS call the matching tool and report only what it returns.
- When reporting numbers, names, or lists from a tool result, use them EXACTLY as given. Never round, approximate, or paraphrase into "selected examples" -- if something is too long to repeat in full, say so plainly and offer to narrow it down, rather than inventing a partial illustrative sample.
- If a tool result has "found": false, don't guess or fall back to general knowledge. If it has a "reason", pass that reason on in plain words -- it may be asking for a detail (like which slot) rather than saying the data is missing. Otherwise, tell the commander plainly that you don't have that data.
- If a tool result has a non-empty "unmodeled_blueprint_effects" list, mention plainly that those specific effects aren't reflected in the numbers -- don't just report the numbers as if they were the whole picture.
- If a tool result has a "variants" list, the request matched several different real modules -- list them for the commander and ask which one they mean (then call the tool again with `variant`); don't say the data is missing, and don't pick one yourself.
- If a tool result has a "caveat", tell the commander it along with the numbers -- never drop it.
- If a tool result has a "summary", base your answer on it: you may rephrase, but keep every number and unit exactly as written.
- Fuel, cargo, location and jump range change all the time: call the tool again for every question about them, and never reuse a number from an earlier reply.
- When asked for the "jump range" without more detail, give jump_range_now (the range with the current fuel and cargo); mention jump_range_max only as the empty-ship maximum, never as the current range.
- Once you have enough information to call a tool, call it immediately. Don't ask the commander to confirm an obvious detail (like a system name they already gave you) before proceeding.
- Keep answers short and conversational, like a ship's computer, not a report.
- Answer in at most three short sentences, in plain language. Never mention tools, tool results, 'found' or your own reasoning, and never explain abbreviations unless a tool result does.
- When the commander names a module type, class and rating (for example 'class 4 rating D sensors'), call simulate_module_swap right away. It finds the current module and the slot by itself: never ask the commander for them.
- If a tool reply says it needs a slot or a variant, ask the commander which one, listing the options the tool gave.
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
            "description": "Get the commander's rank, credits, current system and station. It does NOT know fuel, cargo or jump range: use get_current_loadout for those.",
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
            "description": "Get the commander's current ship: fuel on board, cargo carried, jump range right now (with that fuel and cargo), the empty-ship maximum jump range, mass, and every fitted module with its class, rating and engineering.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ship_info",
            "description": "Get a named ship's hull mass and slot layout: core slot sizes, weapon hardpoints by size, utility mounts and internal slots.",
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
            "description": "'What if I fit a different module' -- swaps one module in the commander's CURRENT ship for a hypothetical one and reports the real resulting mass, jump range now (current fuel and cargo) and empty-ship maximum jump range, and power changes, or whether it's compatible at all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_group": {"type": "string", "description": "e.g. 'frame shift drive', 'power plant', 'shield booster'"},
                    "module_class": {"type": "integer", "description": "Module class/size, 0-8 (utility mounts such as shield boosters are class 0)"},
                    "module_rating": {"type": "string", "description": "Module rating letter, e.g. 'A'"},
                    "blueprint_name": {"type": "string", "description": "Optional engineering blueprint to apply to the hypothetical module, e.g. 'Increased range'"},
                    "grade": {"type": "integer", "description": "Blueprint grade 1-5, required if blueprint_name is given"},
                    "slot": {"type": "string", "description": "Exact slot name taken from an earlier tool result, for example TinyHardpoint3 for a utility mount or Slot05_Size4 for an internal slot. Leave it out unless the tool asked for it."},
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


def _role(message):
    return message.get("role") if isinstance(message, dict) else getattr(message, "role", None)


def _is_empty(text):
    return not (text or "").strip()


def _estimated_tokens(messages):
    """A cautious estimate of the prompt size: the messages as JSON plus the
    tool schemas, which ride along with every request."""
    return (len(json.dumps(messages, default=str)) + len(json.dumps(TOOLS))) / CHARS_PER_TOKEN


def _trim_history(history):
    """Drop the OLDEST complete earlier turns (a user message and everything up
    to just before the next user message) until the prompt is under
    PROMPT_BUDGET of the window. Never the system message, never the current
    turn (the last user message and what follows it). In place, so the
    caller's history stays valid for the next question."""
    limit = PROMPT_BUDGET * NUM_CTX
    while _estimated_tokens(history) > limit:
        users = [i for i, m in enumerate(history) if _role(m) == "user"]
        if len(users) < 2:
            return
        del history[users[0]:users[1]]


def _chat(messages):
    return chat(
        model=MODEL, messages=messages, tools=TOOLS,
        options={"num_ctx": NUM_CTX},
        # thinking is off unless asked for: measured harmless on every model
        # tried, and a thinking model burned its output budget and returned
        # an empty answer with it on
        think=os.environ.get("ELLIE_THINK") == "1",
    )


def _variant_options(variants):
    """'Name (symbol)' for each of the first MAX_OPTIONS variants, each at most
    MAX_OPTION_CHARS long. The symbol is what a follow-up ("the SCO one") has to
    pass back, so it is never cut: a long name is shortened to make room, and
    when there is no room the symbol stands alone (the longest real one is 61
    characters, over the cap by itself). A variant with no name falls back to
    its mount word."""
    options = []
    for variant in variants[:MAX_OPTIONS]:
        symbol = str(variant.get("symbol") or "")
        label = variant.get("name") or variant.get("mount")
        if not label:
            options.append(symbol)
            continue
        option = f"{label} ({symbol})"
        if len(option) > MAX_OPTION_CHARS:
            room = MAX_OPTION_CHARS - len(f" ({symbol})")
            option = f"{str(label)[:room - 3]}... ({symbol})" if room >= 4 else symbol
        options.append(option)
    return options


def _compact_text(result):
    """What a finished turn's tool result is shrunk to: its summary; or, for a
    "which one?" reply (a `variants` list and no summary), the reason and ALL
    the options, because cutting the JSON at 400 characters could drop the
    tail of the list and a follow-up could no longer see every choice; else the
    first 400 characters of its JSON."""
    if isinstance(result, dict) and result.get("summary"):
        return str(result["summary"])
    if isinstance(result, dict) and result.get("variants"):
        reason = str(result.get("reason") or "")
        return (reason + " " if reason else "") + "Options: " + ", ".join(_variant_options(result["variants"]))
    return json.dumps(result)[:400]


def _compact_tool_results(history, produced):
    """Once a turn is over, shrink each tool result it produced (see
    _compact_text) and mark it as possibly stale: a follow-up like "and with
    grade 5 instead?" still has the numbers, but an old fuel or cargo figure is
    no longer presented as current and the history stops filling the window."""
    for message, result in produced:
        text = _compact_text(result)
        compact = {"role": "tool", "tool_name": message["tool_name"], "content": STALE_PREFIX + text}
        for i, kept in enumerate(history):
            if kept is message:
                history[i] = compact
                break


def ask_ellie(user_message, history):
    """history is a list of message dicts starting with the system
    prompt; mutated and returned so a caller can keep a conversation
    going across calls."""
    history.append({"role": "user", "content": user_message})
    current_question = history[-1]
    produced = []          # (tool message, result) for this turn, compacted when it ends

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            _trim_history(history)
            response = _chat(history)
            msg = response.message
            if not msg.tool_calls and (_is_empty(msg.content) or getattr(response, "done_reason", None) == "length"):
                # nothing usable (or cut off): one more try with only the
                # system message and this question, then a fixed apology
                system = [m for m in history[:1] if _role(m) == "system"]
                response = _chat(system + [current_question])
                msg = response.message
                if not msg.tool_calls and _is_empty(msg.content):
                    history.append({"role": "assistant", "content": EMPTY_ANSWER_FALLBACK})
                    return EMPTY_ANSWER_FALLBACK, history
            history.append(msg)

            if not msg.tool_calls:
                return msg.content, history

            for call in msg.tool_calls:
                result = call_tool(call.function.name, call.function.arguments)
                tool_message = {"role": "tool", "tool_name": call.function.name, "content": json.dumps(result)}
                history.append(tool_message)
                produced.append((tool_message, result))

        return "Sorry, I got stuck trying to answer that -- too many tool calls in a row.", history
    finally:
        _compact_tool_results(history, produced)


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
