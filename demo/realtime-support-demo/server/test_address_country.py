"""Tests for US/Canada inference from dealership address."""

from __future__ import annotations

import unittest

from address_timezone import (
    address_billing_context,
    country_from_address,
    infer_billing_currency_from_address,
    is_quebec_address,
)


class AddressCountryTests(unittest.TestCase):
    def test_us_tx_zip(self) -> None:
        addr = "123 Main St, Austin, TX 78701"
        self.assertEqual(country_from_address(addr), "US")
        self.assertEqual(infer_billing_currency_from_address(addr), "USD")
        self.assertFalse(is_quebec_address(addr))
        ctx = address_billing_context(addr)
        self.assertEqual(ctx["tax_field"], "none")

    def test_canada_on_postal(self) -> None:
        addr = "1000 Bay St, Toronto, ON M5J 2R8"
        self.assertEqual(country_from_address(addr), "CA")
        self.assertEqual(infer_billing_currency_from_address(addr), "CAD")
        self.assertFalse(is_quebec_address(addr))
        ctx = address_billing_context(addr)
        self.assertEqual(ctx["tax_field"], "gst_hst")

    def test_quebec_qst(self) -> None:
        addr = "500 Rue Sainte-Catherine, Montreal, QC H3B 1A1"
        self.assertEqual(country_from_address(addr), "CA")
        self.assertTrue(is_quebec_address(addr))
        ctx = address_billing_context(addr)
        self.assertEqual(ctx["tax_field"], "qst")

    def test_canada_keyword(self) -> None:
        addr = "88 Auto Lane, Calgary, Alberta, Canada"
        self.assertEqual(country_from_address(addr), "CA")


if __name__ == "__main__":
    unittest.main()
