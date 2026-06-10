"""Customer callback calendar — schedule + read appointments for the support AI.

A "callback" is a time a current Hammer customer asked someone to reach out and
help them with their account. The AI can read the calendar (to avoid conflicts /
confirm a slot) and write to it (schedule a callback). The admin dashboard renders
the same data.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from support_dashboard_store import (
    create_appointment,
    list_appointments,
)

_log = logging.getLogger(__name__)

# Callbacks are booked during Hammer business hours (Central) on 30-minute slots.
_BUSINESS_TZ = ZoneInfo("America/Chicago")
_OPEN_HOUR = 9
_CLOSE_HOUR = 17
_SLOT_MIN = 30
_SEARCH_DAYS = 21  # how far forward to look for the closest open slot


def _central_now() -> datetime:
    return datetime.now(_BUSINESS_TZ)


def _to_central(dt: datetime) -> datetime:
    """Make dt timezone-aware in Central. Naive values are assumed to be Central wall time."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_BUSINESS_TZ)
    return dt.astimezone(_BUSINESS_TZ)


def _in_business_hours(start: datetime, duration_min: int) -> bool:
    if start.weekday() >= 5:  # Sat/Sun
        return False
    open_t = start.replace(hour=_OPEN_HOUR, minute=0, second=0, microsecond=0)
    close_t = start.replace(hour=_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return start >= open_t and (start + timedelta(minutes=duration_min)) <= close_t


def _existing_busy() -> list[tuple[datetime, datetime]]:
    """Return (start, end) Central intervals for appointments that block a slot."""
    busy: list[tuple[datetime, datetime]] = []
    try:
        rows = list_appointments(limit=500)
    except Exception:
        return busy
    for r in rows:
        if str(r.get("status") or "").strip().lower() == "cancelled":
            continue
        dt = _parse_iso(str(r.get("requested_at") or ""))
        if not dt:
            continue
        start = _to_central(dt)
        try:
            dur = int(r.get("duration_min") or 30)
        except (TypeError, ValueError):
            dur = 30
        busy.append((start, start + timedelta(minutes=max(dur, 1))))
    return busy


def _slot_free(start: datetime, duration_min: int, busy: list[tuple[datetime, datetime]]) -> bool:
    end = start + timedelta(minutes=duration_min)
    return not any(s < end and start < e for s, e in busy)


def _resolve_slot(
    requested: datetime, duration_min: int, busy: list[tuple[datetime, datetime]]
) -> tuple[datetime, bool]:
    """Book the requested time if it's open and in-hours; otherwise return the closest open slot.

    Returns (final_start_central, adjusted) where adjusted is True when we had to move
    the customer off their requested time.
    """
    requested = _to_central(requested)
    now = _central_now()
    grace = now - timedelta(minutes=1)

    # Honor the exact requested time when it is in the future, within business hours, and open.
    if requested >= grace and _in_business_hours(requested, duration_min) and _slot_free(requested, duration_min, busy):
        return requested, False

    # Otherwise scan 30-minute business-hour slots and pick the one closest to what they asked for.
    best: datetime | None = None
    best_diff: float | None = None
    start_day = min(requested, now).date()
    for day_offset in range(_SEARCH_DAYS):
        day = start_day + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        slot = datetime.combine(day, time(_OPEN_HOUR, 0), tzinfo=_BUSINESS_TZ)
        last_start = datetime.combine(day, time(_CLOSE_HOUR, 0), tzinfo=_BUSINESS_TZ) - timedelta(minutes=duration_min)
        while slot <= last_start:
            if slot >= grace and _slot_free(slot, duration_min, busy):
                diff = abs((slot - requested).total_seconds())
                if best_diff is None or diff < best_diff or (diff == best_diff and slot < best):  # type: ignore[operator]
                    best, best_diff = slot, diff
            slot += timedelta(minutes=_SLOT_MIN)

    if best is not None:
        return best, True
    # Nothing open in the horizon — fall back to the requested time rather than dropping it.
    return requested, False


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

    # Phone is optional — a requested day/time should always make it onto the
    # calendar even if the customer won't share a number (a rep can reach them
    # via their Hammer account email).
    missing = [
        label
        for label, val in (
            ("dealership_name", dealership_name),
            ("first_name", first_name),
            ("last_name", last_name),
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
    original_label = requested_label_in or _human_label(dt, fallback=requested_time)

    # Book the customer's requested time when it's open; otherwise move them to the
    # closest available in-hours slot and surface that change so the AI can confirm it.
    adjusted = False
    final_dt = dt
    if dt is not None:
        final_dt, adjusted = _resolve_slot(dt, duration_min, _existing_busy())

    requested_at = _normalize_dt_to_storage(final_dt) if final_dt else ""
    if adjusted:
        label = _human_label(final_dt)
    else:
        label = requested_label_in or _human_label(final_dt, fallback=requested_time)

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

    # Fire-and-forget: never make the customer wait on the Slack round trip.
    try:
        import threading

        from support_ticket_slack import post_callback_scheduled_alert

        threading.Thread(
            target=post_callback_scheduled_alert,
            kwargs=dict(
                dealership_name=dealership_name,
                contact_name=f"{first_name} {last_name}".strip(),
                phone=phone,
                email=email,
                when_label=label,
                reason=reason,
                channel=channel,
                source="ai",
            ),
            daemon=True,
        ).start()
    except Exception:
        _log.exception("callback slack alert failed")

    confirm_time = label or "the requested time"
    if adjusted:
        message = (
            f"The {original_label or 'requested'} time wasn't open, so I booked the closest "
            f"available slot: {confirm_time}. Confirm this works for the customer (offer to "
            f"find another time if not). A Hammer specialist will reach out then."
        )
    else:
        message = (
            f"Your callback is scheduled for {confirm_time}. A Hammer specialist will "
            f"reach out then to help with your account."
        )
    return {
        "ok": True,
        "appointment_id": appt["id"],
        "scheduled_for": appt["requested_at"],
        "when_label": label,
        "requested_label": original_label,
        "adjusted": adjusted,
        "message": message,
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
