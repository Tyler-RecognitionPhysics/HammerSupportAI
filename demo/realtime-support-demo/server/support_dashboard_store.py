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
                    interaction_summary TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requested_at TEXT NOT NULL DEFAULT '',
                    duration_min INTEGER NOT NULL DEFAULT 30,
                    dealership_name TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'requested',
                    source TEXT NOT NULL DEFAULT 'ai',
                    channel TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT '',
                    requested_label TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_appointments_requested ON appointments(requested_at);
                CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);
                CREATE TABLE IF NOT EXISTS cs_questions_cache (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    data_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO cs_questions_cache (id, data_json) VALUES (1, '{}');
                CREATE TABLE IF NOT EXISTS cs_question_map_cache (
                    issue_hash TEXT PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT '',
                    issue_text TEXT NOT NULL DEFAULT '',
                    is_support INTEGER NOT NULL DEFAULT 0,
                    label TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_cs_question_map_cache_model ON cs_question_map_cache(model);
                CREATE TABLE IF NOT EXISTS qa_answers (
                    question_key TEXT PRIMARY KEY,
                    question TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'other',
                    answer TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'approved',
                    source TEXT NOT NULL DEFAULT 'human',
                    sources_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            _migrate_session_columns(conn)
            _migrate_ticket_columns(conn)
            _migrate_qa_answer_columns(conn)


def _migrate_session_columns(conn: sqlite3.Connection) -> None:
    cols = {
        "interaction_summary": "TEXT NOT NULL DEFAULT ''",
        "ticket_created": "INTEGER NOT NULL DEFAULT 0",
        "hubspot_ticket_id": "TEXT NOT NULL DEFAULT ''",
        "dealership_name": "TEXT NOT NULL DEFAULT ''",
        "first_name": "TEXT NOT NULL DEFAULT ''",
        "last_name": "TEXT NOT NULL DEFAULT ''",
        "email": "TEXT NOT NULL DEFAULT ''",
        "phone": "TEXT NOT NULL DEFAULT ''",
    }
    for name, typedef in cols.items():
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {typedef}")
        except sqlite3.OperationalError:
            pass


def _migrate_ticket_columns(conn: sqlite3.Connection) -> None:
    cols = {
        "first_name": "TEXT NOT NULL DEFAULT ''",
        "last_name": "TEXT NOT NULL DEFAULT ''",
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "channel": "TEXT NOT NULL DEFAULT ''",
        "resolved": "INTEGER NOT NULL DEFAULT 0",
        "hubspot_ticket_id": "TEXT NOT NULL DEFAULT ''",
        "issue_category": "TEXT NOT NULL DEFAULT ''",
    }
    for name, typedef in cols.items():
        try:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {typedef}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_session ON tickets(session_id)")
    except sqlite3.OperationalError:
        pass


def _migrate_qa_answer_columns(conn: sqlite3.Connection) -> None:
    cols = {
        "status": "TEXT NOT NULL DEFAULT 'approved'",
        "source": "TEXT NOT NULL DEFAULT 'human'",
        "sources_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, typedef in cols.items():
        try:
            conn.execute(f"ALTER TABLE qa_answers ADD COLUMN {name} {typedef}")
        except sqlite3.OperationalError:
            pass


def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
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
        "interaction_summary": row["interaction_summary"] if "interaction_summary" in keys else "",
        "ticket_created": bool(row["ticket_created"]) if "ticket_created" in keys else False,
        "hubspot_ticket_id": row["hubspot_ticket_id"] if "hubspot_ticket_id" in keys else "",
        "dealership_name": row["dealership_name"] if "dealership_name" in keys else "",
        "first_name": row["first_name"] if "first_name" in keys else "",
        "last_name": row["last_name"] if "last_name" in keys else "",
        "email": row["email"] if "email" in keys else "",
        "phone": row["phone"] if "phone" in keys else "",
    }


def hydrate_support_session(session: SupportSession, call_id: str) -> None:
    """Load persisted session state into SupportSession (voice + chat)."""
    data = get_session(call_id)
    if not data:
        return
    session.call_id = call_id
    session.channel = data.get("channel") or session.channel
    session.issue_category = data.get("issue_category") or ""
    session.escalated = bool(data.get("escalated"))
    session.resolved = bool(data.get("resolved"))
    session.session_log = list(data.get("session_log") or [])
    session.ticket_created = bool(data.get("ticket_created"))
    session.hubspot_ticket_id = str(data.get("hubspot_ticket_id") or "")
    session.dealership_name = str(data.get("dealership_name") or "")
    session.first_name = str(data.get("first_name") or "")
    session.last_name = str(data.get("last_name") or "")
    session.email = str(data.get("email") or "")
    session.phone = str(data.get("phone") or "")


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
                  ticket_created = ?,
                  hubspot_ticket_id = ?,
                  dealership_name = ?,
                  first_name = ?,
                  last_name = ?,
                  email = ?,
                  phone = ?,
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
                    int(getattr(session, "ticket_created", False)),
                    str(getattr(session, "hubspot_ticket_id", "") or ""),
                    str(getattr(session, "dealership_name", "") or ""),
                    str(getattr(session, "first_name", "") or ""),
                    str(getattr(session, "last_name", "") or ""),
                    str(getattr(session, "email", "") or ""),
                    str(getattr(session, "phone", "") or ""),
                    now,
                    now,
                    session.call_id,
                ),
            )

    if session.call_id in _active:
        if session.resolved or session.escalated:
            _active.pop(session.call_id, None)
        else:
            _active[session.call_id]["transcript"] = transcript

    if transcript:
        import threading
        def _bg_summary():
            try:
                summary = _generate_ai_summary(transcript)
                if summary:
                    with _db_lock:
                        with _connect() as conn:
                            conn.execute(
                                "UPDATE sessions SET interaction_summary = ? WHERE call_id = ?",
                                (summary, session.call_id)
                            )
            except Exception:
                pass
        threading.Thread(target=_bg_summary, daemon=True).start()


def _generate_ai_summary(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        role = "User" if turn.get("role") == "user" else "Hannah"
        lines.append(f"{role}: {turn.get('text')}")
    transcript_text = "\n".join(lines)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or not transcript_text:
        return ""

    import httpx
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an expert customer support auditor. Analyze the following conversation transcript "
                                "between a customer (User) and our support assistant (Hannah). Write a very concise, one-sentence "
                                "summary of what happened, what the customer asked/needed, and whether it was resolved or escalated. Keep it under 25 words."
                            )
                        },
                        {
                            "role": "user",
                            "content": f"Transcript:\n{transcript_text}"
                        }
                    ],
                    "temperature": 0.3,
                    "max_tokens": 150
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return ""


