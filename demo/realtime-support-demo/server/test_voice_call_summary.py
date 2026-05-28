"""Tests for end-of-call voice summary contact gating."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from voice_call_summary import (
    VoiceCallLeadAccumulator,
    build_interaction_summary,
    maybe_post_voice_call_summary,
)


class VoiceCallSummaryContactGateTests(unittest.TestCase):
    def test_no_contact_skipped(self) -> None:
        acc = VoiceCallLeadAccumulator()
        self.assertFalse(acc.has_actionable_contact())

    def test_phone_only(self) -> None:
        acc = VoiceCallLeadAccumulator()
        acc.set_value("phone", "5551234567")
        self.assertTrue(acc.has_actionable_contact())

    def test_email_only(self) -> None:
        acc = VoiceCallLeadAccumulator()
        acc.set_value("email", "buyer@dealer.com")
        self.assertTrue(acc.has_actionable_contact())

    def test_dealership_only(self) -> None:
        acc = VoiceCallLeadAccumulator()
        acc.set_value("dealership_name", "Sunrise Motors")
        self.assertTrue(acc.has_actionable_contact())

    def test_capture_lead_fired_without_fields(self) -> None:
        acc = VoiceCallLeadAccumulator()
        acc.capture_lead_fired = True
        self.assertTrue(acc.has_actionable_contact())

    def test_agreement_email_sent_flag(self) -> None:
        acc = VoiceCallLeadAccumulator()
        acc.agreement_email_sent = True
        self.assertTrue(acc.has_actionable_contact())

    @patch("voice_call_summary.post_voice_call_summary")
    @patch("voice_call_summary.voice_call_summary_webhook_configured", return_value=True)
    def test_maybe_post_with_email_no_phone(
        self, _configured: unittest.mock.MagicMock, post: unittest.mock.MagicMock
    ) -> None:
        acc = VoiceCallLeadAccumulator(call_id="test-1", channel="browser")
        acc.set_value("email", "buyer@dealer.com")
        acc.set_value("dealership_name", "Sunrise Motors")
        posted = maybe_post_voice_call_summary(acc)
        self.assertTrue(posted)
        post.assert_called_once()

    def test_should_post_browser_pen_only(self) -> None:
        acc = VoiceCallLeadAccumulator(call_id="test-2", channel="browser")
        self.assertFalse(acc.has_actionable_contact())
        self.assertTrue(acc.should_post_summary())

    def test_should_post_with_transcript_lines(self) -> None:
        acc = VoiceCallLeadAccumulator(call_id="test-3", channel="elevenlabs_browser")
        acc.append_log("Visitor: I need a blue pen")
        acc.append_log("Agent: Got it.")
        self.assertTrue(acc.should_post_summary())

    @patch("voice_call_summary.voice_call_summary_webhook_configured", return_value=True)
    def test_maybe_post_skipped_empty_sip(self, _configured: unittest.mock.MagicMock) -> None:
        acc = VoiceCallLeadAccumulator(call_id="test-4", channel="sip")
        posted = maybe_post_voice_call_summary(acc)
        self.assertFalse(posted)

    def test_channel_label_elevenlabs_phone(self) -> None:
        from voice_call_summary import _channel_label

        self.assertEqual(_channel_label("elevenlabs-phone"), "Hammer Voice Ai")
        self.assertEqual(_channel_label("elevenlabs-browser"), "Hammer Voice Ai")
        self.assertEqual(_channel_label("phone"), "Phone")

    def test_slack_summary_dialer_sections(self) -> None:
        acc = VoiceCallLeadAccumulator(call_id="fmt-1", channel="phone", call_direction="inbound")
        acc.set_value("name", "David")
        acc.set_value("dealership_name", "Speedlag")
        acc.set_value("email", "david@speedlag.com")
        acc.set_value("phone", "+15551234567")
        acc.interaction_summary = (
            "Summary: Tyler from Hammer AI contacted David at Speedlag about AI integration. "
            "David asked for email. Decisions and agreements: boss decides. "
            "Action items: send email."
        )
        acc.capture_lead_fired = True
        text = build_interaction_summary(acc)
        self.assertIn("*Speedlag · David*", text)
        self.assertIn("*Summary*", text)
        self.assertIn("*Decisions*", text)
        self.assertIn("*Next*", text)
        self.assertNotIn("WHO TO CALL BACK", text)
        self.assertNotIn("WHERE THEY LEFT OFF", text)

    @patch("voice_call_summary.post_voice_call_summary")
    @patch("voice_call_summary.voice_call_summary_webhook_configured", return_value=True)
    def test_maybe_post_browser_pen_only(
        self, _configured: unittest.mock.MagicMock, post: unittest.mock.MagicMock
    ) -> None:
        acc = VoiceCallLeadAccumulator(call_id="test-5", channel="browser")
        posted = maybe_post_voice_call_summary(acc)
        self.assertTrue(posted)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
