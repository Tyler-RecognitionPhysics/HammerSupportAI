"""Tests for address → timezone inference."""

from __future__ import annotations

import unittest

from address_timezone import (
    hammer_timezone_label,
    iana_timezone_from_address,
    infer_hammer_timezone,
)


class AddressTimezoneTests(unittest.TestCase):
    def test_austin_tx_central(self) -> None:
        addr = "123 Main St, Austin, TX 78701"
        self.assertEqual(iana_timezone_from_address(addr), "America/Chicago")
        self.assertEqual(
            infer_hammer_timezone(addr),
            "Central Time (US & Canada)",
        )

    def test_los_angeles_ca_pacific(self) -> None:
        addr = "500 Auto Row, Los Angeles, CA 90012"
        self.assertEqual(iana_timezone_from_address(addr), "America/Los_Angeles")
        self.assertEqual(
            hammer_timezone_label(iana_timezone_from_address(addr)),
            "Pacific Time (US & Canada)",
        )

    def test_toronto_on_eastern(self) -> None:
        addr = "1000 Bay St, Toronto, ON M5J 2R8"
        self.assertEqual(iana_timezone_from_address(addr), "America/Toronto")

    def test_phoenix_az(self) -> None:
        addr = "1 Dealer Way, Phoenix, AZ 85001"
        self.assertEqual(iana_timezone_from_address(addr), "America/Phoenix")
        self.assertEqual(infer_hammer_timezone(addr), "Arizona")

    def test_match_form_option(self) -> None:
        opts = [
            "(GMT-08:00) Pacific Time (US & Canada)",
            "(GMT-06:00) Central Time (US & Canada)",
        ]
        picked = infer_hammer_timezone("Austin, TX 78701", form_options=opts)
        self.assertIn("Central", picked)


if __name__ == "__main__":
    unittest.main()
