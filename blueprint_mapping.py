"""
Shared module_name -> Coriolis-category mapping.

Coriolis groups blueprint materials by broad category ("Weapon", "Power
plant") rather than specific module name ("Beam Laser") -- see the note
at the top of ingest_coriolis.py for why. This mapping is what lets
anything keyed by a specific module name (your Excel sheet, a query
from Ellie) fall back to Coriolis's broader data when the Excel sheet
does not have that specific module/upgrade/grade filled in yet.

Used by: cross_check.py (QA), query_functions.py (get_blueprint fallback).
None = known to have no Coriolis coverage (Guardian tech).
"""
import re

MODULE_TO_CORIOLIS_KEYWORDS = {
    # hardpoints
    "Abrasion Blaster": ["weapon"],
    "Beam Laser": ["weapon"],
    "Burst Laser": ["weapon"],
    "Cannon": ["weapon"],
    "Enzyme Missile Rack": ["weapon"],
    "Fragment Cannon": ["weapon"],
    "Mine Launcher": ["weapon"],
    "Mining Laser": ["weapon"],
    "Missile Rack": ["weapon"],
    "Multi-cannon": ["weapon"],
    "Plasma Accelerator": ["weapon"],
    "Pulse Laser": ["weapon"],
    "Rail Gun": ["weapon"],
    "Seeker Missile Rack": ["weapon"],
    "Torpedo Pylon": ["weapon"],
    # optional internal
    "Auto Field-Maintenance Unit": ["auto field", "afmu"],
    "Cargo Rack": ["cargo rack"],
    "Collector Limpet Controller": ["collector limpet"],
    "Detailed Surface Scanner": ["detailed surface", "dss"],
    "Fuel Scoop": ["fuel scoop"],
    "Fuel Transfer Limpet Controller": ["fuel transfer limpet"],
    "Hatch Breaker Limpet Controller": ["hatch breaker limpet"],
    "Prospector Limpet Controller": ["prospector limpet"],
    "Refinery": ["refinery"],
    # utility
    "Caustic Sink Launcher": ["caustic sink"],
    "Chaff Launcher": ["chaff launcher"],
    "Electronic Countermeasure": ["electronic counter measures", "ecm"],
    "Frame Shift Wake Scanner": ["wake scanner"],
    "Heat Sink Launcher": ["heat sink launcher"],
    "Kill Warrant Scanner": ["kill warrant", "kws"],
    "Manifest Scanner": ["manifest scanner"],
    "Point Defence": ["point defence"],
    "Shield Booster": ["shield booster"],
    # Guardian tech -- no Coriolis coverage, not an error
    "Guardian Hybrid Power Distributor": None,
    "Guardian Power Plant": None,
    "Guardian Gauss Cannon": None,
    "Guardian Nanite Torpedo Pylon": None,
    "Guardian Plasma Charger": None,
    "Guardian Shard Cannon": None,
    "Guardian FSD Booster": None,
    "Guardian Hull Reinforcement": None,
    "Guardian Module Reinforcement": None,
    "Guardian Shield Reinforcement": None,
}


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
