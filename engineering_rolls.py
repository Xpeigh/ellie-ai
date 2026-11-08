"""Rolls per grade, from engineering_rolls.json.

An engineer at rank 5 makes every upgrade N rolls for grade N (a rule, confirmed by the owner and by the
journal). Below rank 5 the journal shows about 4 rolls each for grades 1 to 4 (observed, one commander;
grade 1 varies between 3 and 5). rolls_from_journal.py regenerates the file from any journal.
"""
import json
from pathlib import Path

ROLLS_PATH = Path(__file__).resolve().parent / "engineering_rolls.json"


def load_rolls(path=None):
    with open(Path(path) if path else ROLLS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def is_thin(entry):
    """Only an OBSERVED entry can be thin: fewer than 5 samples, or its usual value covers fewer than
    3 in 4 of them. A rule entry never is."""
    if entry.get("source") != "observed":
        return False
    return entry["samples"] < 5 or entry.get("share", 1.0) < 0.75


def rolls_for(table, rank5, grade):
    """The entry for one grade at an engineer of rank 5 (rank5=True) or below, with a `thin` flag, or None
    when the table has none (grade 5 below rank 5 does not exist: nobody crafts above their rank)."""
    entry = table["rank5" if rank5 else "below5"].get(str(grade))
    return None if entry is None else {**entry, "thin": is_thin(entry)}
