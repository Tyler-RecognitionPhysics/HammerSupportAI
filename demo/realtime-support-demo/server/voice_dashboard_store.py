"""Local SQLite store for voice dashboard — call records, events, settings overrides."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = _SERVER_DIR / ".data" / "voice_dashboard.sqlite"

_db_lock = Lock()
_active_sessions: dict[str, dict[str, Any]] = {}

SETTING_KEYS = (
    "pen_prompt",
    "hammer_prompt",
    "pen_close_prompt",
    "chat_model",
)


def _is_serverless() -> bool:
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _db_path() -> Path:
    override = os.environ.get("REALTIME_SALES_VOICE_DASHBOARD_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        return Path("/tmp/realtime-sales-demo/voice_dashboard.sqlite")
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
                CREATE TABLE IF NOT EXISTS call_records (
                    call_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL DEFAULT 'voice',
                    call_direction TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    ended_at TEXT NOT NULL DEFAULT '',
                    values_json TEXT NOT NULL DEFAULT '{}',
                    session_log_json TEXT NOT NULL DEFAULT '[]',
                    interaction_summary TEXT NOT NULL DEFAULT '',
                    capture_lead_fired INTEGER NOT NULL DEFAULT 0,
                    agreement_email_sent INTEGER NOT NULL DEFAULT 0,
                    i_approve_approved INTEGER NOT NULL DEFAULT 0,
                    account_created INTEGER NOT NULL DEFAULT 0,
                    pen_challenge_skipped INTEGER NOT NULL DEFAULT 0,
                    pen_hammer_close_active INTEGER NOT NULL DEFAULT 0,
                    summary_sent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_call_records_started
                    ON call_records(started_at DESC);
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_call_events_call_id
                    ON call_events(call_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_call_events_created
                    ON call_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT 'null',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )


def _acc_to_row(acc: Any) -> dict[str, Any]:
    return {
        "call_id": acc.call_id or "",
        "channel": acc.channel or "voice",
        "call_direction": acc.call_direction or "",
        "started_at": acc.started_at or "",
        "ended_at": acc.ended_at or "",
        "values_json": json.dumps(acc.values or {}),
        "session_log_json": json.dumps(acc.session_log or []),
        "interaction_summary": acc.interaction_summary or "",
        "capture_lead_fired": int(bool(acc.capture_lead_fired)),
        "agreement_email_sent": int(bool(acc.agreement_email_sent)),
        "i_approve_approved": int(bool(acc.i_approve_approved)),
        "account_created": int(bool(acc.account_created)),
        "pen_challenge_skipped": int(bool(acc.pen_challenge_skipped)),
        "pen_hammer_close_active": int(bool(acc.pen_hammer_close_active)),
        "summary_sent": int(bool(acc.summary_sent)),
        "updated_at": _utc_now(),
    }


def _row_to_call(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "call_id": row["call_id"],
        "channel": row["channel"],
        "call_direction": row["call_direction"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "values": json.loads(row["values_json"] or "{}"),
        "session_log": json.loads(row["session_log_json"] or "[]"),
        "interaction_summary": row["interaction_summary"],
        "capture_lead_fired": bool(row["capture_lead_fired"]),
        "agreement_email_sent": bool(row["agreement_email_sent"]),
        "i_approve_approved": bool(row["i_approve_approved"]),
        "account_created": bool(row["account_created"]),
        "pen_challenge_skipped": bool(row["pen_challenge_skipped"]),
        "pen_hammer_close_active": bool(row["pen_hammer_close_active"]),
        "summary_sent": bool(row["summary_sent"]),
        "updated_at": row["updated_at"],
    }


def upsert_call_record(acc: Any) -> None:
    if not acc.call_id:
        return
    init_db()
    row = _acc_to_row(acc)
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO call_records (
                    call_id, channel, call_direction, started_at, ended_at,
                    values_json, session_log_json, interaction_summary,
                    capture_lead_fired, agreement_email_sent, i_approve_approved,
                    account_created, pen_challenge_skipped, pen_hammer_close_active,
                    summary_sent, updated_at
                ) VALUES (
                    :call_id, :channel, :call_direction, :started_at, :ended_at,
                    :values_json, :session_log_json, :interaction_summary,
                    :capture_lead_fired, :agreement_email_sent, :i_approve_approved,
                    :account_created, :pen_challenge_skipped, :pen_hammer_close_active,
                    :summary_sent, :updated_at
                )
                ON CONFLICT(call_id) DO UPDATE SET
                    channel=excluded.channel,
                    call_direction=excluded.call_direction,
                    started_at=CASE WHEN excluded.started_at != '' THEN excluded.started_at ELSE call_records.started_at END,
                    ended_at=CASE WHEN excluded.ended_at != '' THEN excluded.ended_at ELSE call_records.ended_at END,
                    values_json=excluded.values_json,
                    session_log_json=excluded.session_log_json,
                    interaction_summary=excluded.interaction_summary,
                    capture_lead_fired=excluded.capture_lead_fired,
                    agreement_email_sent=excluded.agreement_email_sent,
                    i_approve_approved=excluded.i_approve_approved,
                    account_created=excluded.account_created,
                    pen_challenge_skipped=excluded.pen_challenge_skipped,
                    pen_hammer_close_active=excluded.pen_hammer_close_active,
                    summary_sent=excluded.summary_sent,
                    updated_at=excluded.updated_at
                """,
                row,
            )


