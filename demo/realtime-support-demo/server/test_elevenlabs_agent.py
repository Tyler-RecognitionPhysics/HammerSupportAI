"""ElevenLabs custom LLM scenario routing (browser hammer vs pen challenge)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from elevenlabs_agent import (
    _build_accumulator_from_el_transcript,
    _build_prompt,
    _has_user_speech,
    _opening_greeting,
    _session_state_block,
    _voice_scenario,
)
from outbound_telephony import initiate_callback, reset_outbound_state_for_tests
from voice_tools import CallSession, hammer_browser_tool_definitions, pen_challenge_tool_definitions


class ElevenLabsScenarioTests(unittest.TestCase):
    def test_voice_scenario_defaults_to_pen_for_phone_without_browser_marker(self) -> None:
        self.assertEqual(_voice_scenario({}), "pen")

    def test_voice_scenario_defaults_to_hammer_for_website_calls(self) -> None:
        body = {"custom_llm_extra_body": {"voice_scenario": "hammer"}}
        self.assertEqual(_voice_scenario(body), "hammer")

    def test_voice_scenario_defaults_to_pen_on_serverless_without_browser_marker(self) -> None:
        with patch.dict(os.environ, {"REALTIME_SALES_SERVERLESS": "1"}, clear=False):
            self.assertEqual(_voice_scenario({}), "pen")
            self.assertEqual(_voice_scenario({"elevenlabs_extra_body": {}}), "pen")

    def test_voice_scenario_hammer_from_extra_body(self) -> None:
        body = {"elevenlabs_extra_body": {"voice_scenario": "hammer"}}
        self.assertEqual(_voice_scenario(body), "hammer")

    def test_voice_scenario_hammer_from_custom_llm_extra_body(self) -> None:
        body = {"custom_llm_extra_body": {"voice_scenario": "hammer"}}
        self.assertEqual(_voice_scenario(body), "hammer")

    def test_voice_scenario_ignores_pen_from_extra_body_to_protect_website(self) -> None:
        body = {"elevenlabs_extra_body": {"voice_scenario": "pen"}}
        self.assertEqual(_voice_scenario(body), "hammer")

    def test_voice_scenario_phone_with_caller_id(self) -> None:
        body = {
            "conversation_initiation_client_data": {
                "dynamic_variables": {"system__caller_id": "+15125550100"},
            },
        }
        self.assertEqual(_voice_scenario(body), "pen")

    def test_voice_scenario_phone_inbound_even_with_hammer_extra(self) -> None:
        body = {
            "custom_llm_extra_body": {"voice_scenario": "hammer"},
            "conversation_initiation_client_data": {
                "dynamic_variables": {"system__caller_id": "+15125550100"},
            },
        }
        self.assertEqual(_voice_scenario(body), "pen")

    def test_session_state_remembers_approved_created_signup(self) -> None:
        session = CallSession(
            voice_scenario="hammer",
            capture_lead_sent=True,
            agreement_email="buyer@dealer.com",
            agreement_dealership="Tyler 67",
        )
        with patch("agreement_approvals.agreement_approval_status", return_value={"approved": True}):
            with patch("hammer_office_session.account_already_created", return_value=(True, "https://office/accounts/1")):
                with patch("hammer_office_session.signup_ready_for_phase_c", return_value=True):
                    with patch("hammer_office_session.get_phase_b_missing_fields", return_value=[]):
                        state = _session_state_block(session)
        self.assertIn("I APPROVE ALREADY VERIFIED", state)
        self.assertIn("HAMMER ACCOUNT ALREADY CREATED", state)
        self.assertIn("Do NOT ask the visitor to reply I approve again", state)

    def test_opening_turn_has_no_user_speech(self) -> None:
        self.assertFalse(_has_user_speech([]))
        self.assertFalse(_has_user_speech([{"role": "user", "content": "   "}]))
        self.assertTrue(_has_user_speech([{"role": "user", "content": "hello"}]))

    def test_opening_greeting_hammer(self) -> None:
        self.assertIn("Hannah", _opening_greeting("hammer"))

    def test_hammer_prompt_uses_sales_instructions_not_pen(self) -> None:
        session = CallSession()
        prompt = _build_prompt(session, "── PRODUCT CONTEXT ──\nSample wiki.", "hammer")
        self.assertIn("BROWSER LIVE DEMO MODE", prompt)
        self.assertIn("You are Hannah", prompt)
        self.assertNotIn("Sell Me This Pen", prompt)
        self.assertIn("Sample wiki.", prompt)
        self.assertIn("EMAIL & PHONE READ-BACK", prompt)
        self.assertIn("Is that exactly right?", prompt)
        self.assertIn("PRICING (AUTHORITATIVE", prompt)
        self.assertIn("31–60 cars: $399/mo", prompt)
        self.assertIn("Facebook AIA: $299/mo", prompt)
        self.assertIn("72 business hours", prompt)
        self.assertNotIn("2-4 weeks", prompt.lower())

    def test_pen_prompt_uses_pen_challenge(self) -> None:
        session = CallSession()
        prompt = _build_prompt(session, "", "pen")
        self.assertIn("pen", prompt.lower())

    def test_el_call_end_outbound_resolves_visitor_phone(self) -> None:
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
            acc = _build_accumulator_from_el_transcript(
                {
                    "conversation_id": "conv-out-1",
                    "conversation_initiation_client_data": {
                        "dynamic_variables": {"system__caller_id": "+17372056753"},
                    },
                    "transcript": [],
                }
            )
        self.assertEqual(acc.call_direction, "outbound")
        self.assertEqual(acc.values.get("phone"), "+15558675309")

    def test_el_call_end_x_customer_phone_header(self) -> None:
        acc = _build_accumulator_from_el_transcript(
            {
                "conversation_id": "conv-out-2",
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "system__caller_id": "+17372056753",
                        "X-Customer-Phone": "+15558675309",
                    },
                },
                "transcript": [],
            }
        )
        self.assertEqual(acc.call_direction, "outbound")
        self.assertEqual(acc.values.get("phone"), "+15558675309")

    def test_el_transcript_create_hammer_account_tool(self) -> None:
        acc = _build_accumulator_from_el_transcript(
            {
                "conversation_id": "conv-account-1",
                "transcript": [
                    {
                        "role": "agent",
                        "tool_calls": [
                            {
                                "tool_name": "create_hammer_account",
                                "parameters": {
                                    "email": "dealer@test.com",
                                    "dealership_name": "Test Auto",
                                },
                                "result": "ok — account created; PHASE C.1 only",
                            }
                        ],
                    }
                ],
            }
        )
        self.assertTrue(acc.account_created)
        self.assertEqual(acc.values.get("email"), "dealer@test.com")

    def test_hammer_browser_tools_exclude_pen_phase(self) -> None:
        pen_names = {t["name"] for t in pen_challenge_tool_definitions()}
        hammer_names = {t["name"] for t in hammer_browser_tool_definitions()}
        self.assertTrue({"begin_hammer_signup", "skip_pen_challenge", "set_buyer_product"} <= pen_names)
        self.assertFalse(hammer_names & {"begin_hammer_signup", "skip_pen_challenge", "set_buyer_product"})
        self.assertIn("capture_lead", hammer_names)
        self.assertIn("search_wiki", hammer_names)


if __name__ == "__main__":
    unittest.main()
