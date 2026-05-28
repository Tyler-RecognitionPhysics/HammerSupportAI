"""Tests for agreement I approve persistence."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agreement_approvals import (
    _fly_approval_api_base,
    _use_fly_approval_store,
    agreement_approval_status,
    just_replied_poll_wait_seconds,
    record_agreement_approval,
    register_pending_agreement,
    reply_indicates_approval,
    reset_agreement_approval,
)


class AgreementApprovalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._store = Path(self._tmpdir.name) / "approvals.json"
        os.environ["REALTIME_SALES_APPROVALS_PATH"] = str(self._store)

    def tearDown(self) -> None:
        os.environ.pop("REALTIME_SALES_APPROVALS_PATH", None)
        self._tmpdir.cleanup()

    def test_reply_detection(self) -> None:
        self.assertTrue(reply_indicates_approval("I approve"))
        self.assertTrue(reply_indicates_approval("Yes, I approved the terms"))
        self.assertFalse(reply_indicates_approval("sounds good"))

    def test_reset_clears_pending_and_approved(self) -> None:
        register_pending_agreement("retry@dealer.com", dealership="Acme")
        record_agreement_approval("retry@dealer.com", reply_text="I approve", source="test")
        self.assertTrue(agreement_approval_status("retry@dealer.com")["approved"])

        self.assertTrue(reset_agreement_approval("retry@dealer.com"))
        status = agreement_approval_status("retry@dealer.com")
        self.assertFalse(status["approved"])
        self.assertFalse(status.get("pending"))

    def test_re_register_after_reset_allows_resend(self) -> None:
        register_pending_agreement("again@dealer.com", dealership="First")
        record_agreement_approval("again@dealer.com", reply_text="I approve", source="test")
        reset_agreement_approval("again@dealer.com")
        register_pending_agreement("again@dealer.com", dealership="Second")
        status = agreement_approval_status("again@dealer.com")
        self.assertFalse(status["approved"])
        self.assertTrue(status.get("pending"))
        self.assertEqual(status.get("dealership"), "Second")

    def test_pending_then_approved(self) -> None:
        register_pending_agreement("buyer@dealer.com", dealership="Acme", product_line="hammer_connect")
        pending = agreement_approval_status("buyer@dealer.com")
        self.assertFalse(pending["approved"])
        self.assertTrue(pending.get("pending"))

        record_agreement_approval("buyer@dealer.com", reply_text="I approve", source="test")
        approved = agreement_approval_status("buyer@dealer.com")
        self.assertTrue(approved["approved"])
        self.assertIn("approvedAt", approved)

    def test_reject_without_i_approve_phrase(self) -> None:
        register_pending_agreement("x@dealer.com")
        record_agreement_approval(
            "x@dealer.com", approved=False, reply_text="thanks", source="test"
        )
        status = agreement_approval_status("x@dealer.com")
        self.assertFalse(status["approved"])

    def test_zapier_approved_flag_when_snippet_omits_phrase(self) -> None:
        register_pending_agreement("zap@dealer.com")
        record_agreement_approval(
            "zap@dealer.com",
            approved=True,
            reply_text="Re: Hammer agreement — see below",
            source="zapier",
        )
        status = agreement_approval_status("zap@dealer.com")
        self.assertTrue(status["approved"])

    def test_voice_call_approval_fallback_when_pending(self) -> None:
        from agreement_approvals import ensure_voice_call_approval, voice_approve_on_call_enabled

        register_pending_agreement("voice@dealer.com")
        with patch.dict(os.environ, {"REALTIME_SALES_VOICE_APPROVE_ON_CALL": "1"}, clear=False):
            self.assertTrue(voice_approve_on_call_enabled())
            with patch("agreement_approvals._post_fly_approval_api", return_value=True):
                status = ensure_voice_call_approval("voice@dealer.com")
        self.assertTrue(status["approved"])

    def test_poll_picks_up_late_approval(self) -> None:
        register_pending_agreement("late@dealer.com")
        import threading
        import time

        def approve_soon() -> None:
            time.sleep(0.35)
            record_agreement_approval("late@dealer.com", reply_text="I approve", source="test")

        threading.Thread(target=approve_soon, daemon=True).start()
        status = agreement_approval_status("late@dealer.com", wait_seconds=2, max_wait_seconds=2)
        self.assertTrue(status["approved"])

    def test_just_replied_wait_env_default(self) -> None:
        os.environ.pop("AGREEMENT_APPROVAL_JUST_REPLIED_WAIT_SECONDS", None)
        self.assertGreaterEqual(just_replied_poll_wait_seconds(), 8)

    def test_fly_approval_api_base_from_health_url(self) -> None:
        os.environ["FLY_TELEPHONY_HEALTH_URL"] = "https://hammer-voice-telephony.fly.dev/api/health"
        os.environ.pop("TELEPHONY_PUBLIC_BASE_URL", None)
        os.environ.pop("FLY_APPROVAL_API_BASE_URL", None)
        self.assertEqual(_fly_approval_api_base(), "https://hammer-voice-telephony.fly.dev")

    def test_use_fly_approval_store_on_serverless(self) -> None:
        os.environ["REALTIME_SALES_SERVERLESS"] = "1"
        self.assertTrue(_use_fly_approval_store())
        os.environ.pop("REALTIME_SALES_SERVERLESS", None)
        self.assertFalse(_use_fly_approval_store())


if __name__ == "__main__":
    unittest.main()
