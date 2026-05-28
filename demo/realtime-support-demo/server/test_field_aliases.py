"""Field alias normalization for incremental Hammer Office fill."""

import unittest

from hammer_office_session import normalize_field_key


class FieldAliasTests(unittest.TestCase):
    def test_address_aliases(self) -> None:
        self.assertEqual(normalize_field_key("full_address"), "address")
        self.assertEqual(normalize_field_key("business-address"), "address")
        self.assertEqual(normalize_field_key("address"), "address")


if __name__ == "__main__":
    unittest.main()
