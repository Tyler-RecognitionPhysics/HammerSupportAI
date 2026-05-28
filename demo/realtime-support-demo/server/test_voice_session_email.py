"""Session email recovery and server-side correction for voice signup tools."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from elevenlabs_agent import _derive_session
from hammer_office import HammerOfficeError
from hammer_office_session import _LiveSession, _mark_field_confirmed, _missing_for_submit, _optimistic_store
from voice_tools import (
    CallSession,
    VoiceToolExecutor,
    _sanitize_capture_email,
    derive_signup_context_from_messages,
)


class SessionEmailTests(unittest.TestCase):
    def test_derive_agreement_email_from_capture_lead(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "capture_lead",
                            "arguments": '{"email":"Tbennett6025@gmail.com","dealership_name":"Test"}',
                        }
                    }
                ],
            }
        ]
        self.assertEqual(
            derive_signup_context_from_messages(messages).email,
            "tbennett6025@gmail.com",
        )

    def test_derive_signup_context_marks_sent_from_tool_result(self) -> None:
        messages = [
            {
                "role": "tool",
                "content": "ok — agreement email queued for t@test.com; SESSION EMAIL KEY = t@test.com",
            }
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertTrue(ctx.capture_lead_sent)
        self.assertEqual(ctx.email, "t@test.com")

    def test_derive_session_sets_agreement_email(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "capture_lead",
                            "arguments": '{"email":"tbennett6025@gmail.com","dealership_name":"Test"}',
                        }
                    }
                ],
            }
        ]
        session = _derive_session(messages)
        self.assertEqual(session.agreement_email, "tbennett6025@gmail.com")

    def test_execute_corrects_wrong_tool_email(self) -> None:
        session = CallSession(agreement_email="tbennett6025@gmail.com", pen_hammer_close_active=True)
        executor = VoiceToolExecutor(lambda: None)
        with patch.object(executor, "_tool_fill_hammer_account_field", return_value="ok") as mocked:
            executor.execute(
                session,
                "fill_hammer_account_field",
                {"email": "test.email@example.com", "field": "phone", "value": "5125551212"},
            )
        mocked.assert_called_once()
        args = mocked.call_args[0][1]
        self.assertEqual(args["email"], "tbennett6025@gmail.com")

    def test_capture_lead_unlocks_hammer_without_begin_signup(self) -> None:
        session = CallSession(voice_scenario="pen", pen_hammer_close_active=False)
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.prewarm_hammer_account_form", return_value={"ok": True}):
                with patch("voice_tools.lead_webhook_configured", return_value=True):
                    with patch("voice_tools.agreement_email_already_queued", return_value=False):
                        result = executor._tool_capture_lead(
                            session,
                            {
                                "email": "buyer@test.com",
                                "dealership_name": "Test Dealer",
                                "selected_plan": "Hammer Drive",
                                "lot_size": "15",
                            },
                        )
        self.assertTrue(session.pen_hammer_close_active)
        self.assertTrue(session.capture_lead_sent)
        self.assertIn("agreement email queued", result)
        post.assert_called_once()

    def test_capture_lead_does_not_claim_sent_when_template_unresolved(self) -> None:
        session = CallSession(voice_scenario="hammer", pen_hammer_close_active=True)
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.lead_webhook_configured", return_value=True):
                with patch("voice_tools.agreement_email_already_queued", return_value=False):
                    result = executor._tool_capture_lead(
                        session,
                        {
                            "email": "buyer@test.com",
                            "dealership_name": "Test Dealer",
                            "selected_plan": "Unknown Product",
                            "lot_size": "15",
                        },
                    )
        self.assertFalse(session.capture_lead_sent)
        self.assertIn("template could not be resolved", result)
        post.assert_not_called()

    def test_create_hammer_account_defaults_currency_when_missing(self) -> None:
        session = CallSession(voice_scenario="hammer", pen_hammer_close_active=True)
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.agreement_approval_status", return_value={"approved": True}):
            with patch("voice_tools.hammer_office_configured", return_value=True):
                with patch("voice_tools.account_already_created", return_value=(False, "")):
                    with patch("voice_tools.create_hammer_account") as create:
                        create.return_value.dry_run = False
                        result = executor._tool_create_hammer_account(
                            session,
                            {
                                "email": "buyer@test.com",
                                "name": "Tyler Bennett",
                                "dealership_name": "Tyler 67",
                                "business_type": "LLC",
                                "phone": "9739083881",
                                "website": "tyler67.com",
                                "address": "123 Easy Street, Austin, Texas 78725",
                            },
                        )
        self.assertIn("account created", result)
        req = create.call_args.args[0]
        self.assertEqual(req.currency, "USD")

    def test_check_agreement_auto_sends_missing_agreement_email(self) -> None:
        session = CallSession(
            pen_hammer_close_active=True,
            agreement_email="t@test.com",
            agreement_dealership="Test Dealer",
            agreement_plan="Hammer Drive",
            agreement_lot_size="15",
            capture_lead_sent=False,
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch.object(executor, "_tool_capture_lead", return_value="ok — agreement email queued for t@test.com; SESSION EMAIL KEY = t@test.com") as capture:
            with patch("voice_tools.agreement_email_already_queued", return_value=False):
                with patch("voice_tools.agreement_approval_status", return_value={"approved": False, "pending": False}):
                    result = executor.execute(
                    session,
                    "check_agreement_approval",
                    {"email": "t@test.com", "just_replied": False},
                )
        capture.assert_called_once()
        self.assertIn("now queued", result)

    def test_no_tool_agreement_reply_auto_sends_email(self) -> None:
        session = CallSession(
            pen_hammer_close_active=True,
            agreement_email="t@test.com",
            agreement_dealership="Test Dealer",
            agreement_plan="",
            pen_buyer_product="Hammer Drive",
            capture_lead_sent=False,
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch.object(
            executor,
            "_tool_capture_lead",
            return_value="ok — agreement email queued for t@test.com; SESSION EMAIL KEY = t@test.com",
        ) as capture:
            with patch("voice_tools.agreement_email_already_queued", return_value=False):
                result = executor.ensure_agreement_email_queued(
                    session,
                    "I sent the agreement email. Did it land in your inbox?",
                )
        capture.assert_called_once()
        args = capture.call_args[0][1]
        self.assertEqual(args["selected_plan"], "Hammer Drive")
        self.assertIn("agreement email queued", result)

    def test_signup_context_infers_dealership_from_transcript(self) -> None:
        messages = [
            {"role": "assistant", "content": "What's the dealership name?"},
            {"role": "user", "content": "Test Dealer 123"},
            {"role": "assistant", "content": "What's the best email?"},
            {"role": "user", "content": "Owner@TestDealer.com"},
            {"role": "assistant", "content": "Drive is three ninety-nine for that lot."},
            {"role": "user", "content": "15 cars"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.dealership_name, "Test Dealer 123")
        self.assertEqual(ctx.email, "owner@testdealer.com")
        self.assertEqual(ctx.selected_plan, "Hammer Drive")
        self.assertEqual(ctx.lot_size, "15")

    def test_signup_context_infers_account_fields_from_transcript(self) -> None:
        messages = [
            {"role": "assistant", "content": "What's your business type?"},
            {"role": "user", "content": "LLC"},
            {"role": "assistant", "content": "What's your phone number?"},
            {"role": "user", "content": "(512) 555-1212"},
            {"role": "assistant", "content": "What's your website?"},
            {"role": "user", "content": "tyler757.com"},
            {"role": "assistant", "content": "What's the full business address?"},
            {"role": "user", "content": "123 Easy Street, Austin, Texas 78725"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields["business_type"], "LLC")
        self.assertEqual(ctx.account_fields["phone"], "5125551212")
        self.assertEqual(ctx.account_fields["website"], "tyler757.com")
        self.assertEqual(ctx.account_fields["address"], "123 Easy Street, Austin, Texas 78725")

    def test_hammer_scenario_auto_fills_when_approved_and_creating(self) -> None:
        session = CallSession(
            voice_scenario="hammer",
            agreement_email="t@test.com",
            agreement_dealership="Tyler76",
            account_fields={
                "last_name": "Bennett",
                "business_type": "LLC",
                "phone": "9739083881",
                "website": "tyler76.com",
                "address": "123 Easy Street, Austin, Texas 78725",
            },
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch("agreement_approvals.agreement_approval_status", return_value={"approved": True}):
            with patch("hammer_office_session.account_already_created", return_value=(False, None)):
                with patch("hammer_office_session.open_hammer_account_form", return_value={"ok": True}):
                    with patch("hammer_office_session.fill_hammer_account_field") as fill:
                        fill.return_value = {
                            "ok": True,
                            "account_created": True,
                            "message": "account created",
                        }
                        result = executor.ensure_account_fields_recorded(
                            session,
                            "One moment while I create your account.",
                        )
        self.assertIsNotNone(result)
        self.assertIn("PHASE C.1", result or "")
        fill.assert_called()

    def test_pen_scenario_skips_auto_account_field_fill(self) -> None:
        session = CallSession(
            voice_scenario="pen",
            agreement_email="t@test.com",
            account_fields={"website": "example.com"},
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.fill_hammer_account_field") as fill:
            result = executor.ensure_account_fields_recorded(session)
        self.assertIsNone(result)
        fill.assert_not_called()

    def test_last_name_fill_sets_name_for_submit(self) -> None:
        sess = _LiveSession(email="t@test.com")
        _optimistic_store(sess, "last_name", "Bennett")
        _mark_field_confirmed(sess, "last_name")
        self.assertEqual(sess.values["name"], "Bennett")
        missing = _missing_for_submit(sess.values, sess.confirmed_fields)
        self.assertNotIn("name", missing)

    def test_business_type_fill_rejects_dealership_category(self) -> None:
        sess = _LiveSession(email="t@test.com")
        with self.assertRaisesRegex(HammerOfficeError, "legal structure"):
            _optimistic_store(sess, "business_type", "motorcycle dealer")

    def test_phone_inferred_without_prev_assistant_context(self) -> None:
        # Hannah may not literally say "phone" or "number" — extraction should still work.
        messages = [
            {"role": "assistant", "content": "Got it. What's the best contact for the dealership?"},
            {"role": "user", "content": "512-883-1336"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields.get("phone"), "5128831336")

    def test_phone_inferred_from_natural_speech(self) -> None:
        messages = [
            {"role": "assistant", "content": "Last one — best callback?"},
            {"role": "user", "content": "Yeah it's (973) 908-3881 thanks"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields.get("phone"), "9739083881")

    def test_phone_inferred_with_country_code(self) -> None:
        messages = [
            {"role": "assistant", "content": "Number?"},
            {"role": "user", "content": "It is +1 512 883 1336"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields.get("phone"), "5128831336")

    def test_phone_not_extracted_from_email_digits(self) -> None:
        messages = [
            {"role": "assistant", "content": "Email?"},
            {"role": "user", "content": "tbennett6025@gmail.com"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertNotIn("phone", ctx.account_fields)

    def test_phone_not_extracted_from_address_zip(self) -> None:
        messages = [
            {"role": "assistant", "content": "Address?"},
            {"role": "user", "content": "123 Easy Street, Austin, Texas, 78725"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertNotIn("phone", ctx.account_fields)
        self.assertEqual(
            ctx.account_fields.get("address"),
            "123 Easy Street, Austin, Texas, 78725",
        )

    def test_business_type_inferred_when_assistant_asked(self) -> None:
        messages = [
            {"role": "assistant", "content": "What's the legal business structure?"},
            {"role": "user", "content": "We're an LLC."},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields.get("business_type"), "LLC")

    def test_website_inferred_when_assistant_asked(self) -> None:
        messages = [
            {"role": "assistant", "content": "What's your website?"},
            {"role": "user", "content": "Our site is hammertime.com"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertEqual(ctx.account_fields.get("website"), "hammertime.com")

    def test_business_type_not_inferred_without_assistant_prompt(self) -> None:
        messages = [
            {"role": "assistant", "content": "Cool."},
            {"role": "user", "content": "We're an LLC."},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertNotIn("business_type", ctx.account_fields)

    def test_website_not_inferred_without_assistant_prompt(self) -> None:
        messages = [
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "Our site is hammertime.com"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertNotIn("website", ctx.account_fields)

    def test_website_not_extracted_from_email(self) -> None:
        messages = [
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "tbennett@gmail.com"},
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertNotIn("website", ctx.account_fields)

    def test_auto_capture_lead_skips_when_agreement_already_pending(self) -> None:
        session = CallSession(
            pen_hammer_close_active=True,
            agreement_email="buyer@test.com",
            agreement_dealership="Test Dealer",
            capture_lead_sent=False,
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.agreement_email_already_queued", return_value=True):
                result = executor._maybe_auto_capture_lead(session)
        self.assertIsNone(result)
        post.assert_not_called()
        self.assertTrue(session.capture_lead_sent)

    def test_capture_lead_sent_derived_from_history_tool_call(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "capture_lead",
                            "arguments": '{"email":"t@test.com","dealership_name":"Test"}',
                        }
                    }
                ],
            }
        ]
        ctx = derive_signup_context_from_messages(messages)
        self.assertTrue(ctx.capture_lead_sent)

    def test_sanitize_capture_email_collapses_spelled_out_readback(self) -> None:
        email, warning = _sanitize_capture_email("t-b-e-n-n-e-t-t-6-0-2-5@gmail.com")
        self.assertIsNone(warning)
        self.assertEqual(email, "tbennett6025@gmail.com")

    def test_capture_lead_collapses_spelled_out_email_before_zapier(self) -> None:
        session = CallSession(voice_scenario="pen", pen_hammer_close_active=True)
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.prewarm_hammer_account_form", return_value={"ok": True}):
                with patch("voice_tools.lead_webhook_configured", return_value=True):
                    with patch("voice_tools.agreement_email_already_queued", return_value=False):
                        result = executor._tool_capture_lead(
                            session,
                            {
                                "email": "t-b-e-n-n-e-t-t-6-0-2-5@gmail.com",
                                "dealership_name": "Test Dealer",
                                "selected_plan": "Hammer Drive",
                                "lot_size": "15",
                            },
                        )
        self.assertTrue(result.startswith("ok —"))
        self.assertEqual(session.agreement_email, "tbennett6025@gmail.com")
        post.assert_called_once()
        payload_email = post.call_args[0][0]["email"]
        self.assertEqual(payload_email.lower(), "tbennett6025@gmail.com")

    def test_capture_lead_browser_retest_resends_when_account_not_created(self) -> None:
        session = CallSession(
            voice_scenario="hammer",
            pen_hammer_close_active=True,
            capture_lead_sent=False,
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.lead_webhook_configured", return_value=True):
                with patch("voice_tools.agreement_email_already_queued", return_value=True):
                    with patch("voice_tools.account_already_created", return_value=(False, None)):
                        with patch("voice_tools.reset_agreement_approval", return_value=True):
                            with patch("voice_tools.reset_voice_signup_session"):
                                with patch("voice_tools.prewarm_hammer_account_form", return_value={"ok": True}):
                                    result = executor._tool_capture_lead(
                                        session,
                                        {
                                            "email": "buyer@test.com",
                                            "dealership_name": "Test Dealer",
                                            "selected_plan": "Hammer Drive",
                                            "lot_size": "15",
                                        },
                                    )
        self.assertTrue(result.startswith("ok —"))
        post.assert_called_once()

    def test_capture_lead_blocks_pen_when_agreement_already_pending(self) -> None:
        session = CallSession(voice_scenario="pen", pen_hammer_close_active=True, capture_lead_sent=False)
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.lead_webhook_configured", return_value=True):
                with patch("voice_tools.agreement_email_already_queued", return_value=True):
                    result = executor._tool_capture_lead(
                        session,
                        {
                            "email": "buyer@test.com",
                            "dealership_name": "Test Dealer",
                            "selected_plan": "Hammer Drive",
                            "lot_size": "15",
                        },
                    )
        self.assertTrue(result.startswith("already sent"))
        self.assertTrue(session.capture_lead_sent)
        post.assert_not_called()

    def test_capture_lead_allows_resend_when_requested(self) -> None:
        session = CallSession(
            voice_scenario="hammer",
            pen_hammer_close_active=True,
            capture_lead_sent=True,
            agreement_email="buyer@test.com",
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch("voice_tools.post_lead_to_zapier") as post:
            with patch("voice_tools.lead_webhook_configured", return_value=True):
                with patch("voice_tools.agreement_email_already_queued", return_value=True):
                    with patch("voice_tools.reset_agreement_approval", return_value=True):
                        result = executor._tool_capture_lead(
                            session,
                            {
                                "email": "buyer@test.com",
                                "dealership_name": "Test Dealer",
                                "selected_plan": "Hammer Drive",
                                "lot_size": "15",
                                "resend_agreement": True,
                            },
                        )
        self.assertTrue(result.startswith("ok —"))
        post.assert_called_once()

    def test_ensure_agreement_email_skips_when_pending(self) -> None:
        session = CallSession(
            pen_hammer_close_active=True,
            agreement_email="t@test.com",
            agreement_dealership="Test Dealer",
            capture_lead_sent=False,
        )
        executor = VoiceToolExecutor(lambda: None)
        with patch.object(executor, "_tool_capture_lead") as capture:
            with patch("voice_tools.agreement_email_already_queued", return_value=True):
                result = executor.ensure_agreement_email_queued(
                    session,
                    "Got the agreement at that same email?",
                )
        capture.assert_not_called()
        self.assertIsNone(result)
        self.assertTrue(session.capture_lead_sent)


if __name__ == "__main__":
    unittest.main()
