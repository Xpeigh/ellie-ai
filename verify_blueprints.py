"""
Check Ellie's engineering costs against what YOUR OWN crafts really consumed.

Every time you engineer a module the game writes an EngineerCraft event to your journal
with the exact materials it took. This replays them all and compares each with the
cost Ellie would give for that blueprint and grade (or experimental effect). A match is
a cost proven in the game; a mismatch is a cost worth reporting.

    python verify_blueprints.py                      # your journal folder, Ellie's database
    python verify_blueprints.py "D:\\Journals"        # another journal folder
    python verify_blueprints.py --write-fixture tests/fixtures/observed_crafts.json
"""
import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

import query_functions as qf

DEFAULT_JOURNAL_DIR = Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"


def read_crafts(journal_dir):
    """Every EngineerCraft event in the folder's Journal.*.log files. `ingredients` maps
    the name the game shows (the journal's own name for raw elements, which have no
    display name) to the count consumed. `effect` is the experimental effect's internal
    name when this craft applied one, else None."""
    crafts = []
    for path in sorted(Path(journal_dir).glob("Journal.*.log")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"EngineerCraft"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") != "EngineerCraft":
                    continue
                ingredients = {i.get("Name_Localised") or i["Name"]: i["Count"]
                               for i in event.get("Ingredients", []) if "Name" in i and "Count" in i}
                crafts.append({"blueprint": event.get("BlueprintName"), "grade": event.get("Level"),
                               "effect": event.get("ApplyExperimentalEffect"), "ingredients": ingredients,
                               "when": event.get("timestamp"), "module": event.get("Module"), "engineer": event.get("Engineer")})
    return crafts


def summarise(crafts):
    """Distinct crafts with how many times each happened. Game facts only: no times,
    no commander data. Two spellings of one material ('Iron' and 'iron') are one
    ingredient; the capitalised spelling is the one kept."""
    grade, special, shown = collections.Counter(), collections.Counter(), {}
    for c in crafts:
        for name in c["ingredients"]:
            key = qf._material_key(name)
            if key not in shown or (name[:1].isupper() and not shown[key][:1].isupper()):
                shown[key] = name
        items = tuple(sorted((qf._material_key(n), q) for n, q in c["ingredients"].items()))
        if c["effect"]:
            special[(c["effect"], items)] += 1
        else:
            grade[(c["blueprint"], c["grade"], items)] += 1

    def named(items):
        return {shown[key]: qty for key, qty in items}

    return {
        "grade_crafts": [{"blueprint": bp, "grade": level, "ingredients": named(items), "crafts": n}
                         for (bp, level, items), n in sorted(grade.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0))],
        "experimental_crafts": [{"effect": effect, "ingredients": named(items), "crafts": n}
                                for (effect, items), n in sorted(special.items(), key=lambda kv: str(kv[0][0]))],
    }


def _judge(kind, ident, grade, observed, expected, crafts):
    if expected is None:
        status = "unknown"
    else:
        same = ({qf._material_key(n): q for n, q in observed.items()} == {qf._material_key(n): q for n, q in expected.items()})
        status = "match" if same else "mismatch"
    return {"kind": kind, "id": ident, "grade": grade, "status": status, "observed": observed, "expected": expected, "crafts": crafts}


def compare(conn, summary):
    """Judge every summarised craft against Ellie's recorded cost. Statuses: 'match',
    'mismatch' (the game consumed something else), 'unknown' (Ellie has no such blueprint)."""
    results = []
    for entry in summary["grade_crafts"]:
        cost = qf._coriolis_cost(conn, entry["blueprint"], entry["grade"])
        results.append(_judge("blueprint", entry["blueprint"], entry["grade"], entry["ingredients"], cost and cost["materials"], entry["crafts"]))
    for entry in summary["experimental_crafts"]:
        cost = qf._special_cost(conn, entry["effect"])
        results.append(_judge("effect", entry["effect"], None, entry["ingredients"], cost and cost["materials"], entry["crafts"]))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check Ellie's engineering costs against your own crafts.")
    parser.add_argument("journal_dir", nargs="?", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--write-fixture", metavar="PATH", help="write the distinct crafts to a JSON file and stop")
    args = parser.parse_args(argv)

    crafts = read_crafts(args.journal_dir)
    summary = summarise(crafts)
    if args.write_fixture:
        Path(args.write_fixture).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{len(crafts)} crafts -> {len(summary['grade_crafts'])} grade combinations, "
              f"{len(summary['experimental_crafts'])} experimental effects -> {args.write_fixture}")
        return 0

    conn = sqlite3.connect(qf.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        results = compare(conn, summary)
    finally:
        conn.close()
    counts = collections.Counter(r["status"] for r in results)
    print(f"{len(crafts)} crafts in {args.journal_dir}")
    print(f"  match: {counts['match']}   mismatch: {counts['mismatch']}   unknown to Ellie: {counts['unknown']}")
    detail = collections.defaultdict(list)
    for c in crafts:
        detail[(c["effect"] or c["blueprint"], None if c["effect"] else c["grade"])].append(c)
    for r in results:
        if r["status"] == "match":
            continue
        print(f"\n{r['status'].upper()}: {r['id']}" + (f" grade {r['grade']}" if r["grade"] else "") + f" ({r['crafts']} craft(s))")
        print(f"  the game consumed: {r['observed']}")
        print(f"  Ellie says:        {r['expected']}")
        for c in detail[(r["id"], r["grade"])][:3]:
            print(f"  e.g. {c['when']}  {c['module']}  {c['engineer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
