"""Unit tests for Zapier lead payload formatting."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from lead_zapier import (
    AgreementApprovalRequest,
    LeadCaptureRequest,
    agreement_approval_status,
    build_zapier_payload,
    lead_webhook_configured,
    lead_webhook_env_name,
    post_lead_to_zapier,
    record_agreement_approval_request,
    zapier_lead_webhook_url_for_channel,
)


class LeadZapierPayloadTests(unittest.TestCase):
    def test_voice_agreement_minimal_fields(self) -> None:
        body = LeadCaptureRequest(
            email="buyer@dealer.com",
            dealership_name="Sunrise Ford",
            selected_plan="Facebook AIA",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["event"], "agreement_email_request")
        self.assertEqual(payload["dealershipName"], "Sunrise Ford")
        self.assertEqual(payload["email"], "buyer@dealer.com")
        self.assertEqual(payload["productLine"], "facebook_aia")
        self.assertIn("Sunrise Ford", payload["agreementEmailHtml"])
        self.assertEqual(payload["phoneNumber"], "")
        self.assertEqual(payload["fullName"], "")

    def test_voice_signup_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Jane Dealer",
            phone="5551234567",
            email="Jane@Example.COM",
            dealership_name="Victory Motorsports",
            website="https://victorymotors.com",
            role="general-manager",
            selected_plan="Hammer Drive 31-60 $399/mo",
            lot_size="45",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["event"], "agreement_email_request")
        self.assertEqual(payload["channel"], "voice")
        self.assertEqual(payload["email"], "jane@example.com")
        self.assertEqual(payload["firstName"], "Jane")
        self.assertEqual(payload["lastName"], "Dealer")
        self.assertIn("Hammer Drive", payload["selectedPlan"])
        self.assertEqual(payload["lotSize"], "45")
        self.assertEqual(payload["productLine"], "hammer_drive")
        self.assertEqual(payload["dealershipName"], "Victory Motorsports")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "399")
        self.assertIn("$399 USD /month", payload["subscriptionMonthlyDisplay"])
        self.assertEqual(payload["emailGreetingLine"], "Hello Victory Motorsports,")
        self.assertIn("Hello Victory Motorsports,", payload["agreementEmailBody"])
        self.assertIn("I approve", payload["agreementEmailBody"])
        self.assertEqual(payload["agreementTemplate"], "hammer_drive")
        self.assertEqual(payload["agreementEmailSubject"], "Hammer agreement — Victory Motorsports")
        self.assertIn("today", payload["firstMonthBillingDisplay"].lower())
        self.assertIn("agreementEmailHtml", payload)
        self.assertIn("agreementEmailHtmlEmbedded", payload)
        self.assertIn("agreementLogoUrl", payload)
        self.assertIn("Hello Victory Motorsports,", payload["agreementEmailHtml"])
        self.assertIn("HAMMER", payload["agreementEmailHtml"])
        self.assertIn("#CC0000", payload["agreementEmailHtml"])
        self.assertIn(
            "127.0.0.1:8780/email/hammer-ai-logo.png",
            payload.get("agreementLogoUrl", ""),
        )
        html = payload["agreementEmailHtml"]
        self.assertIn("<strong>I approve</strong>", html)
        self.assertIn("<strong>HAMMER</strong>", html)
        self.assertIn("<strong>Your service description:</strong>", html)
        self.assertIn("<strong>Subscription:</strong>", html)
        self.assertIn("<strong>Next Payment:</strong>", html)
        self.assertRegex(html, r"<strong>Next Payment:</strong> <strong>\d{1,2}/\d{1,2}/\d{2}</strong>")
        self.assertIn("<strong>$399 USD today</strong>", html)
        self.assertIn("<strong>CANCELLATION POLICY:</strong>", html)
        self.assertIn("<strong>1 day notice</strong>", html)
        self.assertIn("<em>unsubscribe</em>", html)
        self.assertIn("<strong>DATA ACCESS AUTHORIZATION:</strong>", html)
        self.assertIn("<strong>Thank you for choosing Hammer!</strong>", html)
        self.assertIn("Hello Victory Motorsports,", payload["agreementEmailHtmlEmbedded"])
        self.assertIn("#CC0000", payload["agreementEmailHtmlEmbedded"])

    def test_voice_signup_hammerdrive_one_word_payload(self) -> None:
        body = LeadCaptureRequest(
            email="buyer@dealer.com",
            dealership_name="Tyler 67",
            selected_plan="HammerDrive",
            lot_size="55",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["event"], "agreement_email_request")
        self.assertEqual(payload["productLine"], "hammer_drive")
        self.assertEqual(payload["agreementTemplate"], "hammer_drive")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "399")
        self.assertIn("agreementEmailSubject", payload)
        self.assertIn("agreementEmailHtml", payload)

    def test_voice_signup_cad_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Jean Tremblay",
            phone="5145550100",
            email="jean@dealer.ca",
            dealership_name="Montreal Auto",
            website="https://montreal-auto.ca",
            role="general-manager",
            selected_plan="Hammer Drive 31-60",
            lot_size="40",
            channel="voice",
            currency="CAD",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["currency"], "CAD")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "399")
        self.assertIn("$399 CAD /month", payload["subscriptionMonthlyDisplay"])
        self.assertIn("agreementEmailHtml", payload)
        self.assertIn("CAD", payload["agreementEmailHtml"])

    def test_voice_signup_facebook_aia_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Sam Manager",
            phone="5551234567",
            email="sam@dealer.com",
            website="sunriseford.com",
            dealership_name="Sunrise Ford",
            role="general-manager",
            selected_plan="Facebook AIA",
            lot_size="50",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["event"], "agreement_email_request")
        self.assertEqual(payload["productLine"], "facebook_aia")
        self.assertEqual(payload["agreementTemplate"], "facebook_aia")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "299")
        self.assertEqual(payload["metaAdSpendDailyAmount"], "15")
        self.assertIn("Facebook AIA agreement", payload["agreementEmailSubject"])
        self.assertIn("Facebook Advertising + AI", payload["agreementEmailHtml"])
        self.assertIn("Ad spend for Ads is non-refundable", payload["agreementEmailHtml"])
        self.assertIn("#CC0000", payload["agreementEmailHtml"])

    def test_voice_signup_hammer_connect_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Rep User",
            phone="5551234567",
            email="rep@dealer.com",
            website="victorymotors.com",
            dealership_name="Victory Motorsports",
            role="sales-manager",
            selected_plan="Hammer Connect",
            lot_size="45",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["productLine"], "hammer_connect")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "99")
        self.assertIn("Hammer Connect agreement", payload["agreementEmailSubject"])
        self.assertIn("<strong>Your service description:</strong> Hammer Connect", payload["agreementEmailHtml"])
        self.assertIn("<strong>Hammer Connect</strong>", payload["agreementEmailHtml"])
        self.assertIn("Hammer/Hammer Connect", payload["agreementEmailHtml"])
        self.assertIn("$99/month", payload["agreementEmailHtml"])
        self.assertNotIn("Additional Users $50 monthly", payload["agreementEmailHtml"])

    def test_voice_signup_marketposter_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Rep User",
            phone="5551234567",
            email="rep@dealer.com",
            website="victorymotors.com",
            dealership_name="Victory Motorsports",
            role="sales-manager",
            selected_plan="MarketPoster",
            seat_count="2 users",
            lot_size="45",
            channel="voice",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["productLine"], "marketposter")
        self.assertEqual(payload["seatCount"], "2")
        self.assertEqual(payload["subscriptionMonthlyAmount"], "249")
        self.assertIn("MarketPoster agreement", payload["agreementEmailSubject"])
        self.assertIn("Facebook Market Place Posting", payload["agreementEmailHtml"])
        self.assertIn("Hammer/MarketPoster", payload["agreementEmailHtml"])
        self.assertIn("$249/month + 2 Users", payload["agreementEmailHtml"])

    def test_website_lead_payload(self) -> None:
        body = LeadCaptureRequest(
            name="Bob",
            phone="+1 555 987 6543",
            email="bob@test.com",
            website="testdealer.com",
            role="sales-manager",
            channel="website",
        )
        payload = build_zapier_payload(body)
        self.assertEqual(payload["event"], "website_lead")
        self.assertEqual(payload["leadSource"], "website form")
        self.assertNotIn("agreementEmailHtml", payload)

    def test_approval_tracking(self) -> None:
        record_agreement_approval_request(
            AgreementApprovalRequest(email="Sign@Up.com", approved=True, reply_text="I approve")
        )
        status = agreement_approval_status("sign@up.com")
        self.assertTrue(status["approved"])
        self.assertEqual(status["email"], "sign@up.com")

    def test_capture_lead_registers_pending_approval(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "approvals.json"
            os.environ["REALTIME_SALES_APPROVALS_PATH"] = str(store)
            try:
                body = LeadCaptureRequest(
                    name="Pending User",
                    phone="5551234567",
                    email="pending@dealer.com",
                    website="dealer.com",
                    dealership_name="Test Dealer",
                    role="sales-manager",
                    selected_plan="Hammer Connect",
                    lot_size="20",
                    channel="voice",
                )
                build_zapier_payload(body)
                status = agreement_approval_status("pending@dealer.com")
                self.assertFalse(status["approved"])
                self.assertTrue(status.get("pending"))
            finally:
                os.environ.pop("REALTIME_SALES_APPROVALS_PATH", None)


class LeadZapierWebhookRoutingTests(unittest.TestCase):
    def test_webhook_env_names(self) -> None:
        self.assertEqual(lead_webhook_env_name("voice"), "ZAPIER_LEAD_WEBHOOK_URL")
        self.assertEqual(lead_webhook_env_name("website"), "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL")

    @patch.dict(
        os.environ,
        {
            "ZAPIER_LEAD_WEBHOOK_URL": "https://hooks.zapier.com/voice",
            "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL": "https://hooks.zapier.com/website",
        },
        clear=False,
    )
    def test_urls_by_channel(self) -> None:
        self.assertEqual(zapier_lead_webhook_url_for_channel("voice"), "https://hooks.zapier.com/voice")
        self.assertEqual(
            zapier_lead_webhook_url_for_channel("website"),
            "https://hooks.zapier.com/website",
        )
        self.assertTrue(lead_webhook_configured("voice"))
        self.assertTrue(lead_webhook_configured("website"))

    @patch.dict(os.environ, {}, clear=False)
    def test_post_lead_uses_website_hook(self) -> None:
        os.environ.pop("ZAPIER_LEAD_WEBHOOK_URL", None)
        os.environ.pop("ZAPIER_WEBSITE_LEAD_WEBHOOK_URL", None)
        os.environ["ZAPIER_WEBSITE_LEAD_WEBHOOK_URL"] = "https://hooks.zapier.com/website"
        payload = {"channel": "website", "event": "website_lead", "email": "a@b.com"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        with patch("lead_zapier.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = mock_response
            post_lead_to_zapier(payload)
            args, kwargs = client_cls.return_value.__enter__.return_value.post.call_args
            self.assertEqual(args[0], "https://hooks.zapier.com/website")
            self.assertEqual(kwargs.get("json"), payload)
        os.environ.pop("ZAPIER_WEBSITE_LEAD_WEBHOOK_URL", None)

    @patch.dict(os.environ, {}, clear=False)
    def test_post_lead_uses_voice_hook(self) -> None:
        os.environ.pop("ZAPIER_LEAD_WEBHOOK_URL", None)
        os.environ.pop("ZAPIER_WEBSITE_LEAD_WEBHOOK_URL", None)
        os.environ["ZAPIER_LEAD_WEBHOOK_URL"] = "https://hooks.zapier.com/voice"
        payload = {"channel": "voice", "event": "agreement_email_request", "email": "a@b.com"}
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        with patch("lead_zapier.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = mock_response
            post_lead_to_zapier(payload)
            args, _kwargs = client_cls.return_value.__enter__.return_value.post.call_args
            self.assertEqual(args[0], "https://hooks.zapier.com/voice")
        os.environ.pop("ZAPIER_LEAD_WEBHOOK_URL", None)

    def test_post_lead_website_missing_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                post_lead_to_zapier({"channel": "website", "event": "website_lead"})
        self.assertIn("ZAPIER_WEBSITE_LEAD_WEBHOOK_URL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
