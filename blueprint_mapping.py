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
