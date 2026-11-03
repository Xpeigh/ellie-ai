# Ellie roadmap: an all-round, knowledgeable, learning commander's assistant

Status: design for review (2026-11-03). Umbrella document: it fixes the direction, the architecture and the order of work. Each sub-project below still gets its own spec, plan and build cycle before any code is written.

## 1. Purpose

Ellie is a local, offline assistant that runs beside Elite Dangerous on the player's own PC. Today she answers engineering, materials, distance and ship-loadout questions from the game's journal files and shipped data, through a 2-4B model, and never states a number she did not get from a tool.

The goal of this roadmap is the best possible experience for the Elite Dangerous community:

- **All-round.** Useful whatever the commander is doing (engineering, exploring, trading, mining, combat, colonising), not only engineering.
- **Knowledgeable.** Able to answer almost any question about the game, and honest when she cannot.
- **Learning.** Improves with use, from a database she keeps on the player's own machine.
- **Still private.** Nothing leaves the PC unless the player chooses to send it.

Success is measured, not felt: a community FAQ test set (section 9) where every answer is either correct and cited, or an honest "I don't know", with zero invented figures.

## 2. Decisions already made (with the commander, 2026-11-02)

1. First promise: **all-round commander's assistant** (breadth), on top of the engineering foundation.
2. Knowledge source: an **offline library** that ships with Ellie, plus **optional online add-ons** that are clearly labelled and off by default.
3. Data she collects: **local**, with a **reviewable export** (the player reads exactly what would be sent and sends it only by choice). No automatic upload.
4. Opinions and advice are presented as **facts first, then a clearly labelled "My take"**.
5. First wave after the ship loadout calculator: **Engineering & builds depth**, then **Library & Memory**.
6. Library scope: how-to and mechanics, where to find things, builds and loadout advice, lore, and the personal questions "What do you think about...?" and "What should I do next?".
7. Model policy: only **2-4B local models** (Ellie shares the PC with the game). `bonsai-2:4b` with thinking off is the provisional default; `granite4.1:3b` stays selectable. Re-test before any default is published.

## 3. Principles (apply to every layer)

1. **Never invent.** A number, name or rule appears in an answer only if it came from a tool result or a cited library entry in the same conversation. "I don't know" is a valid answer and is recorded.
2. **Local by default.** The runtime talks only to the local Ollama server and the local database. Online features are opt-in connectors.
3. **Cite what you retrieved.** A library answer names its pack and entry. Advice names the facts it rests on.
4. **Small-model budget.** About 8K tokens of context, a handful of capped passages per turn, under about 2.5 GB of graphics memory, answers that come back in a few seconds.
5. **Deterministic where possible.** Calculations, rankings and routing are code; the model chooses tools and phrases results.
6. **Measured, not assumed.** Behaviour is checked against a benchmark that runs the real chat loop on real model outputs.

## 4. Architecture: four layers and optional connectors

```
question -> model picks tool(s) -> results carry source tags
         -> answer = Facts (with sources) + optional "My take"
         -> interaction recorded locally (Memory)
```

- **Tools (exact).** Journal-fed live state and deterministic calculators. This exists today: blueprints, materials, engineers, distances, player state, ship info, current loadout, swap simulation. Rule: all numbers come from here (or from a cited library entry).
- **Library (cited knowledge).** Packs of curated, cited text. A new tool `search_library(query)` returns a few short passages, each with pack, entry title, source and date. No match returns "nothing found", never a guess.
- **Memory (her own local database).** Three stores, section 6.
- **Advisor.** Turns player state plus facts into "what should I do next?" and "what do you think about...?" answers, section 7.
- **Optional online connectors.** Separate modules (for example market prices, route plotting), off by default, each labelled in the answer ("from <service>, fetched <time>"), cached, and never sent personal data beyond what the query itself needs.

The existing rule for the chat loop stays: flat tool arguments (strings and integers), tool results include a short ready-made `summary`, and results never expose a figure without its unit and meaning.

## 5. The Library

**Purpose:** let a 2-4B model be knowledgeable by retrieval, not memory.

**Content model.** A *pack* is a folder with a `manifest.json` (name, version, licence, source URLs, retrieved/last-checked date, attribution text) and entries in Markdown with a short front-matter (title, tags, source, date). Packs are separate from the code repository's MIT licence so each keeps its own licence and attribution, and the community can contribute packs.

**Retrieval.** SQLite full-text search (FTS5, BM25 ranking), built at install time from the packs. No embedding model at first. Semantic search is added only if measured recall on the FAQ test set is poor (it would cost an extra download and extra GPU or CPU while gaming).

**Answering rules.**
- Return at most 3 passages and cap their total length (target at most about 1,200 tokens) so the 8K context holds history, tools and the library together.
- The model quotes or paraphrases only returned text; every library answer shows `[pack: entry]`.
- Passages are data, never instructions: they are fenced in the tool result and the prompt says so.
- No hit, or low confidence: "I don't have that in my library", and the question is recorded in the gap log so we know what to write next.

**Seed content (first packs).** "Where do I find..." (material sources, services, engineers, stations) and "How do I..." (mechanics, engineering, ranks, credits, exploration). Sources are chosen for reusable licences first (the commander's own published engineering sheet, Coriolis and EDCD data, the community wiki under its share-alike licence with attribution, the game's own journal documentation). Frontier's IP posture is the same one the project already declares in the README.

## 6. Memory: how she learns

Three stores, built in this order.

1. **Interaction and gap log (first).** Per turn: question, tools called, outcome flags (tool errors, `found: false`, empty or looping answers), the answer, and the player's thumbs up/down or correction. Local SQLite, under the player's control: view, delete, export. Purposes: a measuring stick, a to-do list for the Library (unanswered questions), and the source for the export.
   **Reviewable export.** A "share a gap report" action builds a file from the log, shows it to the player in full, lets them delete lines, and only then lets them save or send it. It strips the commander name, squadron and similar personal fields by default. Nothing is sent automatically.
