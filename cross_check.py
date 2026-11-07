"""
Cross-check the spreadsheet (graded_blueprints, from Engineers.xlsx) against Coriolis
(coriolis_blueprints) and report every difference, plus the material names the
sheet uses that no other source knows (typos, currency, placeholders) so the
owner can fix the sheet.

A sheet row is matched to Coriolis the way Ellie's own lookup does it: the module name
resolves to its group(s), the group's allow-list (module_blueprints) says which blueprints
it can take, and the upgrade name is matched with the forgiving blueprint matcher.

Guardian modules use a different unlock mechanic and aren't in Coriolis's blueprint
file; those are reported as "no reference", not as mismatches.

    python cross_check.py
"""
import json
import sqlite3
from pathlib import Path

import query_functions as qf

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")


def normalize_materials(materials_json):
    """{name: qty} with lower-cased keys, so 'Sulphur' == 'sulphur', and no stray '(n)'."""
    counted, _extras = qf._split_materials(materials_json)
    return {name.lower().strip(): qty for name, qty in counted.items()}


def compare_row(conn, row):
    """(status, detail) for one graded_blueprints row. status: 'match', 'mismatch'
    (detail has the two cost dicts), 'no_coriolis_row' (Coriolis has no such blueprint
    and grade) or 'no_reference' (the module isn't one Coriolis has blueprints for)."""
    module = row["module_name"] or ""
    groups = ["bh"] if row["category"] == "armour" and not module else qf._resolve_module_groups(conn, module)
    if not groups:
        return "no_reference", None
    allowed = [r["fdname"] for r in conn.execute(
        f"SELECT DISTINCT fdname FROM module_blueprints WHERE grp IN ({qf._in_marks(groups)})", groups)]
    if not allowed:
        return "no_reference", None
    known = conn.execute(
        f"SELECT DISTINCT fdname, upgrade_name FROM coriolis_blueprints WHERE fdname IN ({qf._in_marks(allowed)})", allowed).fetchall()
    hits = qf._blueprint_name_matches(known, row["upgrade"])
    cost = qf._coriolis_cost(conn, hits[0]["fdname"], row["grade"]) if len(hits) == 1 else None
    if cost is None:
        return "no_coriolis_row", None
    sheet = normalize_materials(row["materials_json"])
    coriolis = {name.lower().strip(): qty for name, qty in cost["materials"].items()}
    if sheet == coriolis:
        return "match", None
    return "mismatch", {"sheet": sheet, "coriolis": coriolis}


def suspect_material_names(conn):
    """Material names the sheet uses that neither Coriolis nor the commander's own
    inventory knows, sorted: typos, currency, placeholders."""
    known = set()
    for table in ("coriolis_blueprints", "coriolis_experimental_effects"):
        for (materials_json,) in conn.execute(f"SELECT materials_json FROM {table}"):
            known.update(qf._material_key(n) for n in qf._split_materials(materials_json)[0])
    for row in conn.execute("SELECT material_name, display_name FROM materials_inventory"):
        known.add(row[0])
        known.add(qf._material_key(row[1] or ""))
    suspects = set()
    for table in ("graded_blueprints", "ungraded_costs"):
        for (materials_json,) in conn.execute(f"SELECT materials_json FROM {table}"):
            counted, extras = qf._split_materials(materials_json)
            for name in list(counted) + extras:
                if qf._material_key(name) not in known:
                    suspects.add(name)
    return sorted(suspects)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT category, module_name, upgrade, grade, materials_json FROM graded_blueprints").fetchall()

    buckets = {"match": [], "mismatch": [], "no_coriolis_row": [], "no_reference": []}
    for row in rows:
        status, detail = compare_row(conn, row)
        buckets[status].append((row, detail))

    print(f"Checked: {len(rows)} rows")
    print(f"  Matches Coriolis exactly: {len(buckets['match'])}")
    print(f"  MISMATCHES: {len(buckets['mismatch'])}")
    print(f"  No Coriolis blueprint found for this module+upgrade+grade: {len(buckets['no_coriolis_row'])}")
    print(f"  No reference available (Guardian tech / unmapped module): {len(buckets['no_reference'])}")
    print()

    if buckets["mismatch"]:
        print("=== MISMATCHES ===")
        for row, detail in buckets["mismatch"]:
            print(f"\n{row['module_name']} / {row['upgrade']} / Grade {row['grade']}")
            print(f"  Sheet:    {detail['sheet']}")
            print(f"  Coriolis: {detail['coriolis']}")

    if buckets["no_coriolis_row"]:
        print("\n=== NO CORIOLIS ROW FOUND (module+upgrade+grade combo not in Coriolis data) ===")
        for row, _ in buckets["no_coriolis_row"]:
            print(f"  {row['module_name']} / {row['upgrade']} / Grade {row['grade']}")

    suspects = suspect_material_names(conn)
    if suspects:
        print("\n=== MATERIAL NAMES IN THE SHEET THAT NO OTHER SOURCE KNOWS (typos, currency, placeholders?) ===")
        for name in suspects:
            print(f"  {name}")

    conn.close()


if __name__ == "__main__":
    main()
