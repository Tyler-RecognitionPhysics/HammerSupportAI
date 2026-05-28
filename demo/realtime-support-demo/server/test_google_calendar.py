"""Unit tests for Google Calendar scheduling helpers."""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from google_calendar import (
    book_appointment,
    calendar_configured,
    check_availability,
    format_book_appointment_result,
    format_check_availability_result,
    parse_appointment_start,
    parse_date,
    parse_time,
    rep_calendar_email,
    resolve_timezone,
)


class GoogleCalendarParsingTests(unittest.TestCase):
    def test_rep_calendar_email_defaults_to_agreement_sender(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rep_calendar_email(), "hannah@hammer-corp.com")

    def test_rep_calendar_email_env_override(self) -> None:
        with patch.dict(
            os.environ,
            {"HAMMER_AGREEMENT_SENDER_EMAIL": "sales@hammer-corp.com"},
            clear=True,
        ):
            self.assertEqual(rep_calendar_email(), "sales@hammer-corp.com")

    def test_resolve_timezone_aliases(self) -> None:
        self.assertEqual(resolve_timezone("Central"), "America/Chicago")
        self.assertEqual(resolve_timezone("ET"), "America/New_York")

    def test_parse_time_12h_and_24h(self) -> None:
        self.assertEqual(parse_time("2pm"), (14, 0))
        self.assertEqual(parse_time("2:30 PM"), (14, 30))
        self.assertEqual(parse_time("14:00"), (14, 0))

    def test_parse_date_iso_and_weekday(self) -> None:
        tz = ZoneInfo("America/Chicago")
        ref = datetime(2026, 5, 26, 10, 0, tzinfo=tz)
        self.assertEqual(parse_date("2026-05-28", tz, ref=ref).isoformat(), "2026-05-28")
        self.assertEqual(parse_date("thursday", tz, ref=ref).isoformat(), "2026-05-28")

    def test_parse_appointment_start(self) -> None:
        tz = ZoneInfo("America/Chicago")
        ref = datetime(2026, 5, 26, 10, 0, tzinfo=tz)
        start, zone = parse_appointment_start("2026-05-28", "2pm", "Central")
        self.assertEqual(zone.key, tz.key)
        self.assertEqual(start.hour, 14)
        self.assertEqual(start.date().isoformat(), "2026-05-28")
        with self.assertRaises(ValueError):
            parse_appointment_start("2026-05-20", "2pm", "Central")


class GoogleCalendarAvailabilityTests(unittest.TestCase):
    def test_check_availability_when_not_configured(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = check_availability("2026-05-28", "2pm", "Central")
        self.assertFalse(result["configured"])
        self.assertIn("not configured", format_check_availability_result(result))

    @patch("google_calendar._slot_is_free", return_value=True)
    @patch("google_calendar.calendar_configured", return_value=True)
    def test_check_availability_open_slot(self, _configured: MagicMock, _free: MagicMock) -> None:
        result = check_availability("2026-05-28", "2pm", "Central")
        self.assertTrue(result["available"])
        self.assertIn("available", format_check_availability_result(result))

    @patch("google_calendar._find_alternatives", return_value=["Thursday, May 28 at 3:00 PM CDT"])
    @patch("google_calendar._slot_is_free", return_value=False)
    @patch("google_calendar.calendar_configured", return_value=True)
    def test_check_availability_busy_slot(
        self,
        _configured: MagicMock,
        _free: MagicMock,
        _alts: MagicMock,
    ) -> None:
        result = check_availability("2026-05-28", "2pm", "Central")
        self.assertFalse(result["available"])
        formatted = format_check_availability_result(result)
        self.assertIn("busy", formatted)
        self.assertIn("3:00 PM", formatted)

    @patch("google_calendar._calendar_service")
    @patch("google_calendar._slot_is_free", return_value=True)
    @patch("google_calendar.calendar_configured", return_value=True)
    def test_book_appointment_creates_event(
        self,
        _configured: MagicMock,
        _free: MagicMock,
        service_factory: MagicMock,
    ) -> None:
        events = MagicMock()
        events.insert.return_value.execute.return_value = {
            "id": "evt123",
            "htmlLink": "https://calendar.google.com/event?eid=evt123",
        }
        service = MagicMock()
        service.events.return_value = events
        service_factory.return_value = service

        result = book_appointment(
            email="buyer@dealer.com",
            date_str="2026-05-28",
            time_str="2pm",
            timezone_str="Central",
            dealership_name="Sunrise Ford",
            selected_plan="Hammer Drive",
        )
        self.assertTrue(result["booked"])
        self.assertIn("booked", format_book_appointment_result(result).lower())
        insert_kwargs = events.insert.call_args.kwargs
        self.assertEqual(insert_kwargs["calendarId"], "hannah@hammer-corp.com")
        if result.get("invite_sent"):
            self.assertEqual(insert_kwargs["sendUpdates"], "all")
            attendee_emails = {item["email"] for item in insert_kwargs["body"]["attendees"]}
            self.assertIn("buyer@dealer.com", attendee_emails)
        else:
            self.assertEqual(insert_kwargs["sendUpdates"], "none")
            self.assertNotIn("attendees", insert_kwargs["body"])
        self.assertIn("hannah@hammer-corp.com", insert_kwargs["calendarId"])

    @patch("google_calendar._calendar_service")
    @patch("google_calendar.calendar_configured", return_value=True)
    def test_list_upcoming_events(self, _cfg: MagicMock, mock_service: MagicMock) -> None:
        mock_service.return_value.events.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Hammer walkthrough — Demo Motors",
                    "start": {"dateTime": "2026-05-28T15:00:00-05:00"},
                    "end": {"dateTime": "2026-05-28T15:30:00-05:00"},
                    "htmlLink": "https://calendar.google.com/event?eid=1",
                    "attendees": [{"email": "buyer@dealer.com"}],
                }
            ]
        }
        from google_calendar import list_upcoming_events

        result = list_upcoming_events(days=7)
        self.assertTrue(result["configured"])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["summary"], "Hammer walkthrough — Demo Motors")
        self.assertIn("buyer@dealer.com", result["events"][0]["attendees"])


if __name__ == "__main__":
    unittest.main()
