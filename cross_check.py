"""
Cross-check graded_blueprints (from Engineers.xlsx) against coriolis_blueprints
(from EDCD/coriolis-data) and report every mismatch.

Coriolis groups modules into broad categories ("Weapon", "Power plant", ...)
rather than specific module names -- see the note at the top of
ingest_coriolis.py. This mapping is what makes the two tables comparable,
and it's also exactly what the query layer will need in Phase 3 -- built
once here, reused there.

Guardian modules (Guardian Gauss Cannon, Guardian Power Plant, etc.) use a
different unlock mechanic and simply aren't in coriolis-data's blueprint
file. Those are reported separately as "no reference available", not as
mismatches -- there's nothing to check them against.
"""
import json
import sqlite3

from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")

# module_name (as it appears in the Excel sheet) -> keyword(s) to match
# against coriolis_blueprints.applies_to (case-insensitive substring).
# None = known to have no Coriolis coverage (Guardian tech) -- not an error.
from blueprint_mapping import MODULE_TO_CORIOLIS_KEYWORDS



def normalize_materials(materials_json):
    """{name: qty} with lowercased keys, so 'Sulphur' == 'sulphur'."""
    materials = json.loads(materials_json)
    if isinstance(materials, dict):
        return {k.lower().strip(): v for k, v in materials.items()}
    # Excel's parse_materials() produces a list of {"qty":.., "item":..}
    return {m["item"].lower().strip(): m["qty"] for m in materials if m.get("item")}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    excel_rows = conn.execute(
        "SELECT category, module_name, upgrade, grade, materials_json FROM graded_blueprints"
    ).fetchall()

    no_reference, matches, mismatches, no_coriolis_row = [], [], [], []

    for row in excel_rows:
        module = row["module_name"]
        keywords = MODULE_TO_CORIOLIS_KEYWORDS.get(module, "UNKNOWN")
        if keywords is None:
            no_reference.append(row)
            continue
        if keywords == "UNKNOWN":
            no_reference.append(row)  # module not in our mapping yet -- treat as unverifiable, don't guess
            continue

        candidates = conn.execute(
            "SELECT applies_to, materials_json FROM coriolis_blueprints "
            "WHERE grade = ? AND lower(upgrade_name) = lower(?)",
            (row["grade"], row["upgrade"]),
        ).fetchall()
        matched = [c for c in candidates if any(kw in c["applies_to"].lower() for kw in keywords)]

        if not matched:
            no_coriolis_row.append(row)
            continue

        excel_materials = normalize_materials(row["materials_json"])
        found_exact = False
        for c in matched:
            coriolis_materials = normalize_materials(c["materials_json"])
            if excel_materials == coriolis_materials:
                found_exact = True
                break

        if found_exact:
            matches.append(row)
        else:
            mismatches.append(
                {
                    "module": module,
                    "upgrade": row["upgrade"],
                    "grade": row["grade"],
                    "excel_materials": excel_materials,
                    "coriolis_materials": [normalize_materials(c["materials_json"]) for c in matched],
                }
            )

    print(f"Checked: {len(excel_rows)} rows")
    print(f"  Matches Coriolis exactly: {len(matches)}")
    print(f"  MISMATCHES: {len(mismatches)}")
    print(f"  No Coriolis blueprint found for this module+upgrade+grade: {len(no_coriolis_row)}")
    print(f"  No reference available (Guardian tech / unmapped module): {len(no_reference)}")
    print()

    if mismatches:
        print("=== MISMATCHES ===")
        for m in mismatches:
            print(f"\n{m['module']} / {m['upgrade']} / Grade {m['grade']}")
            print(f"  Excel:    {m['excel_materials']}")
            for cm in m["coriolis_materials"]:
                print(f"  Coriolis: {cm}")

    if no_coriolis_row:
        print("\n=== NO CORIOLIS ROW FOUND (module+upgrade+grade combo not in Coriolis data) ===")
        for row in no_coriolis_row:
            print(f"  {row['module']} / {row['upgrade']} / Grade {row['grade']}" if 'module' in row.keys() else f"  {row['module_name']} / {row['upgrade']} / Grade {row['grade']}")

    conn.close()


if __name__ == "__main__":
    main()
