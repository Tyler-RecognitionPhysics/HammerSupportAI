"""Tests for SIP telephony latency helpers."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from sip_realtime import (
    SipRealtimeService,
    _realtime_session_patch,
    _sip_audio_input_capture,
    _sip_transcription_config,
    _sip_transcription_config_capture,
    extract_caller_phone_from_incoming_event,
    reset_sip_service_for_tests,
)
from voice_instructions import pen_challenge_instructions, warm_instruction_cache


class SipAcceptPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_sip_service_for_tests()

    def test_slim_accept_payload_locks_voice_and_audio(self) -> None:
        svc = SipRealtimeService(
            api_key="sk-test",
            webhook_secret="whsec_test",
            get_retriever=MagicMock(),
        )
        payload = svc._build_slim_accept_payload()
        self.assertEqual(payload["type"], "realtime")
        self.assertEqual(payload["model"], "gpt-realtime-2")
        self.assertIn("instructions", payload)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["output_modalities"], ["audio"])
        self.assertEqual(payload["audio"]["output"]["voice"], "shimmer")
        self.assertEqual(payload["audio"]["output"]["speed"], 1.0)
        self.assertEqual(payload["reasoning"], {"effort": "minimal"})

    def test_full_accept_payload_includes_latency_tuning(self) -> None:
        svc = SipRealtimeService(
            api_key="sk-test",
            webhook_secret="whsec_test",
            get_retriever=MagicMock(),
        )
        payload = svc._build_accept_payload()
        self.assertEqual(payload["reasoning"], {"effort": "minimal"})
        self.assertTrue(payload["parallel_tool_calls"])
        self.assertEqual(payload["output_modalities"], ["audio"])
        audio_in = payload["audio"]["input"]
        turn = audio_in["turn_detection"]
        self.assertEqual(turn["type"], "semantic_vad")
        self.assertEqual(turn["eagerness"], "low")
        self.assertFalse(turn["create_response"])
        self.assertFalse(turn["interrupt_response"])
        transcription = audio_in["transcription"]
        self.assertEqual(transcription["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(transcription["language"], "en")
        self.assertIn("Hammer", transcription["prompt"])
        out = payload["audio"]["output"]
        self.assertEqual(out["voice"], "shimmer")
        self.assertEqual(out["speed"], 1.0)

    def test_default_transcription_config_is_latency_neutral(self) -> None:
        cfg = _sip_transcription_config()
        self.assertEqual(cfg["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(cfg["language"], "en")
        self.assertIn("Hammer", cfg["prompt"])
        self.assertIn("Gmail", cfg["prompt"])

    def test_capture_transcription_config_upgrades_model(self) -> None:
        cfg = _sip_transcription_config_capture()
        self.assertEqual(cfg["model"], "gpt-4o-transcribe")
        self.assertEqual(cfg["language"], "en")
        self.assertIn("Hammer", cfg["prompt"])

    def test_capture_transcription_kill_switch(self) -> None:
        with patch.dict(
            os.environ,
            {"REALTIME_SALES_SIP_CAPTURE_TRANSCRIPTION_MODEL": "gpt-4o-mini-transcribe"},
        ):
            cfg = _sip_transcription_config_capture()
        self.assertEqual(cfg["model"], "gpt-4o-mini-transcribe")

    def test_capture_audio_input_uses_capture_transcription(self) -> None:
        audio_in = _sip_audio_input_capture()
        self.assertEqual(audio_in["transcription"]["model"], "gpt-4o-transcribe")
        self.assertEqual(audio_in["turn_detection"]["type"], "semantic_vad")

    def test_session_patch_includes_realtime_type(self) -> None:
        patch = _realtime_session_patch(
            instructions="x",
            audio={"input": {"turn_detection": {"type": "semantic_vad"}}},
        )
        self.assertEqual(patch["type"], "realtime")
        self.assertEqual(patch["instructions"], "x")


class CallerPhoneExtractionTests(unittest.TestCase):
    def _event(self, headers: list[dict[str, str]]) -> object:
        class Data:
            sip_headers = headers

        class Event:
            data = Data()

        return Event()

    def test_from_header_sip_uri(self) -> None:
        ev = self._event(
            [{"name": "From", "value": "sip:+15125550199@twilio.pstn.twilio.com"}]
        )
        self.assertEqual(extract_caller_phone_from_incoming_event(ev), "+15125550199")

    def test_p_asserted_identity_fallback(self) -> None:
        ev = self._event(
            [
                {"name": "From", "value": "anonymous"},
                {"name": "P-Asserted-Identity", "value": "<sip:+17372056753@example.com>"},
            ]
        )
        self.assertEqual(extract_caller_phone_from_incoming_event(ev), "+17372056753")

    def test_missing_headers(self) -> None:
        self.assertEqual(extract_caller_phone_from_incoming_event(self._event([])), "")


class InstructionCacheTests(unittest.TestCase):
    def test_warm_instruction_cache_is_idempotent(self) -> None:
        warm_instruction_cache()
        first = pen_challenge_instructions()
        warm_instruction_cache()
        second = pen_challenge_instructions()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
