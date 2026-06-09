"""Unit tests for support ticket orchestration."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from support_ticket_service import (
    SupportTicketPayload,
    _validate_payload,
    payload_from_dict,
    ticket_success_message,
)


class SupportTicketValidationTests(unittest.TestCase):
    def test_validate_complete_payload(self) -> None:
        p = SupportTicketPayload(
            dealership_name="Sunrise Ford",
            first_name="Jane",
            last_name="Dealer",
            email="jane@dealer.com",
            phone="+15551234567",
            issue_summary="Cannot log in",
        )
        self.assertIsNone(_validate_payload(p))

    def test_validate_missing_email(self) -> None:
        p = SupportTicketPayload(
            dealership_name="Sunrise Ford",
            first_name="Jane",
            last_name="Dealer",
            email="not-an-email",
            phone="+15551234567",
            issue_summary="Help",
        )
        self.assertIn("email", _validate_payload(p) or "")

    def test_payload_from_dict_resolved_string(self) -> None:
        p = payload_from_dict(
            {
                "dealership_name": "Acme",
                "first_name": "A",
                "last_name": "B",
                "email": "a@b.com",
                "phone": "5551234567",
                "issue_summary": "Test",
                "resolved": "true",
            }
        )
        self.assertTrue(p.resolved)
        self.assertEqual(p.phone, "+15551234567")

    def test_ticket_success_message_matches_outcome(self) -> None:
        self.assertIn("resolved", ticket_success_message(True))
        self.assertIn("follow up", ticket_success_message(False))

    def test_hubspot_create_requires_explicit_live_flag(self) -> None:
        from hubspot_ticket_create import hubspot_ticket_create_configured

        env = {
            "HUBSPOT_PRIVATE_APP_TOKEN": "pat-test",
            "HUBSPOT_NEW_TICKET_PIPELINE_ID": "0",
            "HUBSPOT_NEW_TICKET_STAGE_ID": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(hubspot_ticket_create_configured())

        env["SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE"] = "1"
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(hubspot_ticket_create_configured())


class SupportTicketServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_hubspot_create_blocked_without_live_flag(self) -> None:
        from hubspot_ticket_create import create_hubspot_support_ticket

        env = {
            "HUBSPOT_PRIVATE_APP_TOKEN": "pat-test",
            "HUBSPOT_NEW_TICKET_PIPELINE_ID": "0",
            "HUBSPOT_NEW_TICKET_STAGE_ID": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            result = await create_hubspot_support_ticket(
                dealership_name="Victory Motors",
                first_name="Sam",
                last_name="Lee",
                email="sam@victory.com",
                phone="+15559876543",
                issue_summary="Need help",
            )

        self.assertFalse(result.get("ok"))
        self.assertIn("SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE", str(result.get("error")))

    async def test_idempotent_session_ticket(self) -> None:
        from support_tools import SupportSession

        session = SupportSession(call_id="chat-test-1", channel="chat", ticket_created=False)
        with patch(
            "support_dashboard_store.get_ticket_for_session",
            return_value={
                "id": 9,
                "hubspot_ticket_id": "999",
                "ticket_url": "https://app.hubspot.com/contacts/3355079/ticket/999",
            },
        ):
            from support_ticket_service import create_and_notify_ticket

            result = await create_and_notify_ticket(
                {
                    "dealership_name": "Acme",
                    "first_name": "A",
                    "last_name": "B",
                    "email": "a@b.com",
                    "phone": "+15551234567",
                    "issue_summary": "Test",
                },
                session=session,
            )
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("already_exists"))

    @patch.dict(
        os.environ,
        {
            "HUBSPOT_PRIVATE_APP_TOKEN": "pat-test",
            "HUBSPOT_NEW_TICKET_PIPELINE_ID": "0",
            "HUBSPOT_NEW_TICKET_STAGE_ID": "1",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_SUPPORT_CHANNEL_ID": "C123",
        },
    )
    @patch("support_ticket_service.hubspot_ticket_create_configured", return_value=True)
    @patch("support_ticket_service.slack_ticket_notify_configured", return_value=True)
    @patch("support_ticket_service.post_new_support_ticket_alert", return_value=True)
    @patch("support_dashboard_store.record_support_ticket", return_value={"ticket_id": 1})
    @patch("support_dashboard_store.update_session_ticket_state")
    @patch("support_dashboard_store.get_ticket_for_session", return_value=None)
    @patch(
        "support_ticket_service.create_hubspot_support_ticket",
        new_callable=AsyncMock,
        return_value={
            "ok": True,
            "hubspot_ticket_id": "42",
            "hubspot_contact_id": "7",
            "ticket_url": "https://app.hubspot.com/contacts/3355079/ticket/42",
        },
    )
    async def test_create_full_flow(self, *_mocks: object) -> None:
        from support_tools import SupportSession
        from support_ticket_service import create_and_notify_ticket

        session = SupportSession(call_id="voice-1", channel="browser_voice")
        result = await create_and_notify_ticket(
            {
                "dealership_name": "Victory Motors",
                "first_name": "Sam",
                "last_name": "Lee",
                "email": "sam@victory.com",
                "phone": "5559876543",
                "issue_summary": "MarketPoster question",
                "resolved": True,
            },
            session=session,
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("hubspot_ticket_id"), "42")
        self.assertIn("resolved", str(result.get("message")))
        self.assertTrue(session.ticket_created)
        self.assertTrue(session.resolved)

    @patch("support_ticket_service.hubspot_ticket_create_configured", return_value=False)
    @patch("support_ticket_service.slack_ticket_notify_configured", return_value=False)
    @patch("support_dashboard_store.record_support_ticket", return_value={"ticket_id": 12})
    @patch("support_dashboard_store.update_session_ticket_state")
    @patch("support_dashboard_store.get_ticket_for_session", return_value=None)
    async def test_create_without_integrations_records_local_fallback(self, *_mocks: object) -> None:
        from support_tools import SupportSession
        from support_ticket_service import create_and_notify_ticket

        session = SupportSession(call_id="manual-1", channel="manual_form")
        result = await create_and_notify_ticket(
            {
                "dealership_name": "Victory Motors",
                "first_name": "Sam",
                "last_name": "Lee",
                "email": "sam@victory.com",
                "phone": "5559876543",
                "issue_summary": "Need billing follow-up",
                "resolved": False,
            },
            session=session,
        )

        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("hubspot_configured"))
        self.assertEqual(result.get("local_ticket_id"), 12)
        self.assertIn("follow up", str(result.get("message")))
        self.assertTrue(session.ticket_created)
        self.assertFalse(session.resolved)


if __name__ == "__main__":
    unittest.main()
