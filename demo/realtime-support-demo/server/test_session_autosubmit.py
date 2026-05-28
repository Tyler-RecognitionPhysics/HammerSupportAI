"""Auto-submit readiness tests."""

import unittest

from hammer_office_session import _missing_for_submit, _request_from_session


class SessionAutosubmitTests(unittest.TestCase):
    def test_missing_until_complete(self) -> None:
        v = {
            "email": "a@b.com",
            "dealership_name": "Acme",
            "name": "Jane Dealer",
            "business_type": "LLC",
            "phone": "5125550100",
        }
        missing = _missing_for_submit(v)
        self.assertIn("website", missing)
        self.assertIn("address", missing)

    def test_complete(self) -> None:
        v = {
            "email": "a@b.com",
            "dealership_name": "Acme Motors",
            "name": "Jane Dealer",
            "business_type": "LLC",
            "phone": "5125550100",
            "website": "https://acme.com",
            "address": "1 Main St, Austin, TX 78701",
            "currency": "USD",
            "role": "owner",
        }
        self.assertEqual(_missing_for_submit(v), [])

    def test_request_from_session(self) -> None:
        req = _request_from_session(
            {
                "email": "a@b.com",
                "dealership_name": "Acme",
                "name": "Jane Dealer",
                "phone": "5125550100",
                "website": "acme.com",
                "address": "1 Main St, Austin, TX 78701",
                "business_type": "LLC",
                "currency": "USD",
                "role": "owner",
            }
        )
        self.assertEqual(req.legal_name, "Acme")
        self.assertEqual(req.cell_phone, "5125550100")


if __name__ == "__main__":
    unittest.main()
