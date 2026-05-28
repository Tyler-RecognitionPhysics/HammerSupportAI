"""Tests for human-readable voice dashboard activity feed."""

from __future__ import annotations

import unittest

from voice_dashboard_activity import enrich_activity_feed, format_activity_event


class VoiceDashboardActivityTests(unittest.TestCase):
    def test_skips_latency_noise(self) -> None:
        fmt = format_activity_event(
            {"event_type": "latency", "detail": {"phase": "first_sse", "elapsed_ms": 5}}
        )
        self.assertTrue(fmt.get("skip"))

    def test_capture_lead_reads_plainly(self) -> None:
        fmt = format_activity_event(
            {
                "event_type": "tool",
                "call_id": "conv-abc123456789",
                "detail": {
                    "tool": "capture_lead",
                    "result_preview": "ok — agreement email queued for buyer@test.com",
                    "email": "buyer@test.com",
                    "dealership_name": "Test Motors",
                },
            }
        )
        self.assertFalse(fmt.get("skip"))
        self.assertEqual(fmt["category"], "Agreement")
        self.assertIn("Sent agreement email", fmt["title"])
        self.assertIn("buyer@test.com", fmt["title"])
        self.assertEqual(fmt["tone"], "success")

    def test_account_created_reads_plainly(self) -> None:
        fmt = format_activity_event(
            {
                "event_type": "tool",
                "call_id": "conv-1",
                "detail": {
                    "tool": "create_hammer_account",
                    "result_preview": "ok — account created; PHASE C.1 only",
                    "email": "buyer@test.com",
                },
            }
        )
        self.assertIn("Created Hammer Office account", fmt["title"])
        self.assertEqual(fmt["tone"], "success")

    def test_call_started(self) -> None:
        fmt = format_activity_event(
            {
                "event_type": "call_started",
                "call_id": "conv-1",
                "detail": {"scenario": "pen", "channel": "phone"},
            }
        )
        self.assertIn("Pen challenge", fmt["title"])
        self.assertIn("Phone call", fmt["title"])

    def test_enrich_filters_latency(self) -> None:
        events = [
            {"event_type": "latency", "detail": {"phase": "first_sse"}},
            {
                "event_type": "tool",
                "detail": {
                    "tool": "search_wiki",
                    "result_preview": "Hammer Drive pricing…",
                    "query": "Drive pricing",
                },
            },
        ]
        out = enrich_activity_feed(events, limit=5)
        self.assertEqual(len(out), 1)
        self.assertIn("activity", out[0])
        self.assertIn("Looked up Hammer product info", out[0]["activity"]["title"])


if __name__ == "__main__":
    unittest.main()
