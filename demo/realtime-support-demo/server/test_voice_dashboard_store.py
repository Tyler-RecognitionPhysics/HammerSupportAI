"""Tests for voice dashboard SQLite store."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_call_summary import VoiceCallLeadAccumulator
from voice_dashboard_store import (
    append_call_event,
    clear_settings,
    funnel_stats,
    get_call,
    get_setting,
    init_db,
    list_calls,
    register_active_session,
    set_setting,
    unregister_active_session,
    upsert_call_record,
)


class VoiceDashboardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = Path(self._tmpdir.name) / "test.sqlite"
        self._env = patch.dict(
            os.environ,
            {"REALTIME_SALES_VOICE_DASHBOARD_DB": str(self._db)},
            clear=False,
        )
        self._env.start()
        init_db()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmpdir.cleanup()

    def test_upsert_and_list_call(self) -> None:
        acc = VoiceCallLeadAccumulator(call_id="c-1", channel="browser")
        acc.touch_started()
        acc.set_value("email", "test@dealer.com")
        acc.capture_lead_fired = True
        upsert_call_record(acc)

        calls = list_calls(limit=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "c-1")
        self.assertEqual(calls[0]["values"]["email"], "test@dealer.com")
        self.assertTrue(calls[0]["capture_lead_fired"])

    def test_call_events_and_detail(self) -> None:
        acc = VoiceCallLeadAccumulator(call_id="c-2")
        upsert_call_record(acc)
        append_call_event(call_id="c-2", event_type="tool", detail={"tool": "capture_lead"})

        call = get_call("c-2")
        assert call is not None
        self.assertEqual(len(call["events"]), 1)
        self.assertEqual(call["events"][0]["event_type"], "tool")

    def test_settings_override(self) -> None:
        set_setting("chat_model", "gpt-4o")
        self.assertEqual(get_setting("chat_model"), "gpt-4o")
        clear_settings()
        self.assertIsNone(get_setting("chat_model"))

    def test_active_sessions_and_funnel(self) -> None:
        register_active_session("live-1", {"scenario": "pen"})
        stats = funnel_stats(days=7)
        self.assertEqual(stats["active_now"], 1)
        unregister_active_session("live-1")
        stats = funnel_stats(days=7)
        self.assertEqual(stats["active_now"], 0)


if __name__ == "__main__":
    unittest.main()
