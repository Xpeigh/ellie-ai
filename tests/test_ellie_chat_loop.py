"""Task 10b B1 / B5 / B6: the chat loop around the model -- context window,
thinking off, empty-answer handling, history trimming and compaction, and the
default model. A fake `chat` stands in for Ollama."""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ellie_chat as ec

PREFIX = "[earlier result, may be out of date] "
FALLBACK = "I couldn't produce an answer that time. Please ask again, or try a shorter question."


def answer(text, done_reason="stop"):
    return SimpleNamespace(
        message=SimpleNamespace(role="assistant", content=text, tool_calls=None), done_reason=done_reason)


def tool_call(name="get_ship_info", **arguments):
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(
        message=SimpleNamespace(role="assistant", content="", tool_calls=[call]), done_reason="stop")


class FakeChat:
    """Replays canned responses and keeps a snapshot of every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def fresh_history():
    return [{"role": "system", "content": "SYSTEM"}]


def roles(messages):
    return [m["role"] if isinstance(m, dict) else m.role for m in messages]


def content_of(message):
    return message["content"] if isinstance(message, dict) else message.content


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch):
    monkeypatch.delenv("ELLIE_THINK", raising=False)


# --- B5: window and thinking ----------------------------------------------------------

def test_every_chat_call_gets_the_context_window_and_thinking_off(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("done"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": False})
    ec.ask_ellie("hello", fresh_history())
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call["options"] == {"num_ctx": ec.NUM_CTX}
        assert call["think"] is False
        assert call["tools"] is ec.TOOLS


def test_the_default_context_window_is_8192():
    assert ec.NUM_CTX == 8192


@pytest.mark.parametrize("value, expected", [
    (None, 8192), ("", 8192), ("4096", 4096), (" 16384 ", 16384),
    ("abc", 8192), ("0", 8192), ("-512", 8192), ("4096.5", 8192), ("1e4", 8192),
])
def test_context_window_override_and_fallback(value, expected):
    assert ec._parse_num_ctx(value) == expected


def test_the_environment_sets_the_window_at_import(monkeypatch):
    monkeypatch.setenv("ELLIE_NUM_CTX", "4096")
    try:
        importlib.reload(ec)
        assert ec.NUM_CTX == 4096
        monkeypatch.setenv("ELLIE_NUM_CTX", "garbage")
        importlib.reload(ec)
        assert ec.NUM_CTX == 8192
    finally:
        monkeypatch.delenv("ELLIE_NUM_CTX", raising=False)
        importlib.reload(ec)
    assert ec.NUM_CTX == 8192


def test_the_changed_window_is_what_chat_receives(monkeypatch):
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "NUM_CTX", 4096)
    ec.ask_ellie("hi", fresh_history())
    assert fake.calls[0]["options"] == {"num_ctx": 4096}


def test_thinking_is_only_on_when_asked_for(monkeypatch):
    for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
        monkeypatch.setenv("ELLIE_THINK", value)
        fake = FakeChat(answer("ok"))
        monkeypatch.setattr(ec, "chat", fake)
        ec.ask_ellie("hi", fresh_history())
        assert fake.calls[0]["think"] is expected, value


# --- B5: empty answers ----------------------------------------------------------------

def test_an_empty_answer_is_retried_once_with_only_the_system_and_current_question(monkeypatch):
    fake = FakeChat(answer(""), answer("The second try works."))
    monkeypatch.setattr(ec, "chat", fake)
    history = fresh_history() + [
        {"role": "user", "content": "an earlier question"},
        {"role": "assistant", "content": "an earlier answer"},
    ]
    reply, history = ec.ask_ellie("current question", history)
    assert reply == "The second try works."
    assert len(fake.calls) == 2
    assert [m["content"] for m in fake.calls[1]["messages"]] == ["SYSTEM", "current question"]
    assert len(fake.calls[0]["messages"]) == 4                  # the first try saw everything
    assert content_of(history[-1]) == "The second try works."


def test_a_response_cut_off_by_the_length_limit_is_also_retried(monkeypatch):
    fake = FakeChat(answer("half an ans", done_reason="length"), answer("A whole answer."))
    monkeypatch.setattr(ec, "chat", fake)
    reply, _ = ec.ask_ellie("hi", fresh_history())
    assert reply == "A whole answer."
    assert len(fake.calls) == 2


def test_whitespace_only_counts_as_empty(monkeypatch):
    fake = FakeChat(answer("  \n "), answer("fine"))
    monkeypatch.setattr(ec, "chat", fake)
    assert ec.ask_ellie("hi", fresh_history())[0] == "fine"


def test_two_empty_answers_give_the_fixed_fallback_and_leave_valid_history(monkeypatch):
    fake = FakeChat(answer(""), answer(""), answer("Back to normal."))
    monkeypatch.setattr(ec, "chat", fake)
    reply, history = ec.ask_ellie("first", fresh_history())
    assert reply == FALLBACK == "I couldn't produce an answer that time. Please ask again, or try a shorter question."
    assert len(fake.calls) == 2                                  # retried ONCE, not forever
    assert roles(history) == ["system", "user", "assistant"]     # the empty message is not stored
    assert history[-1]["content"] == FALLBACK
    reply, history = ec.ask_ellie("second", history)             # and the next turn is fine
    assert reply == "Back to normal."
    assert roles(history) == ["system", "user", "assistant", "user", "assistant"]


def test_an_answer_that_makes_a_tool_call_is_not_empty(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("It is a Python."))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": True})
    assert ec.ask_ellie("what is a python", fresh_history())[0] == "It is a Python."
    assert len(fake.calls) == 2


# --- B5: trimming ---------------------------------------------------------------------

def big_turn(n, size=600):
    return [
        {"role": "user", "content": f"question {n} " + "q" * size},
        {"role": "assistant", "content": f"answer {n} " + "a" * size},
    ]


def test_old_turns_are_dropped_oldest_first_when_the_prompt_would_not_fit(monkeypatch):
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "TOOLS", [])
    monkeypatch.setattr(ec, "NUM_CTX", 1000)              # limit: 0.75 * 1000 = 750 tokens ~ 2,475 characters
    history = fresh_history() + big_turn(1, 450) + big_turn(2, 450) + big_turn(3, 450)
    limit = 0.75 * ec.NUM_CTX
    assert ec._estimated_tokens(history + [{"role": "user", "content": "current"}]) > limit      # 3 turns don't fit
    assert ec._estimated_tokens(history[:1] + history[3:] + [{"role": "user", "content": "current"}]) < limit   # 2 do
    reply, history = ec.ask_ellie("current", history)
    seen = [m["content"] for m in fake.calls[0]["messages"]]
    assert seen[0] == "SYSTEM"                                         # the system message always stays
    assert seen[-1] == "current"                                        # and so does the current turn
    assert not any(c.startswith("question 1") or c.startswith("answer 1") for c in seen)   # oldest gone first
    assert any(c.startswith("question 2") for c in seen) and any(c.startswith("answer 3") for c in seen)   # only as many as needed
    # turns are dropped whole (a user message AND its answers), never half
    assert roles(fake.calls[0]["messages"]) == ["system", "user", "assistant", "user", "assistant", "user"]
    # and the stored history is the trimmed one, still valid for the next turn
    assert content_of(history[0]) == "SYSTEM"
    assert not any(content_of(m).startswith(("question 1", "answer 1")) for m in history)
    assert roles(history)[1] == "user" and roles(history)[-1] == "assistant"


def test_nothing_is_dropped_while_the_prompt_fits(monkeypatch):
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "TOOLS", [])
    history = fresh_history() + big_turn(1, size=50) + big_turn(2, size=50)
    ec.ask_ellie("current", history)
    assert len(fake.calls[0]["messages"]) == 6


def test_the_current_turn_is_never_dropped_even_if_it_alone_is_too_big(monkeypatch):
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "TOOLS", [])
    monkeypatch.setattr(ec, "NUM_CTX", 1000)
    history = fresh_history() + big_turn(1)
    huge = "z" * 5000
    reply, history = ec.ask_ellie(huge, history)
    seen = [m["content"] for m in fake.calls[0]["messages"]]
    assert seen == ["SYSTEM", huge]                                      # the old turn went, the question stayed
    assert reply == "ok"


def test_the_tool_schemas_count_toward_the_estimate(monkeypatch):
    """The ~2,000 tokens of tool definitions are in every prompt, so with them
    counted an old turn that would otherwise fit has to go."""
    monkeypatch.setattr(ec, "NUM_CTX", 800)                               # limit: 600 tokens
    history = fresh_history() + big_turn(1, 500)
    current = {"role": "user", "content": "current"}
    monkeypatch.setattr(ec, "TOOLS", [])
    assert ec._estimated_tokens(history + [current]) < 600                # fits without any tool schemas
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    ec.ask_ellie("current", list(history))
    assert [m["content"] for m in fake.calls[0]["messages"]][0:2] == ["SYSTEM", "question 1 " + "q" * 500]   # nothing dropped

    monkeypatch.setattr(ec, "TOOLS", [{"pad": "t" * 1000}])               # ~300 tokens of schemas
    assert ec._estimated_tokens(history + [current]) > 600                # now it does not
    fake = FakeChat(answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    ec.ask_ellie("current", list(history))
    assert [m["content"] for m in fake.calls[0]["messages"]] == ["SYSTEM", "current"]


def test_trimming_happens_before_every_round_of_a_long_turn(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "TOOLS", [])
    monkeypatch.setattr(ec, "NUM_CTX", 1000)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": True, "blob": "b" * 2000})
    history = fresh_history() + big_turn(1, size=300)
    ec.ask_ellie("current", history)
    second_round = [m["content"] if isinstance(m, dict) else m.content for m in fake.calls[1]["messages"]]
    assert second_round[0] == "SYSTEM" and second_round[1] == "current"   # the earlier turn was dropped by round 2
    assert any("blob" in c for c in second_round)                          # the current tool result is intact


# --- B1: compaction of old tool results -------------------------------------------------------

def test_a_tool_result_with_a_summary_is_stored_as_prefix_plus_summary(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("Here you go."))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": True, "summary": "Python: 55 t.", "big": "x" * 3000})
    reply, history = ec.ask_ellie("tell me about the python", fresh_history())
    tool_messages = [m for m in history if isinstance(m, dict) and m["role"] == "tool"]
    assert tool_messages == [{"role": "tool", "tool_name": "get_ship_info", "content": PREFIX + "Python: 55 t."}]


def test_the_turn_in_progress_keeps_the_full_result_until_it_ends(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("Here you go."))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": True, "summary": "Python: 55 t.", "extra": 12345})
    ec.ask_ellie("tell me about the python", fresh_history())
    second_round_tool = [m for m in fake.calls[1]["messages"] if isinstance(m, dict) and m["role"] == "tool"][0]
    assert json.loads(second_round_tool["content"]) == {"found": True, "summary": "Python: 55 t.", "extra": 12345}


def test_a_result_without_a_summary_is_cut_to_400_characters_then_prefixed(monkeypatch):
    result = {"found": True, "rows": ["r" * 50 for _ in range(30)]}
    fake = FakeChat(tool_call(ship_name="Python"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: result)
    _, history = ec.ask_ellie("q", fresh_history())
    stored = [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"]
    assert stored == PREFIX + json.dumps(result)[:400]
    assert len(stored) == len(PREFIX) + 400


def test_a_short_result_without_a_summary_is_kept_whole(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": False})
    _, history = ec.ask_ellie("q", fresh_history())
    assert [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"] == PREFIX + '{"found": false}'


def test_user_and_assistant_messages_stay_and_a_second_turn_runs(monkeypatch):
    fake = FakeChat(tool_call(ship_name="Python"), answer("First answer."), answer("Second answer."))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"found": True, "summary": "S1."})
    reply, history = ec.ask_ellie("first question", fresh_history())
    assert reply == "First answer."
    reply, history = ec.ask_ellie("and with grade 5 instead?", history)
    assert reply == "Second answer."
    contents = [m["content"] if isinstance(m, dict) else m.content for m in history]
    assert contents.count("first question") == 1 and "First answer." in contents and "Second answer." in contents
    # the second turn's model call saw the compacted first-turn result, summary included
    assert PREFIX + "S1." in [m["content"] for m in fake.calls[2]["messages"] if isinstance(m, dict) and m["role"] == "tool"]


def test_each_turns_results_are_compacted_when_that_turn_ends(monkeypatch):
    fake = FakeChat(tool_call(ship_name="A"), answer("a"), tool_call(ship_name="B"), answer("b"))
    monkeypatch.setattr(ec, "chat", fake)
    results = iter([{"summary": "ONE"}, {"summary": "TWO"}])
    monkeypatch.setattr(ec, "call_tool", lambda name, args: next(results))
    _, history = ec.ask_ellie("q1", fresh_history())
    _, history = ec.ask_ellie("q2", history)
    assert [m["content"] for m in history if isinstance(m, dict) and m["role"] == "tool"] == [PREFIX + "ONE", PREFIX + "TWO"]


def test_results_are_compacted_even_when_the_turn_ends_in_the_fallback(monkeypatch):
    fake = FakeChat(tool_call(ship_name="A"), answer(""), answer(""))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"summary": "KEPT"})
    reply, history = ec.ask_ellie("q", fresh_history())
    assert reply == FALLBACK
    assert [m["content"] for m in history if isinstance(m, dict) and m["role"] == "tool"] == [PREFIX + "KEPT"]


def test_results_are_compacted_when_the_model_loops_on_tool_calls(monkeypatch):
    fake = FakeChat(*[tool_call(ship_name="A") for _ in range(ec.MAX_TOOL_ROUNDS)])
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"summary": "LOOP"})
    reply, history = ec.ask_ellie("q", fresh_history())
    assert reply.startswith("Sorry, I got stuck")
    tools = [m["content"] for m in history if isinstance(m, dict) and m["role"] == "tool"]
    assert tools == [PREFIX + "LOOP"] * ec.MAX_TOOL_ROUNDS


# --- B6: the default model ----------------------------------------------------------------------------

def test_the_default_model_is_bonsai():
    assert ec.DEFAULT_MODEL == "bonsai-2:4b"
    assert ec.choose_model((), {}) == "bonsai-2:4b"
    assert ec.choose_model(["ellie_chat.py"], {}) == "bonsai-2:4b"


def test_the_model_can_be_chosen_by_environment_or_command_line():
    assert ec.choose_model((), {"ELLIE_MODEL": "lfm3:1.7b-q4_K_M"}) == "lfm3:1.7b-q4_K_M"
    assert ec.choose_model(["ellie_chat.py"], {"ELLIE_MODEL": "  lfm3:1.7b-q4_K_M  "}) == "lfm3:1.7b-q4_K_M"
    assert ec.choose_model(["ellie_chat.py", "granite4.1:3b"], {"ELLIE_MODEL": "lfm3:1.7b-q4_K_M"}) == "granite4.1:3b"   # the command line wins
    assert ec.choose_model(["ellie_chat.py", "   "], {"ELLIE_MODEL": "lfm3:1.7b-q4_K_M"}) == "lfm3:1.7b-q4_K_M"          # blank is ignored
    assert ec.choose_model((), {"ELLIE_MODEL": "   "}) == "bonsai-2:4b"


def test_importing_never_reads_the_command_line_but_does_read_the_environment(monkeypatch):
    """The chat window imports this module; whatever arguments launched the app must not become the model name."""
    original_argv = sys.argv
    try:
        monkeypatch.delenv("ELLIE_MODEL", raising=False)
        monkeypatch.setattr(sys, "argv", ["app.py", "--port", "9999"])
        importlib.reload(ec)
        assert ec.MODEL == "bonsai-2:4b"
        monkeypatch.setenv("ELLIE_MODEL", "lfm3:1.7b-q4_K_M")
        importlib.reload(ec)
        assert ec.MODEL == "lfm3:1.7b-q4_K_M"
    finally:
        monkeypatch.setattr(sys, "argv", original_argv)
        monkeypatch.delenv("ELLIE_MODEL", raising=False)
        importlib.reload(ec)


def test_the_terminal_chat_reads_the_model_from_the_command_line(monkeypatch):
    answers = iter(["quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sys, "argv", ["ellie_chat.py", "granite4.1:3b"])
    original = ec.MODEL
    try:
        ec.main()
        assert ec.MODEL == "granite4.1:3b"
    finally:
        ec.MODEL = original


def test_ollama_is_pinned_to_a_version_that_accepts_think_and_options():
    lines = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text().split()
    assert "ollama>=0.6.0" in lines


# --- Task 10c: a "which variant?" reply is compacted to its options, not cut at 400 characters ---------------

def variants_result(count=4, name_length=20):
    variants = [
        {"symbol": f"int_hyperdrive_variant{i}_size6_class5", "name": ("N" * name_length) + str(i), "mount": None}
        for i in range(1, count + 1)
    ]
    return {
        "found": False,
        "reason": f"{count} different modules are frame shift drive class 6 rating A -- specify which with `variant`",
        "variants": variants,
    }


def test_a_variants_reply_keeps_every_option_when_compacted(monkeypatch):
    result = variants_result(4)
    assert len(json.dumps(result)) > 400 and "variant4" not in json.dumps(result)[:400]     # the old cut lost the tail
    fake = FakeChat(tool_call(name="simulate_module_swap", module_group="frame shift drive"), answer("Which one?"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: result)
    _, history = ec.ask_ellie("fit a class 6 A drive", fresh_history())
    stored = [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"]
    assert stored == PREFIX + result["reason"] + " Options: " + ", ".join(
        f"{v['name']} ({v['symbol']})" for v in result["variants"])
    for variant in result["variants"]:
        assert variant["symbol"] in stored


def test_a_follow_up_after_a_variants_reply_still_runs_and_sees_the_options(monkeypatch):
    result = variants_result(5, name_length=30)
    assert "variant5" not in json.dumps(result)[:400]               # the old cut would have lost the last options
    fake = FakeChat(tool_call(name="simulate_module_swap"), answer("Which one?"), answer("The SCO one it is."))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: result)
    _, history = ec.ask_ellie("fit a class 6 A drive", fresh_history())
    reply, history = ec.ask_ellie("the SCO one", history)
    assert reply == "The SCO one it is."
    seen = [m["content"] for m in fake.calls[2]["messages"] if isinstance(m, dict) and m["role"] == "tool"][0]
    assert all(v["symbol"] in seen for v in result["variants"])


def test_at_most_six_options_are_kept(monkeypatch):
    result = variants_result(8, name_length=5)
    fake = FakeChat(tool_call(name="simulate_module_swap"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: result)
    _, history = ec.ask_ellie("q", fresh_history())
    stored = [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"]
    assert stored.count("int_hyperdrive_variant") == 6
    assert "variant6_" in stored and "variant7_" not in stored


def test_each_option_is_at_most_60_characters_and_keeps_its_symbol(monkeypatch):
    long_name = {"symbol": "int_hyperdrive_a_size6_class5", "name": "A very long module name " * 4, "mount": None}
    fake = FakeChat(tool_call(name="simulate_module_swap"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"reason": "R", "variants": [long_name]})
    _, history = ec.ask_ellie("q", fresh_history())
    stored = [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"]
    entry = stored[len(PREFIX + "R Options: "):]
    assert len(entry) <= 60 and entry.endswith("(int_hyperdrive_a_size6_class5)")
    assert entry == "A very long module name A... (int_hyperdrive_a_size6_class5)"
    assert len(entry) == 60                                          # the label gave up exactly the room it had to


def test_the_option_label_falls_back_to_the_mount_then_to_the_symbol_alone():
    options = ec._variant_options([
        {"symbol": "hpt_beamlaser_fixed_small", "name": None, "mount": "fixed"},
        {"symbol": "hpt_beamlaser_gimbal_small", "name": None, "mount": None},
        {"symbol": "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii", "name": "Frame Shift Drive Mk II (SCO)", "mount": None},
    ])
    assert options == [
        "fixed (hpt_beamlaser_fixed_small)",
        "hpt_beamlaser_gimbal_small",
        # the longest real symbol is 61 characters: the symbol is what a follow-up needs, so it stays whole
        "int_hyperdrive_overcharge_size8_class5_overchargebooster_mkii",
    ]


def test_a_variants_reply_with_no_reason_still_compacts(monkeypatch):
    fake = FakeChat(tool_call(name="simulate_module_swap"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    monkeypatch.setattr(ec, "call_tool", lambda name, args: {"variants": [{"symbol": "s1", "name": "One", "mount": None}]})
    _, history = ec.ask_ellie("q", fresh_history())
    assert [m for m in history if isinstance(m, dict) and m["role"] == "tool"][0]["content"] == PREFIX + "Options: One (s1)"


def test_a_summary_still_wins_over_variants_and_other_results_keep_the_400_cut(monkeypatch):
    fake = FakeChat(tool_call(name="a"), answer("ok"), tool_call(name="b"), answer("ok"), tool_call(name="c"), answer("ok"))
    monkeypatch.setattr(ec, "chat", fake)
    results = iter([
        {"summary": "THE SUMMARY", "variants": [{"symbol": "s", "name": "n", "mount": None}]},
        {"variants": []},                                    # empty list: nothing to list
        {"rows": ["x" * 50 for _ in range(30)]},
    ])
    monkeypatch.setattr(ec, "call_tool", lambda name, args: next(results))
    history = fresh_history()
    for question in ("q1", "q2", "q3"):
        _, history = ec.ask_ellie(question, history)
    stored = [m["content"] for m in history if isinstance(m, dict) and m["role"] == "tool"]
    assert stored[0] == PREFIX + "THE SUMMARY"
    assert stored[1] == PREFIX + '{"variants": []}'
    assert stored[2] == PREFIX + json.dumps({"rows": ["x" * 50 for _ in range(30)]})[:400]
