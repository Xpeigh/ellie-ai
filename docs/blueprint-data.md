# Where Ellie's engineering costs come from

Ellie never gives a material cost from memory. Every number comes from a table in her local
database, `elite-dangerous.db`, and the reply says which source it used.

## The two sources

1. **Coriolis** ([EDCD/coriolis-data](https://github.com/EDCD/coriolis-data)), community-maintained and
   cloned into `coriolis-data/` by `python setup.py`. It covers every engineerable module: core
   internals (frame shift drive, thrusters, power plant, power distributor, life support, sensors),
   shields, armour, weapons, utilities and optional internals. Everyone has this.
2. **Your own spreadsheet**, `Engineers.xlsx` (optional, not part of the repository). If you keep one, the
   rows you filled in are used first.

## Which one wins

Sheet first: when your spreadsheet has the module, upgrade and grade, that row is used. Otherwise Coriolis
answers. Where both have an answer they agree on 617 of 622 rows (checked 2026-11-06 with `python cross_check.py`).

## What a cost means

- A blueprint's cost is **per roll**: what one craft uses. How many rolls a grade needs is not part of it.
- An experimental effect's cost is **per application**.
- If a source lists an upgrade with no materials, Ellie says "no materials are recorded", never "free".

## Known differences between the sources (2026-11-06)

For you to decide, nothing is changed silently. The sheet is used for these.

| Module / upgrade / grade | Sheet | Coriolis |
|---|---|---|
| Mining Laser / Long Range / 2 | Sulphur, Heat Dispersion Plate, Modified Consumer Firmware, 5 Merc Coin | Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 3 | Chromium, Modified Consumer Firmware, Heat Exchangers, 5 Merc Coin | Focus Crystals, Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 4 | Modified Consumer Firmware, Conductive Polymers, Selenium, Heat Vanes, 10 Merc Coin | Conductive Polymers, Focus Crystals, Modified Consumer Firmware |
| Mining Laser / Long Range / 5 | Cracked Industrial Firmware, Thermic Alloys, Biotech Conductors, Proto Heat Radiators, 25 Merc Coin | Biotech Conductors, Cracked Industrial Firmware, Thermic Alloys |
| Chaff Launcher / Ammo Capacity / 1 | Mechanical Scrap | Mechanical Scrap, Niobium, Vanadium |

## Checking costs against your own crafts

Every craft you make is written to your journal with the materials it really used.

```bash
python verify_blueprints.py
```

compares each one with the cost Ellie gives and prints any difference, with when and on which module it
happened. A mismatch is worth reporting to Coriolis (open an issue on their GitHub repository with the
blueprint, what the game used, and how many crafts showed it).

## Updating after a game update

Coriolis's maintainers update their data after a patch. Then run:

```bash
python setup.py
```

and answer **yes** to "Update the Coriolis data (git pull) and re-ingest it?". It pulls the newest
Coriolis data, re-ingests the cost tables and rebuilds the ship, module and name tables from it. If you only
changed your spreadsheet, run `python ingest_excel.py` instead.

What is rebuilt: the reference tables (blueprint costs, effects, ship and module stats, which module takes which
blueprint and effect, module names). What is never touched: the live tables the journal watcher keeps (your
materials, ship, fuel and cargo).
