"""Tests for the shared EMAIL & PHONE READ-BACK prompt rules.

These rules drive Hannah's first-pass capture behavior. They live in
``web/src/voice-contact-readback.ts`` and are mirrored into the SIP / browser
prompts. If anyone weakens the one-breath rule or re-introduces the always-spell
default, these tests fail loudly.
"""

from __future__ import annotations

import unittest

from voice_instructions import voice_contact_readback_rules


class VoiceContactReadbackContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = voice_contact_readback_rules()

    def test_one_breath_rule_is_present(self) -> None:
        text = self.rules.lower()
        self.assertIn("one breath", text)
        self.assertIn("first time", text)
        self.assertIn("one flowing line", text)

    def test_nato_alphabet_is_defined(self) -> None:
        for word in ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Mike", "November", "Sierra", "Tango", "Victor"):
            self.assertIn(word, self.rules, f"NATO word missing from readback rules: {word}")

    def test_confusable_letter_set_is_listed(self) -> None:
        text = self.rules
        for pair in ("M / N", "B / D / P / T / V", "F / S / X", "I / E / Y"):
            self.assertIn(pair, text, f"confusable group missing: {pair}")

    def test_provider_domains_spoken_naturally(self) -> None:
        for provider in ("Gmail", "Outlook", "Yahoo", "Hotmail", "iCloud"):
            self.assertIn(provider, self.rules)
        self.assertIn("spoken names", self.rules.lower())

    def test_full_letter_spelling_is_fallback_not_default(self) -> None:
        """Old rule made full letter spelling mandatory on every read-back.

        New rule keeps it as a fallback only — fails if anyone re-introduces
        the always-spell-every-character mandate.
        """
        text = self.rules.lower()
        self.assertIn("fallback", text)
        self.assertNotIn("mandatory — every time", text)
        self.assertNotIn("always say \"is that exactly right", text)

    def test_phone_grouped_by_area_prefix_line(self) -> None:
        text = self.rules.lower()
        self.assertTrue(
            "area" in text and "prefix" in text and "line" in text,
            "phone read-back grouping (area / prefix / line) was removed",
        )

    def test_immutable_value_rule_still_present(self) -> None:
        text = self.rules.lower()
        self.assertIn("immutable", text)
        self.assertIn("session email key", text)

    def test_anti_loop_rule_present(self) -> None:
        import re

        text = self.rules.lower()
        # Match "after two failed corrections" tolerant of markdown bolding,
        # e.g. "after **two** failed corrections" or "after two failed corrections".
        self.assertRegex(text, r"after\s+\**\s*two\s*\**\s+failed corrections")


class CaptureGuardTests(unittest.TestCase):
    def test_suspicious_email_warnings_trigger(self) -> None:
        from voice_tools import _suspicious_capture_warning

        self.assertTrue(_suspicious_capture_warning(email="", dealership="Acme"))
        self.assertTrue(_suspicious_capture_warning(email="not-an-email", dealership="Acme"))
        self.assertTrue(_suspicious_capture_warning(email="a@gmail.com", dealership="Acme"))
        self.assertTrue(_suspicious_capture_warning(email="12345@gmail.com", dealership="Acme"))
        # Custom domain with ambiguous letter pair
        self.assertTrue(
            _suspicious_capture_warning(email="alex@victorymotorsmn.com", dealership="Acme")
        )

    def test_clean_email_passes(self) -> None:
        from voice_tools import _suspicious_capture_warning

        self.assertEqual(
            _suspicious_capture_warning(email="tyler@gmail.com", dealership="Victory Motors"),
            "",
        )
        self.assertEqual(
            _suspicious_capture_warning(
                email="alex.benson@victorymotors.com",
                dealership="Victory Motors",
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
