"""Task 10b B6/B7: the README names the default model and states the system
requirements from measured numbers, with the caveats."""
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_the_setup_pulls_the_default_model_and_explains_how_to_try_another():
    assert "ollama pull bonsai-2:4b" in README
    assert "ollama pull granite4.1:3b" not in README
    assert "ELLIE_MODEL" in README                          # works for the chat window too
    assert "python ellie_chat.py <model>" in README
    assert "[docs/models.md](docs/models.md)" in README
    assert "`lfm3:1.7b-q4_K_M` (1.1 GB) is the lightweight alternative" in README


def test_the_models_page_exists_and_matches_what_the_readme_promises():
    page = (Path(__file__).resolve().parent.parent / "docs" / "models.md").read_text(encoding="utf-8")
    for model in ("bonsai-2:4b", "lfm3:1.7b-q4_K_M", "bonsai-2:2b", "granite4.1:3b"):
        assert f"`{model}`" in page
    assert "ELLIE_MODEL" in page and "python ellie_chat.py <model>" in page
    assert "LFM Open License v1.0" in page                  # the one license with a condition is stated, not hidden


def test_there_is_a_system_requirements_subsection_under_requirements():
    requirements = README.index("## Requirements")
    subsection = README.index("### System requirements")
    assert requirements < subsection < README.index("## Setup")
    # the existing bullets are kept
    assert "- Windows (the journal location is currently hardcoded" in README
    assert "- Disk space:" in README


def test_the_table_carries_the_measured_numbers():
    section = README[README.index("### System requirements"):README.index("## Setup")]
    for needle in (
        "8 GB VRAM", "12 GB VRAM or more", "16 GB", "32 GB if you run other tools",
        "anything that runs the game", "about 6 GB", "about 8 GB", "SSD",
    ):
        assert needle in section, needle


def test_the_notes_and_caveats_are_stated_plainly():
    section = README[README.index("### System requirements"):README.index("## Setup")]
    for needle in (
        "1.6 GB", "1.7-1.9 GB", "about 2 s per answer", "about 48 s", "about 5 s",
        "competes with the game for the CPU", "Ollama 0.40.6 or newer",
        "OLLAMA_KEEP_ALIVE", "30m", "5 minutes", "only needed for the import",
        "tested on NVIDIA only", "derived", "store page",
        "i7-11700K", "RTX 5070 Ti", "0.40.13",
    ):
        assert needle in section, needle
