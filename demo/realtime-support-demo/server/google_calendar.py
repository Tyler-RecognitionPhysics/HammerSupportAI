"""Google Calendar availability checks and rep walkthrough bookings."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_log = logging.getLogger(__name__)

DEFAULT_REP_EMAIL = "hannah@hammer-corp.com"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_DURATION_MIN = 30
SCOPES = ("https://www.googleapis.com/auth/calendar",)

_TZ_ALIASES: dict[str, str] = {
    "central": "America/Chicago",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "chicago": "America/Chicago",
    "austin": "America/Chicago",
    "eastern": "America/New_York",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "pacific": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "mountain": "America/Denver",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
}

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$",
    re.I,
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def rep_calendar_email() -> str:
    """Calendar owned by the same inbox that sends Hammer agreement emails."""
    return (
        os.environ.get("HAMMER_AGREEMENT_SENDER_EMAIL", "").strip()
        or os.environ.get("GOOGLE_CALENDAR_REP_CALENDAR_ID", "").strip()
        or DEFAULT_REP_EMAIL
    )


def default_timezone() -> str:
    raw = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", DEFAULT_TIMEZONE).strip()
    return raw or DEFAULT_TIMEZONE


def appointment_duration_minutes() -> int:
    try:
        return max(15, min(120, int(os.environ.get("GOOGLE_CALENDAR_APPT_DURATION_MINUTES", "30"))))
    except ValueError:
        return DEFAULT_DURATION_MIN


def calendar_configured() -> bool:
    return _credentials_info() is not None


def resolve_timezone(raw: str | None) -> str:
    text = (raw or "").strip().lower().replace("_", " ")
    if not text:
        return default_timezone()
    if "/" in text:
        try:
            ZoneInfo(text)
            return text
        except ZoneInfoNotFoundError:
            pass
    compact = re.sub(r"[^a-z]", "", text)
    if compact in _TZ_ALIASES:
        return _TZ_ALIASES[compact]
    for key, zone in _TZ_ALIASES.items():
        if key in compact:
            return zone
    return default_timezone()


def _credentials_info() -> dict[str, Any] | None:
    raw = os.environ.get("GOOGLE_CALENDAR_CREDENTIALS_JSON", "").strip()
    if not raw:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception as exc:
                _log.warning("google calendar credentials file unreadable: %s", exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if os.path.isfile(raw):
            try:
                with open(raw, encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception as exc:
                _log.warning("google calendar credentials path unreadable: %s", exc)
        _log.warning("GOOGLE_CALENDAR_CREDENTIALS_JSON is not valid JSON")
        return None


def _delegated_user() -> str | None:
    raw = os.environ.get("GOOGLE_CALENDAR_DELEGATED_USER", "").strip()
    return raw or None


@lru_cache(maxsize=1)
def _calendar_service():
    info = _credentials_info()
    if not info:
        raise RuntimeError("Google Calendar is not configured")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    subject = _delegated_user()
    if subject:
        creds = creds.with_subject(subject)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def parse_date(date_str: str, tz: ZoneInfo, ref: datetime | None = None) -> date:
    text = (date_str or "").strip().lower()
    if not text:
        raise ValueError("date is required")
    if _ISO_DATE_RE.match(text):
        return date.fromisoformat(text)

    ref = ref or datetime.now(tz)
    today = ref.date()
    if text in {"today", "tonight"}:
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    for index, weekday in enumerate(_WEEKDAYS):
        if weekday in text or weekday[:3] in text.split():
            days_ahead = (index - today.weekday()) % 7
            if "next" in text:
                days_ahead = days_ahead or 7
            elif days_ahead == 0 and text != weekday and weekday[:3] not in text.split():
                days_ahead = 7
            return today + timedelta(days=days_ahead)

    raise ValueError(f"could not parse date: {date_str!r}")


def parse_time(time_str: str) -> tuple[int, int]:
    text = (time_str or "").strip().lower()
    if not text:
        raise ValueError("time is required")

    match = _TIME_RE.match(text)
    if not match:
        twenty_four = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text)
        if twenty_four:
            hour = int(twenty_four.group(1))
            minute = int(twenty_four.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        raise ValueError(f"could not parse time: {time_str!r}")

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "")
    if meridiem.startswith("p") and hour != 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    elif not meridiem and hour <= 12 and "pm" not in text and hour < 8:
        pass
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time: {time_str!r}")
    return hour, minute


def parse_appointment_start(date_str: str, time_str: str, timezone_str: str | None) -> tuple[datetime, ZoneInfo]:
    zone_name = resolve_timezone(timezone_str)
    tz = ZoneInfo(zone_name)
    day = parse_date(date_str, tz)
    hour, minute = parse_time(time_str)
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if start <= datetime.now(tz) - timedelta(minutes=5):
        raise ValueError("requested time is in the past")
    return start, tz


def format_display(start: datetime, tz: ZoneInfo) -> str:
    local = start.astimezone(tz)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%A, %B')} {local.day} at {hour}:{local.strftime('%M %p %Z')}"


def _busy_intervals(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    service = _calendar_service()
    calendar_id = rep_calendar_email()
    body = {
        "timeMin": start.astimezone(ZoneInfo("UTC")).isoformat(),
        "timeMax": end.astimezone(ZoneInfo("UTC")).isoformat(),
        "timeZone": str(start.tzinfo),
        "items": [{"id": calendar_id}],
    }
    response = service.freebusy().query(body=body).execute()
    busy = response.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    intervals: list[tuple[datetime, datetime]] = []
    for block in busy:
        begin = datetime.fromisoformat(block["start"].replace("Z", "+00:00"))
        finish = datetime.fromisoformat(block["end"].replace("Z", "+00:00"))
        intervals.append((begin, finish))
    return intervals


def _slot_is_free(start: datetime, duration_minutes: int) -> bool:
    end = start + timedelta(minutes=duration_minutes)
    for busy_start, busy_end in _busy_intervals(start - timedelta(minutes=1), end + timedelta(minutes=1)):
        if busy_start < end and busy_end > start:
            return False
    return True


def _find_alternatives(
    requested: datetime,
    tz: ZoneInfo,
    duration_minutes: int,
    *,
    limit: int = 3,
) -> list[str]:
    alternatives: list[str] = []
    cursor = requested
    day_end = datetime(
        requested.year,
        requested.month,
        requested.day,
        17,
        0,
        tzinfo=tz,
    )
    if cursor.hour >= 17:
        day_end = cursor.replace(hour=17, minute=0, second=0, microsecond=0)

    while len(alternatives) < limit:
        if cursor >= day_end:
            next_day = (requested.date() + timedelta(days=1))
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            cursor = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=tz)
            requested = cursor
            day_end = cursor.replace(hour=17, minute=0, second=0, microsecond=0)
        if _slot_is_free(cursor, duration_minutes):
            alternatives.append(format_display(cursor, tz))
        cursor += timedelta(minutes=30)
        if cursor - requested > timedelta(days=7):
            break
    return alternatives


def check_availability(
    date_str: str,
    time_str: str,
    timezone_str: str | None = None,
    *,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    duration = duration_minutes or appointment_duration_minutes()
    if not calendar_configured():
        return {
            "configured": False,
            "available": True,
            "note": "calendar not configured",
        }
    try:
        start, tz = parse_appointment_start(date_str, time_str, timezone_str)
    except ValueError as exc:
        return {"configured": True, "available": False, "error": str(exc)}

    available = _slot_is_free(start, duration)
    display = format_display(start, tz)
    result: dict[str, Any] = {
        "configured": True,
        "available": available,
        "display": display,
        "start_iso": start.isoformat(),
        "timezone": str(tz),
    }
    if not available:
        result["alternatives"] = _find_alternatives(start, tz, duration)
    return result


def book_appointment(
    *,
    email: str,
    date_str: str,
    time_str: str,
    timezone_str: str | None = None,
    name: str = "",
    dealership_name: str = "",
    selected_plan: str = "",
    notes: str = "",
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    duration = duration_minutes or appointment_duration_minutes()
    if not calendar_configured():
        return {
            "configured": False,
            "booked": False,
            "note": "calendar not configured",
        }

    prospect = (email or "").strip().lower()
    if not prospect or "@" not in prospect:
        return {"configured": True, "booked": False, "error": "valid email is required"}

    try:
        start, tz = parse_appointment_start(date_str, time_str, timezone_str)
    except ValueError as exc:
        return {"configured": True, "booked": False, "error": str(exc)}

    if not _slot_is_free(start, duration):
        return {
            "configured": True,
            "booked": False,
            "error": "that time is no longer available",
            "alternatives": _find_alternatives(start, tz, duration),
        }

    end = start + timedelta(minutes=duration)
    rep_email = rep_calendar_email()
    label = (dealership_name or name or prospect).strip()
    summary = f"Hammer walkthrough — {label}" if label else "Hammer rep walkthrough"
    description_lines = [
        "Booked by Hannah (Hammer voice AI).",
        f"Prospect: {prospect}",
    ]
    if name.strip():
        description_lines.append(f"Name: {name.strip()}")
    if dealership_name.strip():
        description_lines.append(f"Dealership: {dealership_name.strip()}")
    if selected_plan.strip():
        description_lines.append(f"Plan: {selected_plan.strip()}")
    if notes.strip():
        description_lines.append(f"Notes: {notes.strip()}")

    delegated = bool(_delegated_user())
    event_body: dict[str, Any] = {
        "summary": summary,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
        "reminders": {"useDefault": True},
    }
    send_updates = "none"
    if delegated:
        event_body["attendees"] = [
            {"email": rep_email, "responseStatus": "accepted"},
            {"email": prospect},
        ]
        send_updates = "all"

    service = _calendar_service()
    created = (
        service.events()
        .insert(
            calendarId=rep_email,
            body=event_body,
            sendUpdates=send_updates,
        )
        .execute()
    )
    link = created.get("htmlLink", "")
    display = format_display(start, tz)
    return {
        "configured": True,
        "booked": True,
        "display": display,
        "event_link": link,
        "event_id": created.get("id", ""),
        "email": prospect,
        "invite_sent": delegated,
    }


def format_check_availability_result(result: dict[str, Any]) -> str:
    if not result.get("configured"):
        return (
            "calendar not configured — note the requested time for the rep verbally; "
            "do not promise a calendar invite was sent."
        )
    if result.get("error"):
        return f"error — {result['error']}"
    if result.get("available"):
        return (
            f"available — {result['display']} is open. "
            "Call book_appointment with the same date/time to send a calendar invite."
        )
    alternatives = result.get("alternatives") or []
    if alternatives:
        return (
            f"busy — {result['display']} is taken. "
            f"Suggest: {'; '.join(alternatives[:3])}"
        )
    return f"busy — {result['display']} is taken. Ask for another time."


def format_book_appointment_result(result: dict[str, Any]) -> str:
    if not result.get("configured"):
        return (
            "calendar not configured — tell the caller you'll note the time for the rep; "
            "do not say a calendar invite was sent."
        )
    if result.get("error"):
        alts = result.get("alternatives") or []
        if alts:
            return f"error — {result['error']}. Suggest: {'; '.join(alts[:3])}"
        return f"error — {result['error']}"
    if result.get("booked"):
        invite = result.get("email") or "their email"
        if result.get("invite_sent"):
            return (
                f"ok — booked {result.get('display', 'appointment')} — "
                f"calendar invite sent to {invite}."
            )
        return (
            f"ok — booked {result.get('display', 'appointment')} on the rep calendar. "
            f"Prospect email {invite} is saved on the event — no auto-invite until "
            "GOOGLE_CALENDAR_DELEGATED_USER is configured in Workspace."
        )
    return "error — appointment was not booked"


def _parse_event_time(raw: dict[str, Any], tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Return (datetime, all_day)."""
    if not isinstance(raw, dict):
        return None, False
    if raw.get("dateTime"):
        try:
            dt = datetime.fromisoformat(str(raw["dateTime"]).replace("Z", "+00:00"))
            return dt.astimezone(tz), False
        except ValueError:
            return None, False
    if raw.get("date"):
        try:
            day = date.fromisoformat(str(raw["date"]))
            return datetime(day.year, day.month, day.day, tzinfo=tz), True
        except ValueError:
            return None, False
    return None, False