def append_call_event(
    *,
    call_id: str = "",
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO call_events (call_id, event_type, detail_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (call_id or "", event_type, json.dumps(detail or {}), _utc_now()),
            )


def register_active_session(call_id: str, meta: dict[str, Any]) -> None:
    if not call_id:
        return
    _active_sessions[call_id] = {**meta, "started_at": meta.get("started_at") or _utc_now()}


def update_active_session(call_id: str, patch: dict[str, Any]) -> None:
    if not call_id:
        return
    if call_id not in _active_sessions:
        register_active_session(call_id, patch)
        return
    _active_sessions[call_id] = {**_active_sessions[call_id], **patch}


def find_account_url_by_email(email: str) -> str | None:
    """Query local SQLite database for a call with the given email that has an account_url."""
    if not email:
        return None
    try:
        init_db()
        email_clean = email.strip().lower()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT values_json FROM call_records WHERE account_created = 1"
            ).fetchall()
            for r in rows:
                val = json.loads(r["values_json"] or "{}")
                if str(val.get("email") or "").strip().lower() == email_clean:
                    url = val.get("account_url")
                    if url:
                        return url
    except Exception:
        pass
    return None


def update_account_url_by_email(email: str, account_url: str) -> None:
    """Save the account_url into any call_records for this email address."""
    if not email or not account_url:
        return
    try:
        init_db()
        email_clean = email.strip().lower()
        with _db_lock:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT call_id, values_json FROM call_records"
                ).fetchall()
                for r in rows:
                    cid = r["call_id"]
                    val = json.loads(r["values_json"] or "{}")
                    if str(val.get("email") or "").strip().lower() == email_clean:
                        val["account_url"] = account_url
                        conn.execute(
                            "UPDATE call_records SET account_created = 1, values_json = ?, updated_at = ? WHERE call_id = ?",
                            [json.dumps(val), _utc_now(), cid],
                        )
    except Exception:
        pass


def unregister_active_session(call_id: str) -> None:
    _active_sessions.pop(call_id, None)


def list_active_sessions() -> list[dict[str, Any]]:
    return [{"call_id": cid, **meta} for cid, meta in _active_sessions.items()]


