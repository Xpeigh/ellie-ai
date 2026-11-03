"""
FastAPI backend for Ellie's chat window (Phase 5a). Serves static/index.html
and wires it to the same ellie_chat.py / query_functions.py logic the
terminal chat already uses -- this file adds no new chat or tool logic,
only a thin HTTP layer over what's already tested.

Runs journal_watcher.py in a background daemon thread so the live-status
sidebar and the chat's own tools (player_state, missing_materials, etc.)
see current data without a separate terminal window.

Run:
    python app.py
Then open http://127.0.0.1:8420 (or just let pywebview open it, later).
"""
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ellie_chat
import journal_watcher
import query_functions as qf

HOST, PORT = "127.0.0.1", 8420

# Chat history lives here, server-side, for the life of this process. The
# commander's own preference (2026-09-21): session-only (no disk storage,
# resets on restart), but Ellie must keep "flow" -- remember earlier turns
# -- for as long as one run of the app stays open. A single module-level
# list is enough for that: this is a single-user local app, there's no
# real concept of separate concurrent "sessions" to keep apart. A lock
# guards it in case two requests ever land at once (e.g. two browser tabs).
_history = [{"role": "system", "content": ellie_chat.SYSTEM_PROMPT}]
_history_lock = threading.Lock()


# How long to wait before starting the watcher again after it stops.
WATCHER_RESTART_SECONDS = 5
# While the journal folder is missing the wait doubles each time (5, 10, 20, 40 ...)
# up to this cap; a watcher that ran this long counts as having had a folder.
WATCHER_MAX_BACKOFF_SECONDS = 60
WATCHER_STABLE_RUN_SECONDS = 30


def watch_forever(target, sleep=time.sleep, delay=None, clock=time.monotonic):
    """Keep `target` (the journal watcher) running for as long as the app
    does: if it raises or returns, say so on stderr, wait `delay` seconds
    (WATCHER_RESTART_SECONDS by default) and start it again. A watcher thread
    that simply died would leave the tools quietly serving stale fuel and
    loadout.

    When the journal folder is missing (the game has never run) it backs off
    instead of retrying every few seconds forever: the wait doubles from `delay`
    up to WATCHER_MAX_BACKOFF_SECONDS, a line is printed only when that wait
    changes, and the back-off starts over once the folder exists (the watcher
    ran, or ended some other way). `sleep` and `clock` are injectable so a test
    doesn't have to wait."""
    base = WATCHER_RESTART_SECONDS if delay is None else delay
    cap = max(base, WATCHER_MAX_BACKOFF_SECONDS)
    misses = 0            # consecutive "no journal folder" failures
    announced = None      # the back-off wait last printed for this streak
    while True:
        started = clock()
        wait = base
        try:
            target()
            outcome = "returned unexpectedly -- restarting"
        except FileNotFoundError as e:
            if clock() - started >= WATCHER_STABLE_RUN_SECONDS:
                misses, announced = 0, None       # it was running fine: a new problem, not the same streak
            misses += 1
            wait = min(base * 2 ** (misses - 1), cap)
            if wait != announced:
                print(f"[journal_watcher] {e} -- live status/materials won't update until this is fixed; "
                      f"retrying in {wait} s", file=sys.stderr)
                announced = wait
            sleep(wait)
            continue
        except Exception:
            traceback.print_exc()
            outcome = "stopped unexpectedly -- restarting"
        if misses:
            print("[journal_watcher] journal folder found", file=sys.stderr)
        misses, announced = 0, None
        print(f"[journal_watcher] {outcome}", file=sys.stderr)
        sleep(wait)


def _run_watcher():
    watch_forever(journal_watcher.main)


def _ensure_live_tables():
    # Same call setup.py itself makes -- so a bare `python app.py` on a
    # fresh clone doesn't fail its very first status poll if setup.py was
    # never run.
    conn = sqlite3.connect(journal_watcher.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    journal_watcher.build_schema(conn)
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_live_tables()
    threading.Thread(target=_run_watcher, daemon=True).start()
    yield


app = FastAPI(title="Ellie", lifespan=lifespan)


class ChatMessage(BaseModel):
    message: str


@app.get("/")
def index():
    return FileResponse(SCRIPT_DIR / "static" / "index.html")


@app.post("/api/chat")
def chat(body: ChatMessage):
    message = body.message.strip()
    if not message:
        return {"reply": ""}
    with _history_lock:
        reply, _ = ellie_chat.ask_ellie(message, _history)
    return {"reply": reply}


@app.post("/api/reset")
def reset():
    with _history_lock:
        _history.clear()
        _history.append({"role": "system", "content": ellie_chat.SYSTEM_PROMPT})
    return {"ok": True}


@app.get("/api/status")
def status():
    return qf.player_state()


app.mount("/static", StaticFiles(directory=str(SCRIPT_DIR / "static")), name="static")


if __name__ == "__main__":
    print(f"Ellie is online at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
