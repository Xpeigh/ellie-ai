"""
First-run setup for Ellie. Checks Ollama, then builds elite-dangerous.db's
tables from Coriolis (auto-cloned), EDSM (guided manual download -- EDSM
blocks scripted downloads, see ingest_edsm.py), and an optional Excel
import. Also makes sure the live-state tables (commander_status,
materials_inventory, engineer_progress) exist even before
journal_watcher.py's first run, so player_state()/get_material_count()/
distance_to_system() have something to query from the very first launch
instead of raising "no such table" (query_functions.py's @_needs_setup
catches that broadly as a backstop, but a clean setup shouldn't need it).

Safe to re-run any time: every step here skips work that's already done,
same as the ingest scripts it calls into directly (not as subprocesses --
that gets a real Python traceback instead of an opaque nonzero exit code
if one of them ever fails).

Run this once before your first `python ellie_chat.py`:
    python setup.py
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "elite-dangerous.db"

CORIOLIS_DIR = SCRIPT_DIR / "coriolis-data"
CORIOLIS_MARKER = CORIOLIS_DIR / "modifications" / "blueprints.json"
CORIOLIS_URL = "https://github.com/EDCD/coriolis-data.git"

EDSM_DIR = SCRIPT_DIR / "edsm_dumps"
EDSM_FILES = {
    "systemsPopulated.json.gz": "https://www.edsm.net/dump/systemsPopulated.json.gz",
    "stations.json.gz": "https://www.edsm.net/dump/stations.json.gz",
}

XLSX_PATH = SCRIPT_DIR / "Engineers.xlsx"
MODEL = "granite4.1:3b"

# table -> which step owns it, for the final readiness report
STEP_TABLES = {
    "Live state (journal_watcher.py fills these in as you play)": [
        "commander_status", "materials_inventory", "engineer_progress",
    ],
    "Coriolis (blueprint/experimental-effect fallback data)": [
        "coriolis_blueprints", "coriolis_experimental_effects",
    ],
    "Ship/module stats (loadout calculator)": ["ship_stats", "module_stats", "module_blueprints"],
    "EDSM (system distances)": [
        "edsm_systems", "edsm_stations",
    ],
    "Excel sheet (engineers, your own blueprint data) -- optional": [
        "engineers", "engineer_unlocks", "personal_upgrades",
        "graded_blueprints", "ungraded_costs", "suit_weapon_costs",
    ],
}


def _heading(text):
    print(f"\n=== {text} ===")


def _ask_yes_no(prompt, default=True):
    suffix = " [Y/n] " if default else " [y/N] "
    ans = input(prompt + suffix).strip().lower()
    if not ans:
        return default
    return ans.startswith("y")


def _table_count(table):
    """None if the table doesn't exist at all yet, otherwise its row count."""
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def check_ollama():
    """Ollama itself and the model can't be bundled into a future .exe --
    gigabytes, and a separate local server -- so this only checks and
    reports, it never tries to install Ollama."""
    _heading("Ollama")
    try:
        import requests
    except ImportError:
        print("Missing the 'requests' package. Install everything this needs with: pip install -r requirements.txt")
        return False

    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        print(
            "Couldn't reach Ollama on http://localhost:11434.\n"
            "Install it from https://ollama.com, make sure it's running, then re-run this script."
        )
        return False

    models = {m.get("name") for m in resp.json().get("models", [])}
    if MODEL in models:
        print(f"Ollama is running and {MODEL} is pulled.")
        return True

    print(f"Ollama is running, but {MODEL} isn't pulled yet.")
    if shutil.which("ollama") is None:
        print(f"The `ollama` command isn't on PATH -- pull it yourself: ollama pull {MODEL}")
        return False
    if not _ask_yes_no(f"Pull it now ({MODEL} is a few GB)?"):
        print(f"Skipped. Pull it yourself later with: ollama pull {MODEL}")
        return False
    result = subprocess.run(["ollama", "pull", MODEL])
    return result.returncode == 0


def ensure_live_tables():
    _heading("Live-state tables")
    sys.path.insert(0, str(SCRIPT_DIR))
    import journal_watcher as jw

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    jw.build_schema(conn)
    conn.close()
    print("commander_status / materials_inventory / engineer_progress ready.")