def list_upcoming_events(*, days: int = 14, max_results: int = 50) -> dict[str, Any]:
    """Upcoming rep calendar events for the admin dashboard."""
    tz_name = default_timezone()
    if not calendar_configured():
        return {
            "configured": False,
            "calendar_id": rep_calendar_email(),
            "timezone": tz_name,
            "events": [],
            "note": "Google Calendar credentials not configured",
        }

    days = max(1, min(days, 60))
    max_results = max(1, min(max_results, 100))
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    calendar_id = rep_calendar_email()

    try:
        service = _calendar_service()
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now.astimezone(ZoneInfo("UTC")).isoformat(),
                timeMax=(now + timedelta(days=days)).astimezone(ZoneInfo("UTC")).isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as exc:
        _log.exception("google calendar list events failed")
        return {
            "configured": True,
            "calendar_id": calendar_id,
            "timezone": tz_name,
            "events": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    events: list[dict[str, Any]] = []
    for item in response.get("items") or []:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start") or {}
        end_raw = item.get("end") or {}
        start_dt, all_day = _parse_event_time(start_raw, tz)
        end_dt, _ = _parse_event_time(end_raw, tz)
        attendees = []
        for att in item.get("attendees") or []:
            if isinstance(att, dict) and att.get("email"):
                attendees.append(str(att["email"]))
        events.append(
            {
                "id": str(item.get("id") or ""),
                "summary": str(item.get("summary") or "(No title)"),
                "description": str(item.get("description") or "")[:500],
                "start": start_dt.isoformat() if start_dt else "",
                "end": end_dt.isoformat() if end_dt else "",
                "all_day": all_day,
                "html_link": str(item.get("htmlLink") or ""),
                "status": str(item.get("status") or ""),
                "attendees": attendees,
                "location": str(item.get("location") or ""),
            }
        )

    return {
        "configured": True,
        "calendar_id": calendar_id,
        "timezone": tz_name,
        "range_days": days,
        "events": events,
        "fetched_at": now.isoformat(timespec="seconds"),
    }
