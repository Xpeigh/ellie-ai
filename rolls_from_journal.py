"""
Work out how many rolls each grade takes, from YOUR OWN engineering crafts.

Reads the journal, follows each engineer's rank, and finds the grades you finished (a higher grade of the
same blueprint on the same module followed, or grade 5, which nothing can follow). At an engineer of rank 5
a grade takes N rolls; below rank 5 it takes about 4. Prints the table Ellie plans with.

    python rolls_from_journal.py                          # your journal folder: print the table
    python rolls_from_journal.py "D:\\Journals"            # another folder
    python rolls_from_journal.py --write engineering_rolls.json --write-fixture tests/fixtures/observed_rolls.json
"""
import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

from verify_blueprints import DEFAULT_JOURNAL_DIR


def read_craft_events(journal_dir):
    """Every non-experimental EngineerCraft, in journal order, with the engineer's rank at that moment
    (tracked from EngineerProgress events; None when the journal has not told us yet)."""
    rank, events = {}, []
    for path in sorted(Path(journal_dir).glob("Journal.*.log")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"EngineerProgress"' not in line and '"EngineerCraft"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "EngineerProgress":
                    for entry in (event.get("Engineers") or [event]):
                        if entry.get("Engineer") and "Rank" in entry:
                            rank[entry["Engineer"]] = entry["Rank"]
                elif event.get("event") == "EngineerCraft" and not event.get("ApplyExperimentalEffect"):
                    events.append({"engineer": event.get("Engineer"), "module": event.get("Module"),
                                   "blueprint": event.get("BlueprintName"), "grade": event.get("Level"),
                                   "rank": rank.get(event.get("Engineer"))})
    return events


def finished_segments(events):
    """[{"rank5": bool, "grade": int, "rolls": int}] for every grade the commander finished. A run is
    consecutive crafts of one blueprint on one module with one engineer; a grade is finished when the
    next step of the run is the next grade up, or when it is grade 5 and the run ends there."""
    runs, current = [], None
    for event in events:
        key = (event["module"], event["blueprint"], event["engineer"])
        if current and current["key"] == key:
            current["events"].append(event)
        else:
            current = {"key": key, "events": [event]}
            runs.append(current)
    segments = []
    for run in runs:
        steps = []                                          # [grade, rolls, rank at the first roll]
        for event in run["events"]:
            if steps and steps[-1][0] == event["grade"]:
                steps[-1][1] += 1
            else:
                steps.append([event["grade"], 1, event["rank"]])
        for i, (grade, rolls, rank) in enumerate(steps):
            if rank is None:
                continue
            followed = i + 1 < len(steps) and steps[i + 1][0] == grade + 1
            last_grade = grade == 5 and i == len(steps) - 1
            if followed or last_grade:
                segments.append({"rank5": rank == 5, "grade": grade, "rolls": rolls})
    return segments


def build_table(segments, basis):
    """The rolls table. rank5: a rule, N rolls for grade N (up_to = the most ever seen, never below N;
    samples = how many finished grades were seen). below5: the most common value seen for grades 1 to 4
    (ties take the smaller), with up_to, samples and the share of samples that equal it."""
    rank5, below5 = {}, {}
    for grade in range(1, 6):
        seen = [s["rolls"] for s in segments if s["rank5"] and s["grade"] == grade]
        rank5[str(grade)] = {"typical": grade, "up_to": max([grade] + seen), "samples": len(seen), "source": "rule"}
    for grade in range(1, 5):
        seen = [s["rolls"] for s in segments if not s["rank5"] and s["grade"] == grade]
        if not seen:
            continue
        counts = collections.Counter(seen)
        top = max(counts.values())
        below5[str(grade)] = {"typical": min(v for v, n in counts.items() if n == top), "up_to": max(seen),
                              "samples": len(seen), "source": "observed", "share": round(top / len(seen), 2)}
    return {"basis": basis, "rank5": rank5, "below5": below5}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Work out rolls per grade from your own engineering crafts.")
    parser.add_argument("journal_dir", nargs="?", default=str(DEFAULT_JOURNAL_DIR))
    parser.add_argument("--write", metavar="PATH", help="write the table (engineering_rolls.json)")
    parser.add_argument("--write-fixture", metavar="PATH", help="write the finished grades (game facts only) for the tests")
    args = parser.parse_args(argv)

    events = read_craft_events(args.journal_dir)
    segments = finished_segments(events)
    basis = (f"Observed in {len(events)} engineering crafts from one commander's journal, read on {datetime.date.today().isoformat()}. "
             "The rank-5 figures are the game's rule (grade N takes N rolls), confirmed by the commander; the below-rank-5 figures are the most common value seen.")
    table = build_table(segments, basis)
    print(f"{len(events)} crafts, {len(segments)} finished grades")
    for name in ("rank5", "below5"):
        for grade, entry in table[name].items():
            print(f"  {name} grade {grade}: {entry}")
    if args.write:
        Path(args.write).write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
        print("wrote", args.write)
    if args.write_fixture:
        Path(args.write_fixture).write_text(json.dumps(segments, indent=0) + "\n", encoding="utf-8")
        print("wrote", args.write_fixture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