2. **Experience replay (second).** Past question-to-tool-call examples that worked (tool succeeded, no thumbs-down) are stored; for a similar new question, the closest one or two are added to the prompt as hints. This is the practical way for a small model to "learn" without fine-tuning (which is out of scope locally). Capped to about 300 tokens.
3. **Player history database (third).** A normalised, queryable subset of the journal (sessions, jumps, trades, missions, scans, materials found and where), backfilled from all existing journals, plus explicit "remember that..." notes from the player. It answers "how far did I travel last week?" and "where did I find that material?" and feeds the Advisor.

**Privacy:** the player can see, export and delete everything Ellie has stored about them; default retention is "keep until the player deletes", stated plainly in the app.

## 7. The Advisor

Two kinds of question, one answer shape: **Facts**, then **My take**.

- **"What should I do next?"** A ranker over the player's state returns a short list of concrete options, each backed by a lookup: for example engineering materials short for a planned blueprint, unsold exploration data at risk, a colonisation delivery nearly done, an engineer one step from unlocking, low fuel before a long jump. The player's stated goal ("I'm exploring this week") is a remembered note that re-weights the list. The model phrases the ranked list; it does not invent options.
- **"What do you think about X?"** The Advisor assembles a comparison from tools and library entries (for a ship or module: the numbers; for a build: the swap results; for a plan: distance, fuel and range), then a short "My take", labelled as opinion, stating when it is community consensus rather than data and when she is unsure.

## 8. Order of work

Each item is its own brainstorm, spec, plan, build and review. Sizes are rough.

| # | Sub-project | Depends on | Done when |
|---|---|---|---|
| 0 | Finish and merge the ship loadout calculator (fix round 10b, default model switch, README requirements) | | merged with the user's say-so |
| 1 | **Engineering depth** (Tools): blueprint coverage for core modules, "what can I craft now", build shopping lists, engineer unlock tracker | 0 | "what does FSD Increased Range grade 5 cost?" is correct for every core module and pinned by tests; benchmark known-gap questions G1/G2 flip to real answers |
| 2 | **Interaction and gap log** with thumbs and reviewable export (Memory store 1) | 0 | every turn is logged locally; export preview works; the benchmark can replay logged questions |
| 3 | **Library engine plus first packs** | 2 | FAQ test passes its target, set in this sub-project's spec (proposed starting point: at least 80% of questions correct and cited, every other one an honest "I don't know", zero invented figures) |
| 4 | **Ship-build Phases B and C** (what fits this slot, build optimizer) | 0, 1 | agreed in its own spec |
| 5 | **Advisor v1** | 1, 2, 3 | "what next" and "what do you think" scenarios pass with sourced facts |
| 6 | **Experience replay** and **player history database** (Memory stores 2 and 3) | 2 | measured gain on the benchmark; history questions pass |

Later waves: live co-pilot and trackers (session stats, fuel/heat/hull alerts, missions, colonisation, exploration, mining, trading; the journal already carries all of these), effortless install (packaged desktop app, build pipeline, EDHM theme sync), online connectors, lore packs.

## 9. Quality gate

- **Benchmark.** The tool-use benchmark (55 questions across current ship, ship info, swaps, lookups, known gaps, no-tool chit-chat, phrasing variations; answer keys computed live from the tools) grows into the **community FAQ test**: add a questions-with-sources set for the Library and the Advisor. It runs before each release and whenever the model changes, on the real chat loop with real models.
- **Pass criteria per answer:** right tool or right retrieval; every number grounded; the key fact present or a legitimate clarifying question; known gaps answered with "I don't know"; no junk arguments, no wrong module, no unfinished answer.
- **Review sizing.** Small pieces get one implementer and one reviewer. A multi-agent review is used only for large, risky changes and only with the owner's agreement on cost (the last one used about 4M tokens).
- **Model policy.** New default models are chosen from this benchmark on the target hardware; the README states measured requirements.

## 10. Constraints and measured facts (2026-11)

- Model footprint (`bonsai-2:4b`): 1.6 GB download, about 1.7-1.9 GB loaded; about 2 s per answer on a modern GPU; CPU-only about 48 s for the first answer then about 5 s.
- Ollama's default 4,096-token window silently truncates the conversation after one loadout answer; the working window is 8,192 tokens, with history trimming (fix round 10b).
- Journal data in one commander's history that later waves can use: colonisation depot updates 1,367, missions 936 accepted, market snapshots 608, trades 659, exploration scans 13,048, mining 358, bounties 331, engineering crafts 257, carrier stats 583, hull damage 299, heat warnings 187, jumps 4,073.

## 11. Risks and open questions

- **Source licensing for packs** (share-alike wiki text, Frontier IP): keep packs separate with manifests; get each source's terms checked before shipping it.
- **Curation effort** is the real cost of the Library. The gap log is how we prioritise.
- **Retrieval quality and misquoting** by small models: measured on the FAQ set before claiming anything; escalate to semantic search only on evidence.
- **Prompt injection through library text and journal fields**: passages and names are fenced as data; reviewed in each sub-project's spec.
- **Context budget** with history, tools, library and replay hints together: each sub-project states its token budget.
- **Open:** how packs are distributed and updated (releases versus in-app download); how "My take" is styled in the chat window; whether thumbs feedback needs a design pass with the existing HUD-style UI.

## 12. Out of scope

Fine-tuning models locally; automatic upload of any user data; accounts or multi-user features; replacing Coriolis, EDSY or Inara; anything that edits the game's files or the EDHM mod's files.
