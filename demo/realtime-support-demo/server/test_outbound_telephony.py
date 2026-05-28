"""Tests for outbound telephony (Call me)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from outbound_telephony import (
    build_bridge_twiml,
    callback_status_public,
    initiate_callback,
    is_active_outbound_callee,
    outbound_enabled,
    reset_outbound_state_for_tests,
    resolve_outbound_caller_phone,
    resolve_sip_caller_for_summary,
    sip_phone_needs_outbound_lookup,
    validate_callback_request,
)
from sip_realtime import extract_caller_phone_from_incoming_event


class OutboundEnabledTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_outbound_state_for_tests()

    def test_disabled_without_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(outbound_enabled())

    def test_enabled_with_credentials(self) -> None:
        env = {
            "TWILIO_OUTBOUND_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "DEMO_PHONE_NUMBER": "+15125550199",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(outbound_enabled())


class OutboundValidationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_outbound_state_for_tests()

    def test_consent_required(self) -> None:
        with self.assertRaises(ValueError):
            validate_callback_request(phone="+15125550199", consent=False, client_ip="1.2.3.4")

    def test_normalizes_us_phone(self) -> None:
        phone = validate_callback_request(phone="(512) 555-0199", consent=True, client_ip="1.2.3.4")
        self.assertEqual(phone, "+15125550199")


class OutboundTwimlTests(unittest.TestCase):
    def test_bridge_twiml_includes_sip_and_customer_header(self) -> None:
        twiml = build_bridge_twiml(
            phone="+15125550199",
            sip_uri="sip:proj_test@sip.api.openai.com;transport=tls",
        )
        self.assertIn("X-Customer-Phone=+15125550199", twiml)
        self.assertIn("sip:proj_test@sip.api.openai.com;transport=tls", twiml)
        self.assertIn("<Dial", twiml)


class OutboundInitiateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_outbound_state_for_tests()

    @patch("outbound_telephony.get_twilio_client")
    def test_initiate_stores_correlation(self, mock_client_factory: MagicMock) -> None:
        mock_call = MagicMock()
        mock_call.sid = "CA123"
        mock_client_factory.return_value.calls.create.return_value = mock_call

        env = {
            "TWILIO_OUTBOUND_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "DEMO_PHONE_NUMBER": "+15125550199",
            "FLY_APP_NAME": "hammer-voice-telephony",
            "OPENAI_PROJECT_ID": "proj_test",
        }
        with patch.dict(os.environ, env, clear=True):
            result = initiate_callback(
                phone="+15125550199",
                consent=True,
                client_ip="127.0.0.1",
                sip_uri="sip:proj_test@sip.api.openai.com;transport=tls",
            )
        self.assertTrue(result["ok"])
        cid = result["cid"]
        status = callback_status_public(cid)
        assert status is not None
        self.assertEqual(status["phone"], "+15125550199")
        self.assertEqual(status["status"], "initiated")


class ResolveOutboundCallerPhoneTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_outbound_state_for_tests()

    @patch("outbound_telephony.get_twilio_client")
    def test_demo_line_replaced_with_site_entered_number(self, mock_client_factory: MagicMock) -> None:
        mock_call = MagicMock()
        mock_call.sid = "CA123"
        mock_client_factory.return_value.calls.create.return_value = mock_call

        env = {
            "TWILIO_OUTBOUND_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "DEMO_PHONE_NUMBER": "+17372056753",
            "FLY_APP_NAME": "hammer-voice-telephony",
            "OPENAI_PROJECT_ID": "proj_test",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(
                sip_phone_needs_outbound_lookup("+17372056753"),
                "demo caller ID must trigger correlation lookup",
            )
            result = initiate_callback(
                phone="+15558675309",
                consent=True,
                client_ip="127.0.0.1",
                sip_uri="sip:proj_test@sip.api.openai.com;transport=tls",
            )
            self.assertTrue(result["ok"])
            resolved = resolve_outbound_caller_phone("+17372056753")
        self.assertEqual(resolved, "+15558675309")

    def test_inbound_caller_unchanged_when_not_demo_line(self) -> None:
        with patch.dict(os.environ, {"DEMO_PHONE_NUMBER": "+17372056753"}, clear=False):
            self.assertFalse(sip_phone_needs_outbound_lookup("+15558675309"))
            self.assertEqual(resolve_outbound_caller_phone("+15558675309"), "+15558675309")

    def test_resolve_sip_caller_demo_line_is_outbound(self) -> None:
        reset_outbound_state_for_tests()
        env = {
            "TWILIO_OUTBOUND_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "DEMO_PHONE_NUMBER": "+17372056753",
            "FLY_APP_NAME": "hammer-voice-telephony",
            "OPENAI_PROJECT_ID": "proj_test",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("outbound_telephony.get_twilio_client"):
                initiate_callback(
                    phone="+15558675309",
                    consent=True,
                    client_ip="127.0.0.1",
                    sip_uri="sip:proj_test@sip.api.openai.com;transport=tls",
                )
            phone, direction = resolve_sip_caller_for_summary("+17372056753")
        self.assertEqual(direction, "outbound")
        self.assertEqual(phone, "+15558675309")

    def test_resolve_sip_caller_visitor_number_active_outbound(self) -> None:
        reset_outbound_state_for_tests()
        env = {
            "TWILIO_OUTBOUND_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "DEMO_PHONE_NUMBER": "+17372056753",
            "FLY_APP_NAME": "hammer-voice-telephony",
            "OPENAI_PROJECT_ID": "proj_test",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("outbound_telephony.get_twilio_client"):
                initiate_callback(
                    phone="+15558675309",
                    consent=True,
                    client_ip="127.0.0.1",
                    sip_uri="sip:proj_test@sip.api.openai.com;transport=tls",
                )
            self.assertTrue(is_active_outbound_callee("+15558675309"))
            phone, direction = resolve_sip_caller_for_summary("+15558675309")
        self.assertEqual(direction, "outbound")
        self.assertEqual(phone, "+15558675309")

    def test_resolve_sip_caller_true_inbound(self) -> None:
        reset_outbound_state_for_tests()
        with patch.dict(os.environ, {"DEMO_PHONE_NUMBER": "+17372056753"}, clear=False):
            phone, direction = resolve_sip_caller_for_summary("+15559876543")
        self.assertEqual(direction, "inbound")
        self.assertEqual(phone, "+15559876543")


class CustomerPhoneHeaderTests(unittest.TestCase):
    def _event(self, headers: list[dict[str, str]]) -> object:
        class Data:
            sip_headers = headers

        class Event:
            data = Data()

        return Event()

    def test_x_customer_phone_preferred(self) -> None:
        ev = self._event(
            [
                {"name": "X-Customer-Phone", "value": "+15125550199"},
                {"name": "From", "value": "sip:+18005550199@twilio.pstn.twilio.com"},
            ]
        )
        self.assertEqual(extract_caller_phone_from_incoming_event(ev), "+15125550199")


if __name__ == "__main__":
    unittest.main()
