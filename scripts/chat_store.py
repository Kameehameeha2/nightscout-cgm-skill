#!/usr/bin/env python3
"""Persist the generated report and its follow-up conversation.

Streamlit re-runs the whole script on every interaction, so anything held only
in local variables dies the moment you type a chat message. Session state would
survive that but not a page refresh, which is what was asked for — so both the
report and the conversation live in the same SQLite file as the readings.

Caveat worth knowing: that file is ephemeral on Streamlit Cloud, so a container
restart still clears the history. Surviving a refresh is the goal here, not
permanent archival.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "cgm_data.db"

MAX_MESSAGES = 60          # keep the prompt bounded on very long conversations


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS session_store (
        name TEXT PRIMARY KEY,
        json TEXT,
        updated_at TEXT
    )""")
    conn.commit()
    return conn


def _put(name: str, value) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO session_store VALUES (?, ?, ?)",
                     (name, json.dumps(value),
                      dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


def _get(name: str):
    if not DB_PATH.exists():
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT json FROM session_store WHERE name = ?",
                           (name,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _drop(name: str) -> None:
    if not DB_PATH.exists():
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM session_store WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def save_report(days: int, context_json: str, report_md: str) -> None:
    """Store the report plus the exact context it was written from, so a
    follow-up question is answered against the same numbers the report used."""
    _put("report", {
        "days": days,
        "context_json": context_json,
        "report_md": report_md,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
    })


def load_report() -> dict | None:
    rec = _get("report")
    return rec if isinstance(rec, dict) and rec.get("report_md") else None


def clear_report() -> None:
    _drop("report")


# --------------------------------------------------------------------------- #
# Conversation
# --------------------------------------------------------------------------- #
def load_messages() -> list[dict]:
    msgs = _get("chat")
    return msgs if isinstance(msgs, list) else []


def save_messages(messages: list[dict]) -> None:
    _put("chat", messages[-MAX_MESSAGES:])


def clear_messages() -> None:
    _drop("chat")
