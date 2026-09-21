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

## Setup

```bash
pip install -r requirements.txt
ollama pull granite4.1:3b
```

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