def list_active_sessions() -> list[dict[str, Any]]:
    return list(_active.values())


def list_sessions(*, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_session_row_to_dict(row) for row in rows]


def get_session(call_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE call_id = ?", (call_id,)).fetchone()
    if not row:
        return None
    return _session_row_to_dict(row)


def session_ticket_created(call_id: str) -> bool:
    data = get_session(call_id)
    return bool(data and data.get("ticket_created"))


def update_session_ticket_state(
    call_id: str,
    *,
    ticket_created: bool,
    hubspot_ticket_id: str = "",
    dealership: str = "",
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    resolved: bool = False,
) -> None:
    init_db()
    now = _utc_now()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                  ticket_created = ?,
                  hubspot_ticket_id = ?,
                  dealership_name = ?,
                  first_name = ?,
                  last_name = ?,
                  email = ?,
                  phone = ?,
                  resolved = CASE WHEN ? = 1 THEN 1 ELSE resolved END,
                  updated_at = ?
                WHERE call_id = ?
                """,
                (
                    int(ticket_created),
                    hubspot_ticket_id.strip(),
                    dealership.strip(),
                    first_name.strip(),
                    last_name.strip(),
                    email.strip(),
                    phone.strip(),
                    int(resolved),
                    now,
                    call_id,
                ),
            )


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


def record_support_ticket(
    *,
    dealership: str,
    email: str,
    phone: str,
    message: str,
    first_name: str = "",
    last_name: str = "",
    contact_name: str = "",
    session_id: str = "",
    channel: str = "",
    resolved: bool = False,
    hubspot_ticket_id: str = "",
    issue_category: str = "",
) -> dict[str, Any]:
    init_db()
    fn = first_name.strip()
    ln = last_name.strip()
    if not fn and not ln and contact_name.strip():
        parts = contact_name.strip().split(None, 1)
        fn = parts[0] if parts else ""
        ln = parts[1] if len(parts) > 1 else ""
    contact_label = f"{fn} {ln}".strip() or contact_name.strip()
    now = _utc_now()
    from hubspot_ticket_create import hubspot_ticket_url

    ticket_url = hubspot_ticket_url(hubspot_ticket_id) if hubspot_ticket_id else ""
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tickets (
                  contact_name, dealership, email, phone, message, created_at,
                  first_name, last_name, session_id, channel, resolved, hubspot_ticket_id, issue_category
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_label,
                    dealership.strip(),
                    email.strip(),
                    phone.strip(),
                    message.strip(),
                    now,
                    fn,
                    ln,
                    session_id.strip(),
                    channel.strip(),
                    int(resolved),
                    hubspot_ticket_id.strip(),
                    issue_category.strip(),
                ),
            )
            ticket_id = cur.lastrowid
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "ticket_url": ticket_url,
        "hubspot_ticket_id": hubspot_ticket_id.strip(),
    }


def get_ticket_for_session(session_id: str) -> dict[str, Any] | None:
    if not session_id.strip():
        return None
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets WHERE session_id = ? ORDER BY id DESC LIMIT 1
            """,
            (session_id.strip(),),
        ).fetchone()
    if not row:
        return None
    from hubspot_ticket_create import hubspot_ticket_url

    hid = str(row["hubspot_ticket_id"] if "hubspot_ticket_id" in row.keys() else "")
    return {
        "id": row["id"],
        "hubspot_ticket_id": hid,
        "ticket_url": hubspot_ticket_url(hid) if hid else "",
        "dealership": row["dealership"],
        "email": row["email"],
        "phone": row["phone"],
        "message": row["message"],
        "session_id": row["session_id"] if "session_id" in row.keys() else "",
        "resolved": bool(row["resolved"]) if "resolved" in row.keys() else False,
    }


