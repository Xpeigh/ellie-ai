"""
Bypasses the ollama Python package entirely -- talks to Ollama's REST API
directly with plain HTTP. If this ALSO fails to produce a tool_calls
field, the problem is server/model-side, not the Python client library.
If this WORKS while ellie_chat.py/test_tool_calling.py don't, the
problem is specifically the installed ollama pip package version.
"""
import json

import requests

payload = {
    "model": "phi4-mini",
    "messages": [{"role": "user", "content": "What is 7 plus 5?"}],
    "tools": [
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
    ],
    "stream": False,
}

response = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
response.raise_for_status()
result = response.json()

print(json.dumps(result, indent=2))
print()
if result.get("message", {}).get("tool_calls"):
    print("SUCCESS: raw HTTP got a proper tool call -- the problem is the Python ollama package version.")
else:
    print("STILL EMPTY/NO TOOL CALL: the problem is server/model-side, not the Python client.")
