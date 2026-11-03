# Models Ellie has been tested with

Ellie talks to a small model running locally in [Ollama](https://ollama.com) and uses Ollama's native tool calling: the model picks one of Ellie's read-only functions and phrases what comes back. Numbers always come from those functions, never from the model's memory.

The default is **`bonsai-2:4b`**. To try another model, either set the `ELLIE_MODEL` environment variable (works for the chat window and the terminal chat) or pass the name to the terminal chat: `python ellie_chat.py <model>`.

## How they were tested

55 questions, each repeated 3 times, run through Ellie's real chat loop against a copy of a real commander's database (current ship, ship information, "what if I fit X" swaps, blueprint and material lookups, multi-turn follow-ups, questions Ellie has no data for, and small talk). Every answer is checked against what the tools actually returned: the right tool with the right arguments, every number grounded in a tool result, figures given with the right kind of unit (a range is not a mass), the key fact present or a fair clarifying question, honest "I don't have that" for missing data, and no junk arguments or unfinished answers. Thinking mode is off.

Treat the scores as a guide, not a ranking to the decimal: **repeating the same test moved a model by about 5 points**, so models within a few points of each other are tied. The scores come from one machine and one commander's data (measured 2026-11, Ollama 0.40.13), and the test scripts are not published yet.

## Results (shipped prompt, 3 runs each)

| Model | Download | Score | "What if I swap" questions | Typical answer | Notes |
|---|---|---|---|---|---|
| **`bonsai-2:4b`** (default) | 1.6 GB | 78% | 67% | about 100 characters | Tidiest answers; rarely invents anything. Apache 2.0. Needs Ollama 0.40.6 or newer. |
| `lfm3:1.7b-q4_K_M` | 1.1 GB | 78% | 56% | about 240 characters | Smallest of the strong models and as accurate overall, but wordier and occasionally thinks out loud. Best with the style rule only; the tool-use rules in the shipped prompt lowered its swap score in a side experiment. License: LFM Open License v1.0 (Apache 2.0 plus free commercial use only below US$10 million annual revenue; personal and community use is free). |
| `bonsai-2:2b` | 0.9 GB | 62% | 30% | about 180 characters | The low-memory option; noticeably weaker on swaps and follow-ups. |
| `granite4.1:3b` | 2.1 GB | 61% | 56% | about 190 characters | The previous default. Often mislabels units and rambles. |
| `qwen2.5:3b` | 1.9 GB | 59% | 22% | about 130 characters | Its 3B build is under the Qwen license, not Apache 2.0. |
| `llama3.2:3b-instruct-q4_K_M` | 2.0 GB | 59% | 15% | about 120 characters | Passes junk arguments (for example a slot called "None"). |
| `ministral-3:3b-instruct-2512-q4_K_M` | 3.0 GB | 52% | 7% | about 120 characters | Strong on current-ship questions, weak on the rest. |

Models that did **not** call tools reliably through Ollama's tool calling in our tests, and so are not recommended: `hermes3:3b`, `granite3.3:2b`, `cogito:3b`, `qwen3:1.7b`, `phi4-mini` (it wrote its tool calls as plain text).

## Other things we learned

- Ellie's prompt wording is tuned for the default model. Rules that lifted `bonsai-2:4b` from 77% to 84% in an earlier, larger test (call the swap tool right away; ask only when the tool asks) lowered `lfm3`'s swap score, so a different model may do better with a different prompt.
- Small models are happiest with short, labelled tool results; most of Ellie's recent work went into making her tool results shorter and clearer.
- Hardware needs are in the README's "System requirements".
