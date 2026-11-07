import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_excel as ie


def test_a_bracketed_count_after_a_material_name_is_dropped():
    cell = "1 Conductive Polymers (0)\n100 Flawed Focus Crystals (100)\n5 Merc Coin"
    assert ie.parse_materials(cell) == [
        {"qty": 1, "item": "Conductive Polymers"},
        {"qty": 100, "item": "Flawed Focus Crystals"},
        {"qty": 5, "item": "Merc Coin"},
    ]


def test_a_line_without_a_quantity_is_kept_verbatim_with_no_quantity():
    assert ie.parse_materials("Needs personal research") == [{"qty": None, "item": "Needs personal research"}]


def test_empty_and_placeholder_cells_stay_empty():
    assert ie.parse_materials(None) == [] and ie.parse_materials("-") == [] and ie.parse_materials("  ") == []
