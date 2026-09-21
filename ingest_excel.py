"""
Ingest Engineers.xlsx into a normalized SQLite database.

This is the "data layer" for the ED engineering assistant. phi4-mini never
generates material counts itself -- it calls functions that query the
tables this script builds, and only phrases what comes back.

Design notes (why this isn't a single generic parser):
  The workbook has THREE distinct row shapes, not one:
    A) Engineer directories (Ship Module Engineers / Personal Equipment
       Engineers) -- merged cells group multiple unlock rows under one
       engineer.
    B) Graded blueprints (Hardpoint/Utility/Optional Internal/Core
       Internal/Armour Costs) -- Module Name + Upgrade, then a
       (materials, modifiers) pair per grade 1-5. Armour Costs omits
       "Module Name" since armour upgrades aren't per-module.
    C) Ungraded costs (Experimental effect sheets, On Foot Modification
       Costs) -- Module + Effect/Upgrade + a single Cost + Modifier,
       no grade columns at all.
    D) On Foot Suits and Weapons Costs -- Type/Name + a single credit
       figure per grade column (a purchase cost, not a material list).
  Sheets that are still just a header row (nothing filled in yet) are
  skipped automatically -- rerun this script after you fill more of the
  workbook in and those rows will show up with no code changes needed.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import openpyxl

SCRIPT_DIR = Path(__file__).resolve().parent
XLSX_PATH = sys.argv[1] if len(sys.argv) > 1 else str(SCRIPT_DIR / "Engineers.xlsx")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else str(SCRIPT_DIR / "elite-dangerous.db")

ENGINEER_SHEETS = {
    "Ship Module Engineers": "ship_module",
    "Personal Equipment Engineers": "personal_equipment",
}

# family B: Module Name(optional) | Upgrade | (Grade N (per roll) | Grade N Modifiers) x5
GRADED_BLUEPRINT_SHEETS = {
    "Ship Hardpoint Costs": "hardpoint",
    "Ship Utility Costs": "utility",
    "Ship Optional Internal Costs": "optional_internal",
    "Ship Core Internal Costs": "core_internal",
    "Ship Armour Costs": "armour",
}

# family C: Module | Effect/Upgrade | Cost | Modifier(s) -- single row, no grade
UNGRADED_COST_SHEETS = {
    "Hardpoint Experimental Costs": "hardpoint_experimental",
    "Utility Experimental Costs": "utility_experimental",
    "Op. Int. Experimental Costs": "optional_internal_experimental",
    "Co. Int. Experimental Costs": "core_internal_experimental",
    "Armour Experimental Costs": "armour_experimental",
    "On Foot Modification Costs": "on_foot_modification",
}

# family D: Type | Name | Grade 1..5 (credits, not materials)
SUIT_WEAPON_SHEET = "On Foot Suits and Weapons Costs"

MATERIAL_LINE_RE = re.compile(r"^([\d,]+)\s+(.+)$")


def unmerge_fill(ws):
    """Return a 2D list of cell values with every merged range's value
    copied into all of its member cells, so downstream code never has to
    special-case merges. openpyxl otherwise leaves every cell but the
    merge's top-left as None."""
    max_row, max_col = ws.max_row, ws.max_column
    grid = [[ws.cell(r, c).value for c in range(1, max_col + 1)] for r in range(1, max_row + 1)]
    for merged_range in ws.merged_cells.ranges:
        min_r, min_c, max_r, max_c = (
            merged_range.min_row,
            merged_range.min_col,
            merged_range.max_row,
            merged_range.max_col,
        )
        anchor_value = grid[min_r - 1][min_c - 1]
        for r in range(min_r, max_r + 1):
            for c in range(min_c, max_c + 1):
                grid[r - 1][c - 1] = anchor_value
    return grid


def rows_as_dicts(grid):
    """First row is the header; yield the rest as header->value dicts."""
    headers = [h if h is not None else f"col_{i}" for i, h in enumerate(grid[0])]
    for row in grid[1:]:
        if all(v is None for v in row):
            continue
        yield dict(zip(headers, row))


