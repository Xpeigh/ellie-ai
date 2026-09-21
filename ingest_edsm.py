"""
Pulls EDSM's populated-systems + stations dumps into elite-dangerous.db.

Run this on YOUR machine -- edsm.net isn't reachable from Claude's sandbox,
so this is untested end-to-end. Run it and paste back whatever prints
(including any error) so we can fix anything that doesn't match.

Deliberately NOT using systemsWithCoordinates.json.gz -- that's the full
89-million-system dump (12+ GB uncompressed) and includes systems nobody's
ever been to. systemsPopulated.json.gz + stations.json.gz covers every
system that actually has a station, which is what a "full station
database" for engineering/trading purposes actually needs.

One-time setup:
    pip install ijson requests

Run:
    python ingest_edsm.py
"""
import gzip
import json
import shutil
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import ijson
import requests


def _clean(value):
    """ijson yields Decimal for JSON numbers with a fractional part;
    sqlite3's driver can't bind Decimal directly, so convert to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value

DUMP_DIR = Path(__file__).resolve().parent / "edsm_dumps"
DB_PATH = str(Path(__file__).resolve().parent / "elite-dangerous.db")

DUMPS = {
    "systemsPopulated": "https://www.edsm.net/dump/systemsPopulated.json.gz",
    "stations": "https://www.edsm.net/dump/stations.json.gz",
}


HEADERS = {
    # Default requests UA literally contains "python" -- plenty of servers,
    # likely including this one, block that outright regardless of whether
    # the request itself is otherwise fine. A normal browser UA fixes it.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def download(name, url):
    DUMP_DIR.mkdir(exist_ok=True)
    dest = DUMP_DIR / f"{name}.json.gz"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"{name}: already downloaded ({dest.stat().st_size / 1e6:.1f} MB), skipping re-download")
        return dest
    print(f"Downloading {name} from {url} ...")
    # streamed to disk, not held in memory -- these files are large
    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f)
    print(f"  saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB compressed)")
    return dest


def build_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS edsm_systems (
            id64 INTEGER PRIMARY KEY, name TEXT, x REAL, y REAL, z REAL,
            allegiance TEXT, government TEXT, population INTEGER,
            security TEXT, primary_economy TEXT, controlling_faction TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_systems_name ON edsm_systems(name);
        """
    )
    # edsm_stations is a full-snapshot table (each run reflects that day's
    # dump, not incremental deltas) -- DROP + recreate is the right
    # semantic here, and also cleanly fixes the wrong schema from the
    # earlier (incorrect) grouped-by-system assumption.
    conn.executescript(
        """
        DROP TABLE IF EXISTS edsm_stations;
        CREATE TABLE edsm_stations (
            station_id INTEGER, market_id INTEGER, name TEXT, type TEXT,
            system_id64 INTEGER, system_name TEXT, distance_to_arrival REAL,
            allegiance TEXT, government TEXT, economy TEXT, second_economy TEXT,
            have_market INTEGER, have_shipyard INTEGER, have_outfitting INTEGER,
            other_services_json TEXT, update_time_json TEXT
        );
        CREATE INDEX idx_stations_system ON edsm_stations(system_id64);
        CREATE INDEX idx_stations_name ON edsm_stations(name);
        """
    )


def ingest_systems(conn, gz_path):
    count = 0
    with gzip.open(gz_path, "rb") as f:
        # streaming parse: yields one system dict at a time, never loads
        # the whole (potentially multi-hundred-MB) array into memory
        for system in ijson.items(f, "item"):
            coords = system.get("coords") or {}
            faction = system.get("controllingFaction") or {}
            conn.execute(
                "INSERT OR REPLACE INTO edsm_systems VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    system.get("id64"),
                    system.get("name"),
                    _clean(coords.get("x")),
                    _clean(coords.get("y")),
                    _clean(coords.get("z")),
                    system.get("allegiance"),
                    system.get("government"),
                    _clean(system.get("population")),
                    system.get("security"),
                    system.get("primaryEconomy"),
                    faction.get("name"),
                ),
            )
            count += 1
            if count % 20000 == 0:
                print(f"  ...{count} systems ingested")
                conn.commit()
    conn.commit()
    return count


def ingest_stations(conn, gz_path):
    gz_path = Path(gz_path)
    print(f"  opening: {gz_path.resolve()}")
    print(f"  file size on disk: {gz_path.stat().st_size / 1e6:.1f} MB")
    count = 0
    with gzip.open(gz_path, "rb") as f:
        for station in ijson.items(f, "item"):
            if count == 0:
                print(f"  first record parsed OK: id={station.get('id')} name={station.get('name')!r}")
            conn.execute(
                "INSERT OR REPLACE INTO edsm_stations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    station.get("id"),
                    station.get("marketId"),
                    station.get("name"),
                    station.get("type"),
                    station.get("systemId64"),
                    station.get("systemName"),
                    _clean(station.get("distanceToArrival")),
                    station.get("allegiance"),
                    station.get("government"),
                    station.get("economy"),
                    station.get("secondEconomy"),
                    int(bool(station.get("haveMarket"))),
                    int(bool(station.get("haveShipyard"))),
                    int(bool(station.get("haveOutfitting"))),
                    json.dumps(station.get("otherServices", [])),
                    json.dumps(station.get("updateTime")),
                ),
            )
            count += 1
            if count % 20000 == 0:
                print(f"  ...{count} stations ingested")
                conn.commit()
    conn.commit()
    print(f"  loop finished, total iterations: {count}")
    return count


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    build_schema(conn)

    systems_gz = download("systemsPopulated", DUMPS["systemsPopulated"])
    stations_gz = download("stations", DUMPS["stations"])

    print("Ingesting systems (this may take a few minutes)...")
    sys_count = ingest_systems(conn, systems_gz)
    print(f"edsm_systems: {sys_count} rows")

    print("Ingesting stations (this may take longer)...")
    stn_count = ingest_stations(conn, stations_gz)
    print(f"edsm_stations: {stn_count} rows")

    conn.close()


if __name__ == "__main__":
    main()