def session_log_to_transcript(log: list[str]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in log:
        text = str(line or "").strip()
        if not text:
            continue
        lower = text.lower()
        if lower.startswith("visitor:"):
            turns.append({"role": "user", "message": text.split(":", 1)[1].strip()})
        elif lower.startswith("agent:"):
            turns.append({"role": "assistant", "message": text.split(":", 1)[1].strip()})
    return turns


def list_calls(*, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM call_records
            ORDER BY COALESCE(NULLIF(ended_at, ''), started_at, updated_at) DESC
            LIMIT ? OFFSET ?
            """,
            (max(1, min(limit, 200)), max(0, offset)),
        ).fetchall()
    return [_row_to_call(r) for r in rows]


_OUTCOME_COLUMNS = (
    "capture_lead_fired",
    "agreement_email_sent",
    "i_approve_approved",
    "account_created",
    "pen_challenge_skipped",
    "pen_hammer_close_active",
)


def patch_call_outcomes(call_id: str, call: dict[str, Any]) -> None:
    """Persist enriched funnel flags back to the local DB for a single call.

    Only writes boolean flags that are True — never clears a flag that is already set.
    """
    if not call_id:
        return
    init_db()
    with _db_lock:
        with _connect() as conn:
            set_clauses = ", ".join(
                f"{col} = MAX({col}, ?)" for col in _OUTCOME_COLUMNS
            )
            values = [1 if call.get(col) else 0 for col in _OUTCOME_COLUMNS]
            conn.execute(
                f"UPDATE call_records SET {set_clauses}, updated_at = ? WHERE call_id = ?",
                [*values, _utc_now(), call_id],
            )


def get_call(call_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM call_records WHERE call_id = ?",
            (call_id,),
        ).fetchone()
    if not row:
        return None
    call = _row_to_call(row)
    events = conn_events_for_call(call_id)
    call["events"] = events
    return call


def get_call_record_only(call_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM call_records WHERE call_id = ?",
            (call_id,),
        ).fetchone()
    if not row:
        return None
    return _row_to_call(row)


def conn_events_for_call(call_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, call_id, event_type, detail_json, created_at
            FROM call_events
            WHERE call_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (call_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "call_id": r["call_id"],
            "event_type": r["event_type"],
            "detail": json.loads(r["detail_json"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def recent_events(*, limit: int = 30, include_latency: bool = False) -> list[dict[str, Any]]:
    init_db()
    where = "" if include_latency else "WHERE event_type != 'latency'"
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, call_id, event_type, detail_json, created_at
            FROM call_events
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "call_id": r["call_id"],
            "event_type": r["event_type"],
            "detail": json.loads(r["detail_json"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _since_iso(days: int = 0, hours: int = 0) -> str:
    delta = timedelta(days=days, hours=hours)
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _occurred_at_sql() -> str:
    """SQL expression for when a call occurred — never use updated_at for funnel counts."""
    return "COALESCE(NULLIF(started_at, ''), NULLIF(ended_at, ''))"


def _central_day_bounds_iso() -> tuple[str, str]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Chicago")
    now = datetime.now(timezone.utc).astimezone(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


def _funnel_counts_since(conn: Any, since: str, *, until: str | None = None) -> dict[str, Any]:
    occurred = _occurred_at_sql()
    if until:
        window = f"{occurred} >= ? AND {occurred} < ?"
        params = (since, until)
    else:
        window = f"{occurred} >= ?"
        params = (since,)

    total = conn.execute(
        f"SELECT COUNT(*) FROM call_records WHERE {window}",
        params,
    ).fetchone()[0]
    agreements = conn.execute(
        f"SELECT COUNT(*) FROM call_records WHERE agreement_email_sent = 1 AND {window}",
        params,
    ).fetchone()[0]
    approved = conn.execute(
        f"SELECT COUNT(*) FROM call_records WHERE i_approve_approved = 1 AND {window}",
        params,
    ).fetchone()[0]
    accounts = conn.execute(
        f"SELECT COUNT(*) FROM call_records WHERE account_created = 1 AND {window}",
        params,
    ).fetchone()[0]
    pen_handoff = conn.execute(
        f"SELECT COUNT(*) FROM call_records WHERE pen_hammer_close_active = 1 AND {window}",
        params,
    ).fetchone()[0]
    channels = conn.execute(
        f"""
        SELECT channel, COUNT(*) AS n
        FROM call_records
        WHERE {window}
        GROUP BY channel
        """,
        params,
    ).fetchall()
    return {
        "calls_total": total,
        "agreement_emails": agreements,
        "approvals": approved,
        "accounts_created": accounts,
        "pen_to_hammer": pen_handoff,
        "channels": {r["channel"]: r["n"] for r in channels},
    }


def funnel_stats(*, days: int = 7, hours: int | None = None) -> dict[str, Any]:
    init_db()
    if hours is not None:
        since = _since_iso(hours=hours)
        period_label = f"last_{hours}h"
    else:
        since = _since_iso(days=days)
        period_label = f"last_{days}d"
    with _connect() as conn:
        counts = _funnel_counts_since(conn, since)
    return {
        "period": period_label,
        **counts,
        "active_now": len(_active_sessions),
    }


def funnel_stats_today_central() -> dict[str, Any]:
    init_db()
    start, end = _central_day_bounds_iso()
    with _connect() as conn:
        counts = _funnel_counts_since(conn, start, until=end)
    return {
        "period": "today_central",
        **counts,
        "active_now": len(_active_sessions),
    }


def get_setting(key: str) -> Any:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return json.loads(row["value_json"])


def get_all_settings() -> dict[str, Any]:
    init_db()
    out: dict[str, Any] = {}
    with _connect() as conn:
        rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
    for row in rows:
        out[row["key"]] = json.loads(row["value_json"])
    return out


def set_setting(key: str, value: Any) -> None:
    init_db()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value), _utc_now()),
            )


def set_settings(updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if key in SETTING_KEYS:
            set_setting(key, value)


def clear_settings() -> None:
    init_db()
    with _db_lock:
        with _connect() as conn:
            conn.execute("DELETE FROM settings")
