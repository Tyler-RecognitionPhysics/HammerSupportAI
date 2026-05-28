"""Reliability tests for Hammer Office instant-mode session values."""

from __future__ import annotations

import unittest

from hammer_office_session import (
    _LiveSession,
    _instant_fill_response,
    _mark_field_confirmed,
    _missing_for_submit_session,
    _optimistic_store,
    _seed_open_values,
)


class SessionSeedTests(unittest.TestCase):
    def test_seed_open_values_populates_required_basics(self) -> None:
        sess = _LiveSession(email="buyer@dealer.com")
        prefilled = _seed_open_values(
            sess,
            "buyer@dealer.com",
            "Tylers auto 555",
            "",
            "Jane Dealer",
        )
        self.assertIn("email", prefilled)
        self.assertEqual(sess.values["email"], "buyer@dealer.com")
        self.assertEqual(sess.values["dealership_name"], "Tylers auto 555")
        self.assertEqual(sess.values["name"], "Jane Dealer")
        missing = _missing_for_submit_session(sess)
        self.assertNotIn("email", missing)
        self.assertNotIn("dealership_name", missing)
        self.assertNotIn("name", missing)  # seeded from Phase A with explicit name

    def test_instant_fill_does_not_pretend_account_created(self) -> None:
        sess = _LiveSession(email="a@b.com")
        _seed_open_values(sess, "a@b.com", "Acme", "", "Jane Dealer")
        for key, val in [
            ("name", "Jane Dealer"),
            ("business_type", "LLC"),
            ("phone", "5125550100"),
            ("website", "https://acme.com"),
            ("address", "1 Main St, Austin, TX 78701"),
            ("role", "owner"),
        ]:
            _optimistic_store(sess, key, val)
            _mark_field_confirmed(sess, key)
        out = _instant_fill_response(sess, "role", "role", "owner")
        self.assertTrue(out.get("ready_to_submit"))
        self.assertNotIn("account_created", out)


if __name__ == "__main__":
    unittest.main()