def parse_materials(cell_text):
    """'1 Sulphur\\n1 Heat Dispersion Plate\\n5 Merc Coin' ->
    [{"qty": 1, "item": "Sulphur"}, ..., {"qty": 5, "item": "Merc Coin"}]
    Returns [] for empty/placeholder ('-') cells."""
    if not cell_text or str(cell_text).strip() in ("-", ""):
        return []
    out = []
    for line in str(cell_text).split("\n"):
        line = line.strip()
        if not line or line == "-":
            continue
        m = MATERIAL_LINE_RE.match(line)
        if m:
            qty_str, item = m.groups()
            qty = qty_str.replace(",", "")
            try:
                qty = int(qty)
            except ValueError:
                pass
            out.append({"qty": qty, "item": item.strip()})
        else:
            # doesn't match "<qty> <name>" (e.g. a credits line with odd
            # formatting) -- keep it verbatim rather than silently drop it
            out.append({"qty": None, "item": line})
    return out


def build_schema(conn):
    # Only drop/recreate the tables THIS script owns. The db is shared with
    # EDSM (edsm_systems/edsm_stations) and the journal watcher
    # (commander_status/materials_inventory/engineer_progress) -- wiping
    # the whole file here would destroy all of that.
    conn.executescript(
        """
        DROP TABLE IF EXISTS engineers;
        DROP TABLE IF EXISTS engineer_unlocks;
        DROP TABLE IF EXISTS personal_upgrades;
        DROP TABLE IF EXISTS graded_blueprints;
        DROP TABLE IF EXISTS ungraded_costs;
        DROP TABLE IF EXISTS suit_weapon_costs;
        CREATE TABLE engineers (
            name TEXT, engineer_type TEXT, region TEXT, station TEXT,
            system TEXT, prerequisites TEXT, notes TEXT
        );
        CREATE TABLE engineer_unlocks (
            engineer_name TEXT, grade INTEGER, item TEXT
        );
        CREATE TABLE personal_upgrades (
            engineer_name TEXT, upgrade TEXT
        );
        CREATE TABLE graded_blueprints (
            category TEXT, module_name TEXT, upgrade TEXT,
            grade INTEGER, materials_json TEXT, modifiers_text TEXT
        );
        CREATE TABLE ungraded_costs (
            category TEXT, module_name TEXT, effect TEXT,
            materials_json TEXT, modifiers_text TEXT
        );
        CREATE TABLE suit_weapon_costs (
            item_type TEXT, name TEXT, grade INTEGER, cost_text TEXT
        );
        """
    )


def split_station_system(text):
    """The sheet's 'System' column is actually 'Station Name, System Name'
    combined (e.g. 'Turner Metallics Inc., Alioth'). Split on the last
    comma -- reliable because ED system names never contain commas."""
    if not text:
        return None, None
    text = str(text).strip()
    if "," in text:
        station, _, system = text.rpartition(",")
        return station.strip(), system.strip()
    return None, text  # some rows only give the system name


def ingest_engineer_sheet(conn, ws, engineer_type):
    grid = unmerge_fill(ws)
    seen_engineers = set()  # merge-fill repeats Name/Location/etc. on every
    # unlock sub-row for the same engineer -- insert the engineers row once
    for row in rows_as_dicts(grid):
        name = row.get("Name")
        if not name:
            continue
        # NB: the sheet's own header naming is misleading -- "Location"
        # actually holds the broad region (Core Systems / Colonia Region),
        # and "System" holds "Station, System" combined.
        region = row.get("Location")
        station, system = split_station_system(row.get("System"))
        prereq = row.get("Prerequisite(s)")
        notes = None
        if engineer_type == "ship_module":
            for i in range(1, 6):
                key = f"Grade {i}"
                val = row.get(key)
                if val and str(val).strip() not in ("-", ""):
                    conn.execute(
                        "INSERT INTO engineer_unlocks VALUES (?,?,?)", (name, i, str(val).strip())
                    )
                    # trailing unnamed column sometimes carries a note
            for k, v in row.items():
                if k and k.startswith("col_") and v:
                    notes = str(v).strip()
        else:  # personal_equipment
            referral = row.get("Referrals")
            upgrade = row.get("Upgrades")
            if upgrade and str(upgrade).strip() not in ("-", ""):
                conn.execute(
                    "INSERT INTO personal_upgrades VALUES (?,?)", (name, str(upgrade).strip())
                )
            if referral:
                notes = f"Referral: {referral}"
            for k, v in row.items():
                if k and k.startswith("col_") and v:
                    notes = (notes + " | " if notes else "") + str(v).strip()
        if name not in seen_engineers:
            seen_engineers.add(name)
            conn.execute(
                "INSERT INTO engineers VALUES (?,?,?,?,?,?,?)",
                (name, engineer_type, region, station, system, prereq, notes),
            )
    conn.commit()