def list_support_tickets(*, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    from hubspot_ticket_create import hubspot_ticket_url

    out: list[dict[str, Any]] = []
    for row in rows:
        keys = row.keys()
        hid = str(row["hubspot_ticket_id"]) if "hubspot_ticket_id" in keys else ""
        out.append(
            {
                "id": row["id"],
                "contact_name": row["contact_name"],
                "first_name": row["first_name"] if "first_name" in keys else "",
                "last_name": row["last_name"] if "last_name" in keys else "",
                "dealership": row["dealership"],
                "email": row["email"],
                "phone": row["phone"],
                "message": row["message"],
                "created_at": row["created_at"],
                "session_id": row["session_id"] if "session_id" in keys else "",
                "channel": row["channel"] if "channel" in keys else "",
                "resolved": bool(row["resolved"]) if "resolved" in keys else False,
                "hubspot_ticket_id": hid,
                "ticket_url": hubspot_ticket_url(hid) if hid else "",
                "issue_category": row["issue_category"] if "issue_category" in keys else "",
            }
        )
    return out


def create_support_ticket(
    *,
    dealership: str,
    email: str,
    phone: str,
    message: str,
    contact_name: str = "",
    first_name: str = "",
    last_name: str = "",
    session_id: str = "",
    channel: str = "",
    resolved: bool = False,
) -> dict[str, Any]:
    """Legacy local-only insert; prefer support_ticket_service.create_and_notify_ticket."""
    row = record_support_ticket(
        dealership=dealership,
        email=email,
        phone=phone,
        message=message,
        contact_name=contact_name,
        first_name=first_name,
        last_name=last_name,
        session_id=session_id,
        channel=channel,
        resolved=resolved,
    )
    return {
        "ok": True,
        "ticket_id": row["ticket_id"],
        "message": "Thanks — a Hammer representative will reach out as soon as possible.",
    }


APPOINTMENT_STATUSES = ("requested", "confirmed", "completed", "cancelled")
_APPOINTMENT_FIELDS = (
    "requested_at", "duration_min", "dealership_name", "first_name", "last_name",
    "email", "phone", "reason", "status", "source", "channel", "session_id",
    "timezone", "requested_label", "notes",
)


def _appointment_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "requested_at": row["requested_at"],
        "duration_min": row["duration_min"],
        "dealership_name": row["dealership_name"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "contact_name": f"{row['first_name']} {row['last_name']}".strip(),
        "email": row["email"],
        "phone": row["phone"],
        "reason": row["reason"],
        "status": row["status"],
        "source": row["source"],
        "channel": row["channel"],
        "session_id": row["session_id"],
        "timezone": row["timezone"],
        "requested_label": row["requested_label"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_appointment(
    *,
    requested_at: str,
    dealership_name: str = "",
    first_name: str = "",
    last_name: str = "",
    email: str = "",
    phone: str = "",
    reason: str = "",
    duration_min: int = 30,
    status: str = "requested",
    source: str = "ai",
    channel: str = "",
    session_id: str = "",
    timezone: str = "",
    requested_label: str = "",
    notes: str = "",
) -> dict[str, Any]:
    init_db()
    now = _utc_now()
    status = status if status in APPOINTMENT_STATUSES else "requested"
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO appointments (
                  requested_at, duration_min, dealership_name, first_name, last_name,
                  email, phone, reason, status, source, channel, session_id,
                  timezone, requested_label, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requested_at.strip(),
                    int(duration_min or 30),
                    dealership_name.strip(),
                    first_name.strip(),
                    last_name.strip(),
                    email.strip(),
                    phone.strip(),
                    reason.strip(),
                    status,
                    source.strip() or "ai",
                    channel.strip(),
                    session_id.strip(),
                    timezone.strip(),
                    requested_label.strip(),
                    notes.strip(),
                    now,
                    now,
                ),
            )
            appt_id = cur.lastrowid
            row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
    return _appointment_row_to_dict(row)


def list_appointments(
    *,
    start: str = "",
    end: str = "",
    status: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if start.strip():
        clauses.append("requested_at >= ?")
        params.append(start.strip())
    if end.strip():
        clauses.append("requested_at <= ?")
        params.append(end.strip())
    if status.strip():
        clauses.append("status = ?")
        params.append(status.strip())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM appointments{where} ORDER BY requested_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_appointment_row_to_dict(r) for r in rows]


def get_appointment(appointment_id: int) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (int(appointment_id),)).fetchone()
    return _appointment_row_to_dict(row) if row else None


def update_appointment(appointment_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    init_db()
    updates = {k: v for k, v in fields.items() if k in _APPOINTMENT_FIELDS and v is not None}
    if "status" in updates and updates["status"] not in APPOINTMENT_STATUSES:
        updates.pop("status")
    if not updates:
        return get_appointment(appointment_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values())
    if "duration_min" in updates:
        params[list(updates).index("duration_min")] = int(updates["duration_min"] or 30)
    params.append(_utc_now())
    params.append(int(appointment_id))
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                f"UPDATE appointments SET {set_clause}, updated_at = ? WHERE id = ?",
                tuple(params),
            )
    return get_appointment(appointment_id)


def delete_appointment(appointment_id: int) -> bool:
    init_db()
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM appointments WHERE id = ?", (int(appointment_id),))
    return cur.rowcount > 0


SETTING_KEYS = ("support_voice_prompt", "support_chat_prompt", "chat_model", "kb_enabled_sources")


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


def get_cs_questions_cache() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT data_json, updated_at FROM cs_questions_cache WHERE id = 1"
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    if not data:
        return None
    data.setdefault("generated_at", row["updated_at"] or "")
    return data


def set_cs_questions_cache(data: dict[str, Any], *, merge: bool = False) -> None:
    init_db()
    now = _utc_now()
    with _db_lock:
        with _connect() as conn:
            if merge:
                row = conn.execute(
                    "SELECT data_json FROM cs_questions_cache WHERE id = 1"
                ).fetchone()
                existing: dict[str, Any] = {}
                if row:
                    try:
                        existing = json.loads(row["data_json"] or "{}")
                    except json.JSONDecodeError:
                        existing = {}
                existing.update(data)
                payload = existing
            else:
                payload = data
            conn.execute(
                """
                INSERT INTO cs_questions_cache (id, data_json, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at
                """,
                (json.dumps(payload), now),
            )


def get_cs_question_map_cache(model: str, issue_hashes: list[str]) -> dict[str, dict[str, Any]]:
    init_db()
    keys = [h for h in issue_hashes if h]
    if not keys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with _connect() as conn:
        for start in range(0, len(keys), 500):
            batch = keys[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT issue_hash, is_support, label, category
                FROM cs_question_map_cache
                WHERE model = ? AND issue_hash IN ({placeholders})
                """,
                (model, *batch),
            ).fetchall()
            for row in rows:
                out[str(row["issue_hash"])] = {
                    "s": bool(row["is_support"]),
                    "q": str(row["label"] or ""),
                    "c": str(row["category"] or "other"),
                }
    return out


def set_cs_question_map_cache(model: str, rows: list[dict[str, Any]]) -> None:
    init_db()
    now = _utc_now()
    payload = [
        (
            str(row.get("issue_hash") or ""),
            model,
            str(row.get("issue_text") or "")[:1200],
            1 if bool(row.get("s")) else 0,
            str(row.get("q") or ""),
            str(row.get("c") or "other"),
            now,
        )
        for row in rows
        if row.get("issue_hash")
    ]
    if not payload:
        return
    with _db_lock:
        with _connect() as conn:
            conn.executemany(
                """
                INSERT INTO cs_question_map_cache (
                  issue_hash, model, issue_text, is_support, label, category, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_hash) DO UPDATE SET
                  model=excluded.model,
                  issue_text=excluded.issue_text,
                  is_support=excluded.is_support,
                  label=excluded.label,
                  category=excluded.category,
                  updated_at=excluded.updated_at
                """,
                payload,
            )


def _qa_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    try:
        sources = json.loads(row["sources_json"] or "[]") if "sources_json" in keys else []
    except (json.JSONDecodeError, TypeError):
        sources = []
    return {
        "question_key": row["question_key"],
        "question": row["question"],
        "category": row["category"],
        "answer": row["answer"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
        "status": (row["status"] if "status" in keys else "approved") or "approved",
        "source": (row["source"] if "source" in keys else "human") or "human",
        "sources": sources,
    }


def get_qa_answers() -> dict[str, dict[str, Any]]:
    """All saved CS answers keyed by question_key."""
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM qa_answers").fetchall()
    return {row["question_key"]: _qa_row_to_dict(row) for row in rows}


def set_qa_answer(
    *,
    question_key: str,
    question: str,
    category: str,
    answer: str,
    updated_by: str = "",
    status: str = "approved",
    source: str = "human",
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    init_db()
    now = _utc_now()
    status = (status or "approved").strip() or "approved"
    source = (source or "human").strip() or "human"
    sources_json = json.dumps(sources or [])
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO qa_answers (
                  question_key, question, category, answer, updated_by, updated_at,
                  status, source, sources_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_key) DO UPDATE SET
                  question=excluded.question,
                  category=excluded.category,
                  answer=excluded.answer,
                  updated_by=excluded.updated_by,
                  updated_at=excluded.updated_at,
                  status=excluded.status,
                  source=excluded.source,
                  sources_json=excluded.sources_json
                """,
                (
                    question_key.strip(),
                    question.strip(),
                    category.strip() or "other",
                    answer.strip(),
                    updated_by.strip(),
                    now,
                    status,
                    source,
                    sources_json,
                ),
            )
    return {
        "question_key": question_key.strip(),
        "question": question.strip(),
        "category": category.strip() or "other",
        "answer": answer.strip(),
        "updated_by": updated_by.strip(),
        "updated_at": now,
        "status": status,
        "source": source,
        "sources": sources or [],
    }


def delete_qa_answer(question_key: str) -> bool:
    init_db()
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM qa_answers WHERE question_key = ?", (question_key.strip(),))
    return cur.rowcount > 0
