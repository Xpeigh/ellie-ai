import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import blueprint_mapping as bm


@pytest.mark.parametrize("text, expected", [
    ("Frame Shift Drive", "frameshiftdrive"),
    ("Multi-cannon", "multicannon"),
    ("  Auto Field-Maintenance Unit ", "autofieldmaintenanceunit"),
    ("", ""), (None, ""),
])
def test_squash_keeps_only_letters_and_digits_in_lower_case(text, expected):
    assert bm.squash(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Increased range", {"increased", "range"}),
    ("LongRange", {"long", "range"}),                  # the part of FSD_LongRange after the underscore
    ("FastBoot", {"fast", "boot"}),
    ("Increased FSD Range", {"increased", "fsd", "range"}),
    ("", set()), (None, set()),
])
def test_name_tokens_split_words_and_camel_case(text, expected):
    assert bm.name_tokens(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Conductive Polymers (0)", "Conductive Polymers"),
    ("Flawed Focus Crystals (100)", "Flawed Focus Crystals"),
    ("Precipitated Alloys (87)", "Precipitated Alloys"),
    ("Carbon", "Carbon"),
    ("Modified Embedded Firmware", "Modified Embedded Firmware"),
    ("Merc Coin", "Merc Coin"),
    ("", ""), (None, ""),
])
def test_clean_material_name_drops_only_a_trailing_count_in_brackets(text, expected):
    assert bm.clean_material_name(text) == expected


def test_the_synonym_and_alias_tables_are_well_formed():
    for name, grp in bm.GROUP_SYNONYMS.items():
        assert name == name.lower() and name.strip() == name, name
        assert grp and grp == grp.lower(), name
    assert bm.GROUP_SYNONYMS["armour"] == "bh" and bm.GROUP_SYNONYMS["fsd"] == "fsd"
    for fdname, aliases in bm.BLUEPRINT_ALIASES.items():
        assert "_" in fdname and isinstance(aliases, tuple) and aliases
