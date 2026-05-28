"""Serverless (Vercel) Hammer signup field store — no Playwright session."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hammer_office_session as hammer_office_session_mod
from hammer_office import HammerAccountResult
from hammer_office_session import (
    _fill_hammer_account_field_serverless,
    _open_hammer_account_form_serverless,
    _serverless_save_record,
    _serverless_signup_path,
    account_already_created,
    clear_signup_submission_state,
    get_phase_b_missing_fields,
    get_session_values,
    signup_ready_for_phase_c,
)


class ServerlessSignupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_dir = hammer_office_session_mod._SERVERLESS_SIGNUP_DIR
        hammer_office_session_mod._SERVERLESS_SIGNUP_DIR = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        hammer_office_session_mod._SERVERLESS_SIGNUP_DIR = self._orig_dir
        self._tmpdir.cleanup()

    @patch.dict(os.environ, {"REALTIME_SALES_SERVERLESS": "1"}, clear=False)
    @patch("hammer_office_session.require_agreement_approval")
    def test_open_and_fill_persist_across_calls(self, _approve: object) -> None:
        email = "buyer@dealer.com"
        _open_hammer_account_form_serverless(
            email,
            dealership_name="Acme Motors",
            name="Jane Dealer",
        )
        self.assertTrue(_serverless_signup_path(email).is_file())
        out = _fill_hammer_account_field_serverless(email, "business_type", "LLC")
        self.assertTrue(out.get("ok"))
        self.assertNotIn("account_created", out)

    @patch.dict(os.environ, {"REALTIME_SALES_SERVERLESS": "1"}, clear=False)
    @patch("hammer_office_session.create_hammer_account")
    @patch("hammer_office_session.require_agreement_approval")
    def test_last_field_fill_submits_without_role_question(self, _approve: object, create_mock: object) -> None:
        create_mock.return_value = HammerAccountResult(
            ok=True,
            message="Hammer Office account created",
            account_url="https://office.hammer-corp.com/accounts/99",
        )
        email = "sync@dealer.com"
        _open_hammer_account_form_serverless(email, dealership_name="Sync Auto", name="Pat Dealer")
        for field, val in [
            ("business_type", "LLC"),
            ("phone", "5125550100"),
            ("website", "https://sync.com"),
            ("address", "1 Main St, Austin, TX 78701"),
        ]:
            _fill_hammer_account_field_serverless(email, field, val)
        out = _fill_hammer_account_field_serverless(email, "address", "1 Main St, Austin, TX 78701")
        self.assertTrue(out.get("account_created"))
        create_mock.assert_called_once()
        done, url = account_already_created(email)
        self.assertTrue(done)
        self.assertIn("accounts/99", url or "")

    @patch.dict(os.environ, {"REALTIME_SALES_SERVERLESS": "1"}, clear=False)
    def test_stale_submitted_flag_does_not_skip_phase_b(self) -> None:
        email = "stale@dealer.com"
        _serverless_save_record(
            email,
            values={
                "email": email,
                "dealership_name": "Stale Motors",
            },
            submitted=True,
            account_url="https://office.hammer-corp.com/accounts/old",
            confirmed_fields=set(),
        )
        self.assertTrue(account_already_created(email)[0])
        missing = get_phase_b_missing_fields(email)
        self.assertIn("legal business structure", missing)
        self.assertFalse(signup_ready_for_phase_c(email))
        clear_signup_submission_state(email)
        self.assertFalse(account_already_created(email)[0])

    @patch.dict(os.environ, {"REALTIME_SALES_SERVERLESS": "1"}, clear=False)
    def test_get_session_values_reads_serverless_record(self) -> None:
        email = "values@dealer.com"
        _serverless_save_record(
            email,
            values={
                "email": email,
                "name": "Tyler Bennett",
                "dealership_name": "Tyler 67",
                "currency": "USD",
            },
            submitted=False,
            account_url=None,
            confirmed_fields={"name"},
        )
        values = get_session_values(email)
        self.assertEqual(values["name"], "Tyler Bennett")
        self.assertEqual(values["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
