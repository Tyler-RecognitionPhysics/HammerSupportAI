import unittest

from voice_call_outcomes import (
    call_matches_outcome_filter,
    call_needs_detail_enrichment,
    infer_outcomes_from_summary,
    primary_outcome,
)


class VoiceCallOutcomesTests(unittest.TestCase):
    def test_infer_email_from_summary(self) -> None:
        call: dict = {"values": {}}
        infer_outcomes_from_summary(
            call,
            "Sent the agreement email to buyer@dealer.com with the premise agreement.",
        )
        self.assertTrue(call["agreement_email_sent"])
        self.assertEqual(call["values"]["email"], "buyer@dealer.com")
        self.assertEqual(primary_outcome(call)["slug"], "email")

    def test_infer_deal_closed(self) -> None:
        call = {"account_created": True, "values": {}}
        self.assertEqual(primary_outcome(call)["label"], "Deal closed")
        self.assertTrue(call_matches_outcome_filter(call, "closed"))

    def test_filter_none(self) -> None:
        call = {"values": {}}
        self.assertTrue(call_matches_outcome_filter(call, "none"))
        self.assertFalse(call_matches_outcome_filter(call, "email"))

    def test_enrich_from_tool_call_in_transcript(self) -> None:
        from voice_call_outcomes import enrich_call_outcomes

        call = {
            "call_id": "conv-tool-1",
            "values": {"email": "buyer@test.com"},
            "pen_hammer_close_active": True,
            "agreement_email_sent": True,
        }
        data = {
            "transcript": [
                {
                    "role": "agent",
                    "tool_calls": [
                        {
                            "tool_name": "create_hammer_account",
                            "parameters": {"email": "buyer@test.com", "dealership_name": "Test Motors"},
                            "result": (
                                "ok — account created; PHASE C.1 only: ask if Welcome to Hammer email arrived"
                            ),
                        }
                    ],
                }
            ],
        }
        enrich_call_outcomes(call, data)
        self.assertTrue(call["account_created"])
        self.assertEqual(primary_outcome(call)["slug"], "closed")

    def test_enrich_does_not_skip_when_partial_flags(self) -> None:
        from voice_call_outcomes import enrich_call_outcomes

        call = {
            "call_id": "conv-partial",
            "pen_hammer_close_active": True,
            "interaction_summary": "Visitor signed up and a Hammer account was created during the call.",
            "values": {},
        }
        enrich_call_outcomes(call)
        self.assertTrue(call["account_created"])

    def test_infer_from_session_log(self) -> None:
        from voice_call_outcomes import infer_outcomes_from_session_log

        call = {"values": {}}
        infer_outcomes_from_session_log(
            call,
            ["Tool: create_hammer_account (ok — account created; PHASE C.1 only)"],
        )
        self.assertTrue(call["account_created"])

    def test_normalize_call_duration_from_range(self) -> None:
        from voice_call_outcomes import normalize_call_duration

        call = {
            "started_at": "2026-05-26T14:55:00+00:00",
            "ended_at": "2026-05-26T15:02:30+00:00",
        }
        normalize_call_duration(call)
        self.assertEqual(call["duration_secs"], 450)

    def test_call_needs_detail_enrichment(self) -> None:
        self.assertFalse(call_needs_detail_enrichment({"call_id": "c1", "account_created": True}))
        self.assertTrue(call_needs_detail_enrichment({"call_id": "conv_abc", "values": {}}))
        self.assertTrue(
            call_needs_detail_enrichment(
                {"call_id": "conv_x", "pen_hammer_close_active": True, "values": {}}
            )
        )
        self.assertTrue(
            call_needs_detail_enrichment(
                {"call_id": "conv_y", "i_approve_approved": True, "values": {}}
            )
        )
        self.assertFalse(
            call_needs_detail_enrichment(
                {
                    "call_id": "conv_z",
                    "agreement_email_sent": True,
                    "capture_lead_fired": True,
                    "values": {},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
