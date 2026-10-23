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


def _run_watcher():
    try:
        journal_watcher.main()
    except FileNotFoundError as e:
        print(f"[journal_watcher] {e} -- live status/materials won't update until this is fixed.")
    except Exception as e:
        print(f"[journal_watcher] stopped unexpectedly: {e}")


def _ensure_live_tables():
    # Same call setup.py itself makes -- so a bare `python app.py` on a
    # fresh clone doesn't fail its very first status poll if setup.py was
    # never run.
    conn = sqlite3.connect(journal_watcher.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    journal_watcher.build_schema(conn)
    conn.close()


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