def ingest_graded_blueprint_sheet(conn, ws, category):
    grid = unmerge_fill(ws)
    headers = grid[0]
    has_module_col = "Module Name" in headers
    for row in rows_as_dicts(grid):
        module_name = row.get("Module Name") if has_module_col else None
        upgrade = row.get("Upgrade")
        if not upgrade:
            continue
        any_grade_data = False
        for i in range(1, 6):
            mat_col = f"Grade {i} (per roll)"
            mod_col = f"Grade {i} Modifiers"
            materials = parse_materials(row.get(mat_col))
            modifiers = row.get(mod_col)
            modifiers = None if not modifiers or str(modifiers).strip() == "-" else str(modifiers).strip()
            if materials or modifiers:
                any_grade_data = True
                conn.execute(
                    "INSERT INTO graded_blueprints VALUES (?,?,?,?,?,?)",
                    (category, module_name, upgrade, i, json.dumps(materials), modifiers),
                )
        if not any_grade_data:
            continue
    conn.commit()


def ingest_ungraded_cost_sheet(conn, ws, category):
    grid = unmerge_fill(ws)
    for row in rows_as_dicts(grid):
        module_name = row.get("Module")
        effect = row.get("Effect") or row.get("Upgrade")
        if not effect or str(effect).strip() == "-":
            continue
        cost = row.get("Cost")
        materials = parse_materials(cost)
        modifiers = row.get("Modifier") or row.get("Modifiers")
        modifiers = None if not modifiers or str(modifiers).strip() == "-" else str(modifiers).strip()
        if not materials and not modifiers:
            continue
        conn.execute(
            "INSERT INTO ungraded_costs VALUES (?,?,?,?,?)",
            (category, module_name, str(effect).strip(), json.dumps(materials), modifiers),
        )
    conn.commit()


def ingest_suit_weapon_sheet(conn, ws):
    grid = unmerge_fill(ws)
    for row in rows_as_dicts(grid):
        name = row.get("Name")
        if not name:
            continue
        item_type = row.get("Type")
        for i in range(1, 6):
            val = row.get(f"Grade {i}")
            if val and str(val).strip() not in ("-", ""):
                conn.execute(
                    "INSERT INTO suit_weapon_costs VALUES (?,?,?,?)",
                    (item_type, name, i, str(val).strip()),
                )
    conn.commit()


def main():
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)

    sheet_names = set(wb.sheetnames)
    report = []

    for sheet, etype in ENGINEER_SHEETS.items():
        if sheet in sheet_names:
            ingest_engineer_sheet(conn, wb[sheet], etype)

    for sheet, category in GRADED_BLUEPRINT_SHEETS.items():
        if sheet in sheet_names:
            ingest_graded_blueprint_sheet(conn, wb[sheet], category)

    for sheet, category in UNGRADED_COST_SHEETS.items():
        if sheet in sheet_names:
            ingest_ungraded_cost_sheet(conn, wb[sheet], category)

    if SUIT_WEAPON_SHEET in sheet_names:
        ingest_suit_weapon_sheet(conn, wb[SUIT_WEAPON_SHEET])

    for table in [
        "engineers",
        "engineer_unlocks",
        "personal_upgrades",
        "graded_blueprints",
        "ungraded_costs",
        "suit_weapon_costs",
    ]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        report.append(f"{table}: {count} rows")

    conn.close()
    print("\n".join(report))


if __name__ == "__main__":
    main()
