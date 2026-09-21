"""
Minimal, isolated tool-calling test -- no Ellie code involved at all.
Usage: python test_tool_calling.py [model_name]
Defaults to phi4-mini if no argument given.
"""
import sys

from ollama import chat

MODEL = sys.argv[1] if len(sys.argv) > 1 else "phi4-mini"


def add_two_numbers(a: int, b: int) -> int:
    """
    Add two numbers together.

    Args:
        a: The first number
        b: The second number
    """
    return int(a) + int(b)


tools = [
    {
        "type": "function",
        "function": {
            "name": "add_two_numbers",
            "description": "Add two numbers together",
            "parameters": {
                "type": "object",
                "required": ["a", "b"],
                "properties": {
                    "a": {"type": "integer", "description": "The first number"},
                    "b": {"type": "integer", "description": "The second number"},
                },
            },
        },
    }
]

messages = [{"role": "user", "content": "What is 7 plus 5?"}]
print(f"Testing model: {MODEL}\n")
response = chat(model=MODEL, messages=messages, tools=tools)

print("Raw message object:")
print(response.message)
print()
print("tool_calls:", response.message.tool_calls)
print("content:", repr(response.message.content))

if response.message.tool_calls:
    print()
    print("SUCCESS: the model correctly triggered a structured tool call.")
else:
    print()
    print("FAILURE: no tool_calls -- the model didn't use the structured mechanism at all.")
