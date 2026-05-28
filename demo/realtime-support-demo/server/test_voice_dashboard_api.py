"""Tests for voice dashboard API and instruction overrides."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from voice_dashboard_api import (
    VoiceSettingsPatch,
    _call_occurred_at,
    _filter_calls_today_central,
    _filter_calls_since,
    dashboard_settings_get,
    dashboard_settings_patch,
)
from voice_dashboard_store import clear_settings, init_db
from voice_instructions import clear_instruction_cache, get_default_prompts, pen_challenge_instructions


class VoiceDashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = Path(self._tmpdir.name) / "test.sqlite"
        self._env = patch.dict(
            os.environ,
            {"REALTIME_SALES_VOICE_DASHBOARD_DB": str(self._db)},
            clear=False,
        )
        self._env.start()
        clear_settings()
        init_db()

    def tearDown(self) -> None:
        clear_settings()
        clear_instruction_cache()
        self._env.stop()
        self._tmpdir.cleanup()

    def test_settings_get_includes_defaults(self) -> None:
        data = dashboard_settings_get()
        self.assertIn("defaults", data)
        self.assertIn("effective", data)
        self.assertTrue(data["defaults"]["pen_prompt"])
        self.assertTrue(data["effective"]["pen_prompt"])

    def test_prompt_override_takes_effect(self) -> None:
        custom = "CUSTOM PEN PROMPT FOR TEST"
        dashboard_settings_patch(VoiceSettingsPatch(pen_prompt=custom))
        self.assertEqual(pen_challenge_instructions(), custom)
        defaults = get_default_prompts()
        self.assertNotEqual(defaults["pen_prompt"], custom)

    def test_today_filter_uses_started_at_not_updated_at(self) -> None:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Chicago")
        today = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
        today_iso = today.astimezone(timezone.utc).isoformat(timespec="seconds")
        yesterday = (today - timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        calls = [
            {"call_id": "today", "started_at": today_iso, "updated_at": today_iso},
            {
                "call_id": "stale-touch",
                "started_at": yesterday,
                "updated_at": today_iso,
            },
        ]
        filtered = _filter_calls_today_central(calls)
        self.assertEqual([c["call_id"] for c in filtered], ["today"])

    def test_filter_since_ignores_updated_at_only(self) -> None:
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        calls = [{"call_id": "x", "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
        self.assertEqual(_filter_calls_since(calls, days=1), [])
        self.assertIsNone(_call_occurred_at({"call_id": "x", "updated_at": old}))


if __name__ == "__main__":
    unittest.main()
