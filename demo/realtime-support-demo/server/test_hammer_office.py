"""Tests for Hammer Office account form helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agreement_approvals import record_agreement_approval, register_pending_agreement
from hammer_office import (
    HammerAccountRequest,
    HammerOfficeError,
    address_is_hammer_placeholder,
    apply_account_fields,
    create_hammer_account,
    display_name_already_taken,
    extract_csrf_token,
    hammer_office_configured,
    parse_forms,
    playwright_keep_browser_after_submit,
    require_agreement_approval,
    resolve_business_type_value,
    uniquify_display_name,
)


LOGIN_HTML = """
<form action="/session" method="post">
<input name="_csrf_token" type="hidden" value="abc123token">
<input id="user_email" name="user[email]" type="email">
<input id="user_password" name="user[password]" type="password">
<button>Login</button>
</form>
"""

ACCOUNT_FORM_HTML = """
<form action="/accounts" method="post">
<input name="_csrf_token" type="hidden" value="csrf2">
<input name="account[email]" type="email" value="">
<input name="account[first_name]" type="text" value="">
<input name="account[last_name]" type="text" value="">
<input name="account[phone_str]" type="text" value="">
<input name="account[mobile_str]" type="text" value="">
<input name="account[website_url]" type="text" value="">
<input name="account[name]" type="text" value="">
<input name="account[owner_name]" type="text" value="">
<input name="account[legal_name]" type="text" value="">
<input name="account[timezone]" type="text" value="">
<button type="submit">Create account</button>
</form>
"""


class HammerOfficeHelperTests(unittest.TestCase):
    def test_address_placeholder_detection(self) -> None:
        self.assertTrue(address_is_hammer_placeholder("123 Main Street, Seattle, WA 98134"))
        self.assertFalse(address_is_hammer_placeholder("456 Oak Ave, Austin, TX 78701"))

    def test_business_type_aliases(self) -> None:
        self.assertEqual(resolve_business_type_value("LLC"), "Limited Liability Corporation")
        self.assertEqual(resolve_business_type_value("sole prop"), "Sole Proprietorship")
        self.assertEqual(resolve_business_type_value("Corporation"), "Corporation")
        self.assertEqual(resolve_business_type_value("S-Corp"), "Corporation")

    def test_dealership_category_is_not_business_type(self) -> None:
        with self.assertRaisesRegex(HammerOfficeError, "legal structure"):
            resolve_business_type_value("auto dealership")
        with self.assertRaisesRegex(HammerOfficeError, "legal structure"):
            resolve_business_type_value("powersports")

    def test_extract_csrf(self) -> None:
        self.assertEqual(extract_csrf_token(LOGIN_HTML), "abc123token")

    def test_parse_account_form(self) -> None:
        forms = parse_forms(ACCOUNT_FORM_HTML)
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["action"], "/accounts")
        self.assertIn("account[email]", forms[0]["fields"])

    def test_apply_account_fields(self) -> None:
        forms = parse_forms(ACCOUNT_FORM_HTML)
        req = HammerAccountRequest(
            email="buyer@dealer.com",
            name="Jane Dealer",
            legal_name="Victory Motors LLC",
            display_name="Victory Motors",
            phone="5125550100",
            cell_phone="5125550101",
            website="victorymotors.com",
            address="123 Main St, Austin, TX 78701",
            business_type="LLC",
            currency="USD",
            dealership_name="Victory Motors",
            role="general-manager",
            selected_plan="Hammer Drive 31-60",
        )
        fields = apply_account_fields(forms[0], req)
        self.assertEqual(fields["account[email]"], "buyer@dealer.com")
        self.assertEqual(fields["account[first_name]"], "Jane")
        self.assertEqual(fields["account[last_name]"], "Dealer")
        self.assertEqual(fields["account[phone_str]"], "5125550100")
        self.assertEqual(fields["account[mobile_str]"], "5125550101")
        self.assertEqual(fields["account[website_url]"], "https://victorymotors.com")
        self.assertEqual(fields["account[name]"], "Victory Motors")
        self.assertEqual(fields["account[legal_name]"], "Victory Motors LLC")
        self.assertEqual(fields["account[timezone]"], "Central Time (US & Canada)")
        self.assertEqual(fields["account[owner_name]"], "Jane Dealer")

    def test_require_approval_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["REALTIME_SALES_APPROVALS_PATH"] = str(Path(tmp) / "a.json")
            try:
                register_pending_agreement("pending@dealer.com")
                with self.assertRaises(HammerOfficeError):
                    require_agreement_approval("pending@dealer.com")
                record_agreement_approval("pending@dealer.com", reply_text="I approve", source="test")
                require_agreement_approval("pending@dealer.com")
            finally:
                os.environ.pop("REALTIME_SALES_APPROVALS_PATH", None)

    def test_create_not_configured(self) -> None:
        os.environ.pop("HAMMER_OFFICE_EMAIL", None)
        os.environ.pop("HAMMER_OFFICE_PASSWORD", None)
        self.assertFalse(hammer_office_configured())
        with self.assertRaises(HammerOfficeError):
            create_hammer_account(
                HammerAccountRequest(email="x@y.com", dealership_name="Acme"),
                skip_approval_check=True,
            )

    def test_display_name_taken_detection(self) -> None:
        self.assertTrue(
            display_name_already_taken("Display name has already been taken"),
        )
        self.assertFalse(display_name_already_taken("Email is invalid"))

    def test_company_name_taken_detection(self) -> None:
        from hammer_office import company_name_already_taken

        msg = "There was a problem creating the account: Company name has already been taken."
        self.assertTrue(company_name_already_taken(msg))
        self.assertFalse(display_name_already_taken(msg))
        self.assertTrue(company_name_already_taken("Legal name is already taken"))
        self.assertTrue(
            company_name_already_taken(
                "There was a problem creating the account: Company name is already taken."
            )
        )

    def test_uniquify_display_name_suffix(self) -> None:
        out = uniquify_display_name("Victory Motors")
        self.assertTrue(out.startswith("Victory Motors"))
        self.assertEqual(len(out), len("Victory Motors") + 3)
        self.assertTrue(out[-3:].isdigit())

    def test_uniquify_legal_name_suffix(self) -> None:
        from hammer_office import uniquify_legal_name

        out = uniquify_legal_name("Bennett Test Motors LLC")
        self.assertTrue(out.startswith("Bennett Test Motors LLC"))
        self.assertTrue(out[-3:].isdigit())

    def test_account_create_succeeded(self) -> None:
        from hammer_office import account_create_succeeded

        self.assertFalse(account_create_succeeded("https://office.hammer-corp.com/accounts/new"))
        self.assertFalse(account_create_succeeded("https://office.hammer-corp.com/accounts"))
        self.assertTrue(account_create_succeeded("https://office.hammer-corp.com/accounts/12345"))

    def test_keep_browser_after_submit_visible_default(self) -> None:
        from hammer_office import hammer_office_runtime_is_deployed, playwright_headless

        os.environ.pop("VERCEL", None)
        os.environ.pop("VERCEL_ENV", None)
        os.environ.pop("REALTIME_SALES_SERVERLESS", None)
        os.environ.pop("HAMMER_OFFICE_ALLOW_VISIBLE_BROWSER", None)
        os.environ.pop("REALTIME_SALES_FORCE_LOCAL", None)
        os.environ.pop("HAMMER_OFFICE_DEBUG", None)
        os.environ["HAMMER_OFFICE_HEADLESS"] = "0"
        os.environ["HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT"] = "0"
        self.assertFalse(hammer_office_runtime_is_deployed())
        self.assertFalse(playwright_headless())
        # Visible mode always keeps the browser open (KEEP_OPEN_AFTER_SUBMIT ignored)
        self.assertTrue(playwright_keep_browser_after_submit())
        os.environ["HAMMER_OFFICE_HEADLESS"] = "1"
        os.environ["HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT"] = "0"
        self.assertTrue(playwright_headless())
        self.assertFalse(playwright_keep_browser_after_submit())

    def test_deployed_runtime_forces_headless(self) -> None:
        from hammer_office import (
            hammer_office_runtime_is_deployed,
            playwright_headless,
            playwright_keep_browser_after_submit,
            playwright_keep_open_ms,
        )

        os.environ.pop("HAMMER_OFFICE_ALLOW_VISIBLE_BROWSER", None)
        os.environ["HAMMER_OFFICE_HEADLESS"] = "0"
        os.environ["VERCEL"] = "1"
        self.assertTrue(hammer_office_runtime_is_deployed())
        self.assertTrue(playwright_headless())
        self.assertEqual(playwright_keep_open_ms(), 0)
        self.assertFalse(playwright_keep_browser_after_submit())
        os.environ.pop("VERCEL", None)


class ServerlessHammerOfficeTests(unittest.TestCase):
    def test_create_hammer_account_on_serverless_uses_fly_proxy(self) -> None:
        os.environ["REALTIME_SALES_SERVERLESS"] = "1"
        os.environ["HAMMER_OFFICE_EMAIL"] = "staff@test.com"
        os.environ["HAMMER_OFFICE_PASSWORD"] = "secret"
        req = HammerAccountRequest(
            email="buyer@dealer.com",
            name="Bennett",
            dealership_name="Tyler76",
            phone="9739083881",
            website="tyler76.com",
            address="123 Easy Street, Austin, Texas 78725",
            business_type="LLC",
            currency="USD",
            role="Owner",
        )
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "ok": True,
            "message": "Hammer Office account created",
            "account_url": "https://office.hammer-corp.com/accounts/abc",
            "dry_run": False,
        }
        with patch("hammer_office.require_agreement_approval"):
            with patch("hammer_office.httpx.Client") as client_cls:
                client_cls.return_value.__enter__.return_value.post.return_value = mock_response
                with patch("hammer_office_session.record_account_created") as record:
                    result = create_hammer_account(req)
        self.assertTrue(result.ok)
        self.assertIn("accounts/abc", result.account_url or "")
        record.assert_called_once()
        post_url = client_cls.return_value.__enter__.return_value.post.call_args[0][0]
        self.assertIn("hammer-voice-telephony.fly.dev", post_url)
        os.environ.pop("REALTIME_SALES_SERVERLESS", None)


if __name__ == "__main__":
    unittest.main()
