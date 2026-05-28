"""SQLite store for Support Control dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from support_tools import SupportSession

_SERVER_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = _SERVER_DIR / ".data" / "support_dashboard.sqlite"
_db_lock = Lock()
_active: dict[str, dict[str, Any]] = {}


def _is_serverless() -> bool:
    return os.environ.get("SUPPORT_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def _db_path() -> Path:
    override = os.environ.get("SUPPORT_DASHBOARD_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-support-demo/support_dashboard.sqlite")
    return _DEFAULT_DB


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _db_lock:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    call_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL DEFAULT 'browser_voice',
                    started_at TEXT NOT NULL DEFAULT '',
                    ended_at TEXT NOT NULL DEFAULT '',
                    issue_category TEXT NOT NULL DEFAULT '',
                    escalated INTEGER NOT NULL DEFAULT 0,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    transcript_json TEXT NOT NULL DEFAULT '[]',
                    session_log_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT 'null',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_name TEXT NOT NULL DEFAULT '',
                    dealership TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at DESC);
                """
            )


def register_session_start(call_id: str, *, channel: str = "browser_voice") -> None:
    init_db()
    now = _utc_now()
    _active[call_id] = {"call_id": call_id, "channel": channel, "started_at": now, "transcript": []}
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (call_id, channel, started_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET channel=excluded.channel, updated_at=excluded.updated_at
                """,
                (call_id, channel, now, now),
            )


def persist_session(session: SupportSession, messages: list[dict], *, agent_reply: str = "") -> None:
    init_db()
    transcript = []
    for m in messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            transcript.append({"role": role, "text": str(m.get("content") or "")})
    if agent_reply:
        transcript.append({"role": "assistant", "text": agent_reply})
    now = _utc_now()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                  issue_category = ?,
                  escalated = ?,
                  resolved = ?,
                  transcript_json = ?,
                  session_log_json = ?,
                  ended_at = ?,
                  updated_at = ?
                WHERE call_id = ?
                """,
                (
                    session.issue_category,
                    int(session.escalated),
                    int(session.resolved),
                    json.dumps(transcript),
                    json.dumps(session.session_log),
                    now,
                    now,
                    session.call_id,
                ),
            )


def list_active_sessions() -> list[dict[str, Any]]:
    return list(_active.values())


def list_sessions(*, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "call_id": row["call_id"],
                "channel": row["channel"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "issue_category": row["issue_category"],
                "escalated": bool(row["escalated"]),
                "resolved": bool(row["resolved"]),
                "transcript": json.loads(row["transcript_json"] or "[]"),
                "session_log": json.loads(row["session_log_json"] or "[]"),
            }
        )
    return out


def get_session(call_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE call_id = ?", (call_id,)).fetchone()
    if not row:
        return None
    return {
        "call_id": row["call_id"],
        "channel": row["channel"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "issue_category": row["issue_category"],
        "escalated": bool(row["escalated"]),
        "resolved": bool(row["resolved"]),
        "transcript": json.loads(row["transcript_json"] or "[]"),
        "session_log": json.loads(row["session_log_json"] or "[]"),
    }


def support_stats() -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        escalated = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE escalated = 1").fetchone()["n"]
        resolved = conn.execute("SELECT COUNT(*) AS n FROM sessions WHERE resolved = 1").fetchone()["n"]
    return {
        "sessions_total": total,
        "escalated": escalated,
        "resolved": resolved,
        "active_now": len(_active),
    }


def create_support_ticket(
    *,
    dealership: str,
    email: str,
    phone: str,
    message: str,
    contact_name: str = "",
) -> dict[str, Any]:
    init_db()
    now = _utc_now()
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tickets (contact_name, dealership, email, phone, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_name.strip(),
                    dealership.strip(),
                    email.strip(),
                    phone.strip(),
                    message.strip(),
                    now,
                ),
            )
            ticket_id = cur.lastrowid
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "message": "Thanks — a Hammer representative will reach out as soon as possible.",
    }


SETTING_KEYS = ("support_voice_prompt", "support_chat_prompt", "chat_model")


def get_all_settings() -> dict[str, Any]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value_json"])
        except json.JSONDecodeError:
            out[row["key"]] = row["value_json"]
    return out


def set_settings(values: dict[str, Any]) -> None:
    init_db()
    now = _utc_now()
    with _db_lock:
        with _connect() as conn:
            for key, val in values.items():
                if key not in SETTING_KEYS:
                    continue
                conn.execute(
                    """
                    INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(val), now),
                )


def clear_settings() -> None:
    init_db()
    with _db_lock:
        with _connect() as conn:
            conn.execute("DELETE FROM settings")
