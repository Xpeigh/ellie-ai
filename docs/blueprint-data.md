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
answers. Where both have an answer they agree on 656 of 668 rows (98%; checked 2026-11-07 with `python cross_check.py`,
which now covers every module). The 12 rows that differ are listed below; nothing was changed silently.

## What a cost means

- A blueprint's cost is **per roll**: what one craft uses. How many rolls a grade needs is not part of it.
- An experimental effect's cost is **per application**.
- If a source lists an upgrade with no materials, Ellie says "no materials are recorded", never "free".

## Known differences between the sources (2026-11-07)

For you to decide, nothing is changed silently. The sheet is used for these. The first five were known on
2026-11-06; the last seven appeared when the cross-check began covering shields and hull reinforcement. The
journal cannot settle them (those blueprints have not been crafted), so check them in the game.

| Module / upgrade / grade | Sheet | Coriolis |
|---|---|---|
| Mining Laser / Long Range / 2 | Sulphur, Heat Dispersion Plate, Modified Consumer Firmware, 5 Merc Coin | Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 3 | Chromium, Modified Consumer Firmware, Heat Exchangers, 5 Merc Coin | Focus Crystals, Modified Consumer Firmware, Sulphur |
| Mining Laser / Long Range / 4 | Modified Consumer Firmware, Conductive Polymers, Selenium, Heat Vanes, 10 Merc Coin | Conductive Polymers, Focus Crystals, Modified Consumer Firmware |
| Mining Laser / Long Range / 5 | Cracked Industrial Firmware, Thermic Alloys, Biotech Conductors, Proto Heat Radiators, 25 Merc Coin | Biotech Conductors, Cracked Industrial Firmware, Thermic Alloys |
| Chaff Launcher / Ammo Capacity / 1 | Mechanical Scrap | Mechanical Scrap, Niobium, Vanadium |
| Shield Generator / Reinforced / 1 | Nickel | Phosphorus |
| Shield Generator / Reinforced / 2 | Nickel, Shield Emitters | Conductive Components, Phosphorus |
| Shield Generator / Reinforced / 3 | Nickel, Shield Emitters, Tungsten | Conductive Components, Mechanical Components, Phosphorus |
| Shield Generator / Reinforced / 4 | Zinc, Tungsten, Molybdenum | Conductive Ceramics, Configurable Components, Manganese |
| Shield Generator / Reinforced / 5 | High Density Composites, Molybdenum, Technetium | Arsenic, Conductive Polymers, Improvised Components |
| Hull Reinforcement Package / Lightweight / 1-5 | Phosphorus; Salvaged Alloys + Manganese; ... (all five grades differ) | Iron; Conductive Components + Iron; ... (all five grades differ) |
| Shield Cell Bank / Rapid Charge / 4 | Chromium, Electrochemical Arrays, Thermic Alloys | Hybrid Capacitors, Precipitated Alloys, Sulphur |

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

## Corrections to Coriolis, with the evidence

Ellie applies a short list of corrections when the tables are built (`blueprint_corrections.json`; each entry
carries its evidence). They come from comparing Coriolis with the crafts in the owner's own journal on 2026-11-07
(`python verify_blueprints.py`), and were applied at the owner's request:

| What | Coriolis says | The game did | Evidence |
|---|---|---|---|
| Stripped Down on weapons | 3 Carbon, 5 Salvaged Alloys, 1 Tin | 5 Carbon, 5 Salvaged Alloys, 1 Tin | 2 crafts, a pulse laser |
| Drag Munitions | 5 Carbon, 5 Grid Resistors, 5 Molybdenum | 5 Carbon, 5 Grid Resistors, **2 Molybdenum** | **1 craft only**, a missile rack; re-check when another is made |
| Target Lock Breaker | material spelled "Adaptive Encyptors Capture" | "Adaptive Encryptors Capture" (as in `blueprints.json` and the game) | a held one never matched the typo |
| Missile racks and effects | no effects listed for missile racks | Drag Munitions and Thermal Cascade were applied to a missile rack | 1 craft each; without this Ellie would refuse them |

Everything else in the journal (all 48 blueprint-and-grade combinations and the other 7 effects, and every
blueprint on every module you crafted) agrees with Coriolis. If Coriolis fixes an entry, the correction either
becomes redundant (harmless) or no longer matches, and `python setup.py` says so ("corrections: N of M applied").

`python setup.py` re-applies the corrections on every run, so they reach an existing install even if you skip the
Coriolis update.

A draft report for Coriolis's maintainers is in [upstream-coriolis-report.md](upstream-coriolis-report.md).

## Plans and the rolls table

`plan_upgrade` answers "what do I need to take this module to grade N?". It multiplies the per-roll costs above by the rolls each grade takes. An engineer at **rank 5** makes every upgrade N rolls for grade N (grade 5 included, a rule). Below rank 5, grades 1 to 4 took about 4 rolls each in the owner's journal (grade 1 varies between 3 and 5, so Ellie says so). The rank used is the chosen engineer's current rank, taken from your journal; with nobody unlocked Ellie assumes the rank-5 engineer you will need and says so. The figures live in `engineering_rolls.json`; to check them against your own play:

```bash
python rolls_from_journal.py
```

prints the table your journal produces. Engineers per blueprint and grade come from Coriolis; Coriolis misspells "Felicity Farseer" in nine lists, which Ellie corrects.