def setup_coriolis():
    _heading("Coriolis data (blueprint costs)")
    existing = _table_count("coriolis_blueprints")
    if existing:
        print(f"coriolis_blueprints already has {existing} rows.")
        if not _ask_yes_no("Re-clone/re-ingest anyway?", default=False):
            print("Skipped.")
            return True

    if not CORIOLIS_MARKER.exists():
        if not _ask_yes_no(f"Clone {CORIOLIS_URL} into ./coriolis-data?"):
            print("Skipped -- blueprint lookups will only have your Excel sheet, if any.")
            return False
        if shutil.which("git") is None:
            print("git isn't on PATH -- install Git, or clone the repo yourself, then re-run this script.")
            return False
        result = subprocess.run(["git", "clone", "--depth", "1", CORIOLIS_URL, str(CORIOLIS_DIR)])
        if result.returncode != 0:
            print("Clone failed -- see the error above.")
            return False

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_coriolis
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False
    ingest_coriolis.main()
    return True


def setup_ship_modules():
    _heading("Ship & module stats (loadout calculator)")
    existing = _table_count("ship_stats")
    if existing:
        print(f"ship_stats already has {existing} rows.")
        if not _ask_yes_no("Re-ingest anyway?", default=False):
            print("Skipped.")
            return True

    if not CORIOLIS_MARKER.exists():
        print("Coriolis data isn't cloned yet -- run the Coriolis step above first.")
        return False

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_ship_modules as ism
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False

    conn = sqlite3.connect(DB_PATH)
    ism.build_schema(conn)
    ship_count = ism.build_ships(conn, str(CORIOLIS_DIR))
    module_count = ism.build_modules(conn, str(CORIOLIS_DIR))
    blueprint_count = ism.build_module_blueprints(conn, str(CORIOLIS_DIR))
    conn.close()
    print(f"ship_stats: {ship_count} rows")
    print(f"module_stats: {module_count} rows")
    print(f"module_blueprints: {blueprint_count} rows")
    return True


def setup_edsm():
    _heading("EDSM data (system distances)")
    existing = _table_count("edsm_systems")
    if existing:
        print(f"edsm_systems already has {existing} rows.")
        if not _ask_yes_no("Re-ingest anyway (the dump files already on disk are reused, not re-downloaded)?", default=False):
            print("Skipped.")
            return True

    EDSM_DIR.mkdir(exist_ok=True)
    missing = [name for name in EDSM_FILES if not (EDSM_DIR / name).exists()]
    if missing:
        print("EDSM blocks scripted downloads, so these need to be grabbed by hand:")
        for name in missing:
            print(f"  {EDSM_FILES[name]}  ->  save as {EDSM_DIR / name}")
        if not _ask_yes_no("Download those now, then come back here and continue?"):
            print("Skipped -- distance_to_system won't work until this is done. Re-run this script once you have them.")
            return False
        input("Press Enter once both files are saved in edsm_dumps/...")
        missing = [name for name in EDSM_FILES if not (EDSM_DIR / name).exists()]
        if missing:
            print(f"Still missing: {', '.join(missing)}. Skipping for now -- re-run this script once they're there.")
            return False

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_edsm
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False
    print("Ingesting EDSM data -- this covers ~150k systems and ~750k stations, so it can take a few minutes.")
    ingest_edsm.main()
    return True


def setup_excel():
    _heading("Excel sheet (engineers)")
    if not XLSX_PATH.exists():
        print(f"No {XLSX_PATH.name} found in this folder -- optional, skipping.")
        print("Engineer lookups won't have data until you add the sheet here and re-run this script.")
        return False

    existing = _table_count("graded_blueprints")
    if existing:
        print(f"graded_blueprints already has {existing} rows.")
        if not _ask_yes_no("Re-import anyway?", default=False):
            print("Skipped.")
            return True

    if not _ask_yes_no(f"Import {XLSX_PATH.name}?"):
        print("Skipped.")
        return False

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import ingest_excel
    except ImportError as e:
        print(f"Missing a required package ({e}). Install everything this needs with: pip install -r requirements.txt")
        return False
    ingest_excel.main()
    return True


def verify():
    _heading("Readiness")
    conn = sqlite3.connect(DB_PATH)
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for group, tables in STEP_TABLES.items():
        missing = [t for t in tables if t not in existing]
        status = "ready" if not missing else f"MISSING: {', '.join(missing)}"
        print(f"  [{status}] {group}")


def main():
    print("Ellie setup -- safe to re-run any time; each step skips work that's already done.")
    ensure_live_tables()
    check_ollama()
    setup_coriolis()
    setup_ship_modules()
    setup_edsm()
    setup_excel()
    verify()
    print("\nRun `python journal_watcher.py` while you play, and `python ellie_chat.py` (in a separate terminal) to chat.")


if __name__ == "__main__":
    main()
