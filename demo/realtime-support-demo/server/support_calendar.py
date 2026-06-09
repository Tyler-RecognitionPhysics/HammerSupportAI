"""Customer callback calendar — schedule + read appointments for the support AI.

A "callback" is a time a current Hammer customer asked someone to reach out and
help them with their account. The AI can read the calendar (to avoid conflicts /
confirm a slot) and write to it (schedule a callback). The admin dashboard renders
the same data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from support_dashboard_store import (
    create_appointment,
    list_appointments,
)

_log = logging.getLogger(__name__)


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 datetime (with or without timezone offset / trailing Z)."""
    raw = (value or "").strip()
    if not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    # Allow a bare date → treat as that day at 09:00 local-naive.
    try:
        if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
            candidate = candidate + "T09:00:00"
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _human_label(dt: datetime | None, fallback: str = "") -> str:
    if not dt:
        return fallback.strip()
    # %-d/%-I are not portable (fail on Windows); format then strip leading zeros.
    try:
        return dt.strftime("%a %b %d, %Y · %I:%M %p").replace(" 0", " ")
    except Exception:
        return fallback.strip()


def _normalize_dt_to_storage(dt: datetime) -> str:
    """Store as ISO 8601. Keep offset if present; otherwise assume it is already local wall-time."""
    return dt.isoformat()


def schedule_callback(args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
    """Create a callback appointment from AI-collected fields. Returns a result dict."""
    dealership_name = str(args.get("dealership_name") or "").strip()
    first_name = str(args.get("first_name") or "").strip()
    last_name = str(args.get("last_name") or "").strip()
    email = str(args.get("email") or "").strip()
    phone = str(args.get("phone") or "").strip()
    reason = str(args.get("reason") or args.get("issue_summary") or "").strip()
    requested_time = str(args.get("requested_time") or args.get("requested_time_iso") or "").strip()
    requested_label_in = str(args.get("requested_time_label") or "").strip()
    tz = str(args.get("timezone") or "").strip()
    try:
        duration_min = int(args.get("duration_min") or 30)
    except (TypeError, ValueError):
        duration_min = 30

    missing = [
        label
        for label, val in (
            ("dealership_name", dealership_name),
            ("first_name", first_name),
            ("last_name", last_name),
            ("phone", phone),
            ("requested_time", requested_time or requested_label_in),
            ("reason", reason),
        )
        if not val
    ]
    if missing:
        return {
            "ok": False,
            "error": f"Missing required fields: {', '.join(missing)}",
            "needs": missing,
        }

    dt = _parse_iso(requested_time)
    requested_at = _normalize_dt_to_storage(dt) if dt else ""
    label = requested_label_in or _human_label(dt, fallback=requested_time)

    channel = ""
    session_id = ""
    if session is not None:
        channel = getattr(session, "channel", "") or ""
        session_id = getattr(session, "call_id", "") or ""

    appt = create_appointment(
        requested_at=requested_at,
        duration_min=duration_min,
        dealership_name=dealership_name,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        reason=reason,
        status="requested",
        source="ai",
        channel=channel,
        session_id=session_id,
        timezone=tz,
        requested_label=label,
        notes="",
    )

    try:
        from support_ticket_slack import post_callback_scheduled_alert

        post_callback_scheduled_alert(
            dealership_name=dealership_name,
            contact_name=f"{first_name} {last_name}".strip(),
            phone=phone,
            email=email,
            when_label=label,
            reason=reason,
            channel=channel,
            source="ai",
        )
    except Exception:
        _log.exception("callback slack alert failed")

    confirm_time = label or "the requested time"
    return {
        "ok": True,
        "appointment_id": appt["id"],
        "scheduled_for": appt["requested_at"],
        "when_label": label,
        "message": (
            f"Your callback is scheduled for {confirm_time}. A Hammer specialist will "
            f"reach out then to help with your account."
        ),
        "unparsed_time": not bool(requested_at),
    }


def list_callbacks(
    *,
    start: str = "",
    end: str = "",
    status: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """Read appointments in a window. Used by the AI to avoid conflicts / confirm slots."""
    rows = list_appointments(start=start, end=end, status=status, limit=limit)
    compact = [
        {
            "id": r["id"],
            "when": r["requested_at"] or r["requested_label"],
            "when_label": r["requested_label"] or _human_label(_parse_iso(r["requested_at"])),
            "duration_min": r["duration_min"],
            "dealership": r["dealership_name"],
            "contact": r["contact_name"],
            "phone": r["phone"],
            "reason": r["reason"],
            "status": r["status"],
        }
        for r in rows
    ]
    return {"ok": True, "count": len(compact), "appointments": compact}


def callbacks_for_day(day: str) -> dict[str, Any]:
    """All callbacks on a given YYYY-MM-DD (by requested_at date prefix)."""
    dt = _parse_iso(day)
    if not dt:
        return {"ok": False, "error": "Provide a date as YYYY-MM-DD."}
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return list_callbacks(start=start.isoformat(), end=end.isoformat())
