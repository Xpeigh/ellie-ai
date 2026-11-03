# Ellie

A local, offline AI assistant for Elite Dangerous engineering. Ask it what a blueprint costs, what
you're missing to apply it, how many of a material you have, where an engineer is, or how far away a
system is. It runs entirely on your machine through [Ollama](https://ollama.com).

> **Status: early / work in progress.** The data layer, journal watcher and terminal chat work. A
> setup script and a desktop chat window are planned but not built yet, so setup is manual for now.

## How it works

Ellie never answers a number from the model's memory. Small local models get specifics wrong, so the
model only does two things: pick one of a handful of read-only functions, and phrase what comes back.

```
Elite journal ──► journal_watcher.py ─┐
Engineers.xlsx ─► ingest_excel.py ────┤
Coriolis data ──► ingest_coriolis.py ─┼─► elite-dangerous.db ──► query_functions.py ──► ellie_chat.py ◄─► Ollama
EDSM dumps ─────► ingest_edsm.py ─────┘      (SQLite)             (the 7 tools)         (chat loop)
```

The tools: `get_blueprint`, `get_experimental_effect`, `missing_materials`, `get_engineer`,
`get_material_count`, `player_state`, `distance_to_system`. Your materials, ranks, ship and location
are read from the game's own journal files, so nothing is sent anywhere and no account is needed.

## Requirements

- Windows (the journal location is currently hardcoded to `Saved Games\Frontier Developments\Elite Dangerous`)
- Python 3.10 or newer (developed on 3.13)
- [Ollama](https://ollama.com) installed and running
- Git
- Disk space: the EDSM dumps are about 3.3 GB compressed, and the finished database is a few hundred MB

### System requirements

On top of Elite Dangerous itself (measured on an 8-core i7-11700K with an RTX 5070 Ti, Ollama 0.40.13):

| | Minimum | Recommended |
|---|---|---|
| Graphics card | 8 GB VRAM | 12 GB VRAM or more |
| RAM | 16 GB | 16 GB (32 GB if you run other tools) |
| CPU | anything that runs the game | anything that runs the game |
| Free storage | about 6 GB | about 8 GB, on an SSD |

- The model (`bonsai-2:4b`) is a 1.6 GB download and about 1.7-1.9 GB once loaded.
- Expect about 2 s per answer on a modern GPU.
- With no usable GPU Ellie still works, but the first answer takes about 48 s on an 8-core desktop CPU and
  later answers about 5 s, and she competes with the game for the CPU.
- She needs Ollama 0.40.6 or newer.
- Ollama unloads the model after 5 minutes idle. Set its keep-alive (`OLLAMA_KEEP_ALIVE`, for example `30m`)
  to keep it ready.
- The EDSM dumps (about 3.3 GB compressed) are only needed for the import, and the finished database is
  about 0.35 GB.

Caveats: this was tested on NVIDIA only. The 8 GB VRAM minimum is derived (the game's own VRAM use was not
measured), so see the game's store page for its own requirements.

## Setup

```bash
pip install -r requirements.txt
ollama pull bonsai-2:4b
```

`bonsai-2:4b` is the default model. To try another one, pull it from Ollama and either set the `ELLIE_MODEL`
environment variable (this works for the chat window too) or start the terminal chat with
`python ellie_chat.py <model>`. [docs/models.md](docs/models.md) lists the models we tested, with scores and licenses;
`lfm3:1.7b-q4_K_M` (1.1 GB) is the lightweight alternative.

**1. Coriolis data** (blueprint and experimental-effect materials):

```bash
git clone https://github.com/EDCD/coriolis-data.git
python ingest_coriolis.py
```

**2. EDSM data** (system coordinates and stations, used for distances). EDSM blocks scripted downloads,
so download these two files in your browser and put them in a folder named `edsm_dumps` next to the
scripts:

- https://www.edsm.net/dump/systemsPopulated.json.gz
- https://www.edsm.net/dump/stations.json.gz

```bash
python ingest_edsm.py
```

**3. Engineers spreadsheet** (optional, but needed for engineer lookups). `ingest_excel.py` reads
`Engineers.xlsx` from the same folder. Without it, blueprint lookups still work from Coriolis data,
but `get_engineer` has no data to read.

```bash
python ingest_excel.py
```

**4. Live game state.** Run this while you play (or once before chatting, to load your current state).
It replays your latest journal, then keeps following it:

```bash
python journal_watcher.py
```

**5. Chat** (in a second terminal):

```bash
python ellie_chat.py
```

The ingest scripts are safe to re-run at any time. They only touch their own tables.

## Running the tests

```bash
pip install -r requirements.txt
python setup.py          # fetches coriolis-data, which the ship and module tests read
python -m pytest tests
```

The tests use temporary databases and never touch `elite-dangerous.db`. The ones that need the
Coriolis data skip, saying so, until `python setup.py` has fetched it; the rest run without it.

## Limitations

- `distance_to_system` only knows populated systems for the destination (the EDSM dump excludes
  systems nobody has settled). Your current system can be anywhere, as its position comes from the journal.
- Blueprint costs come from your spreadsheet first, then fall back to Coriolis's broader
  per-category data.
- Ship build recommendations (mass, slots, jump range) are not implemented.

## Disclaimer

This is an unofficial fan project. It is not affiliated with or endorsed by Frontier Developments plc.
Elite Dangerous and related names are trademarks of Frontier Developments plc. Blueprint and module
data come from [EDCD/coriolis-data](https://github.com/EDCD/coriolis-data) and system/station data from
[EDSM](https://www.edsm.net); both are Frontier Developments intellectual property used under the same
non-commercial, community-tool arrangement as Coriolis, EDSM, EDMC and similar tools. That data is
downloaded by you at setup and is not distributed in this repository.

## License

Code: [MIT](LICENSE).
