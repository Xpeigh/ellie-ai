"""
Names for the engineering blueprint lookups. Coriolis groups blueprints by module
group ('fsd', 't', 'pp', ...) and names each blueprint by an internal 'fdname'
(FSD_LongRange); players and the sheet use module and blueprint names in words. This
file holds the pure parts of that translation: the hand-kept synonyms the module files
cannot supply, in-game blueprint aliases, and the small string helpers the lookups
share. The database-backed resolver lives in query_functions.py.
Used by: query_functions.py, ingest_excel.py.
"""
import json
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Part 1 of "engineering depth": names. Everything below is pure (no database).
# ---------------------------------------------------------------------------

# Module names the module files in coriolis-data cannot supply (those files name
# each group, e.g. 'frame_shift_drive' -> "frame shift drive"): abbreviations, a group
# that has no file (ship armour, 'bh'), and the wording players use. name -> group code.
# Names are compared with squash(), so case, spaces and hyphens do not matter.
GROUP_SYNONYMS = {
    "armour": "bh", "armor": "bh", "ship armour": "bh", "ship armor": "bh",
    "bulkheads": "bh", "bulkhead": "bh",
    "engines": "t", "main engines": "t", "drives": "t",
    "distributor": "pd",
    "fsd": "fsd",
    "afmu": "am",
    "hrp": "hr",
    "dss": "ss", "detailed surface scanner": "ss",
    "manifest scanner": "cs",
    "ecm": "ec", "electronic counter measures": "ec",
    "kws": "kw",
    "scb": "scb",
    "fsdi": "fi",
    "wake scanner": "ws",
    "prismatic shield generator": "psg",
}

# In-game blueprint names that share no words with Coriolis's own name for the
# blueprint, keyed by Coriolis fdname. Names that DO share the words ("Increased FSD
# Range" for "Increased range") need no entry: the matcher's word rule finds them.
# An alias is added only once the owner has seen the name in the game.
BLUEPRINT_ALIASES = {
    "Engine_Reinforced": ("Drive Strengthening",),
}


def squash(text):
    """Lower case, letters and digits only: 'Multi-cannon' and 'multi cannon' compare equal."""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def name_tokens(text):
    """The lower-case words in a name, splitting camelCase: 'LongRange' -> {'long', 'range'}."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return set(re.findall(r"[a-z0-9]+", spaced.lower()))


def clean_material_name(text):
    """A material name without the stray count the sheet sometimes leaves in brackets:
    'Conductive Polymers (0)' -> 'Conductive Polymers'."""
    return re.sub(r"\s*\(\d+\)\s*$", "", text or "").strip()


# Evidence-backed corrections to Coriolis's data, applied when the tables are built.
CORRECTIONS_PATH = Path(__file__).resolve().parent / "blueprint_corrections.json"


def load_corrections(path=None):
    """blueprint_corrections.json as {"graded": [...], "experimental": [...], "allowed_effects": [...]};
    a missing file is the same as an empty one. Each entry carries the journal evidence for it."""
    path = Path(path) if path else CORRECTIONS_PATH
    data = json.load(open(path, encoding="utf-8")) if path.exists() else {}
    return {key: data.get(key, []) for key in ("graded", "experimental", "allowed_effects")}


# Words a typed blueprint or effect name may carry besides the name's own words, whatever the module
# ("Dirty Drive Tuning", "Efficient Weapon", "High Capacity Magazine"). Any OTHER extra word must belong
# to the module being asked about, otherwise the match is refused: "Engine Focused" is not "Focused" for a laser.
GENERIC_EXTRA_WORDS = frozenset({"weapon", "weapons", "mount", "modification", "tuning", "drive", "drives",
                                 "magazine", "upgrade", "blueprint"})
