"""Instant fill response tests (no Playwright)."""

from __future__ import annotations

import unittest

from hammer_office_session import (
    _LiveSession,
    _instant_fill_response,
    _mark_field_confirmed,
    _missing_for_submit_session,
    _optimistic_store,
)


class InstantFillTests(unittest.TestCase):
    def test_optimistic_address_us(self) -> None:
        sess = _LiveSession(email="a@b.com")
        _optimistic_store(sess, "address", "123 Main St, Austin, TX 78701")
        _mark_field_confirmed(sess, "address")
        self.assertEqual(sess.values.get("currency"), "USD")
        out = _instant_fill_response(sess, "address", "address", "123 Main St, Austin, TX 78701")
        self.assertEqual(out.get("billing_country"), "US")
        self.assertIn("US", str(out.get("message")))
        self.assertTrue(_missing_for_submit_session(sess))

    def test_complete_triggers_phase_c_hint(self) -> None:
        sess = _LiveSession(email="a@b.com")
        vals = {
            "email": "a@b.com",
            "dealership_name": "Acme",
            "name": "Jane Dealer",
            "business_type": "LLC",
            "phone": "5125550100",
            "website": "https://acme.com",
            "address": "123 Main St, Austin, TX 78701",
            "currency": "USD",
        }
        for k, v in vals.items():
            _optimistic_store(sess, k, v)
            if k not in ("email", "dealership_name"):
                _mark_field_confirmed(sess, k)
        _optimistic_store(sess, "role", "owner")
        self.assertEqual(_missing_for_submit_session(sess), [])
        out = _instant_fill_response(sess, "role", "role", "owner")
        self.assertTrue(out.get("ready_to_submit"))


if __name__ == "__main__":
    unittest.main()
