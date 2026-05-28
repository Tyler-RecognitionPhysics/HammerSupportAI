"""Visible-browser test: Hammer Office account creation (simulates voice AI PHASE B fills).

Usage (from server/):
  py -3 scripts/test_create_account_tylers.py

Optional env:
  HAMMER_TEST_EMAIL  — signup email (default: tbennett6025@gmail.com)
  HAMMER_TEST_DEALERSHIP — legal/display name (default: Tyler's Auto)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")

# Visible Chromium + step-by-step fills (not instant background mode)
os.environ.setdefault("HAMMER_OFFICE_USE_PLAYWRIGHT", "1")
os.environ["HAMMER_OFFICE_ALLOW_VISIBLE_BROWSER"] = "1"
os.environ["HAMMER_OFFICE_HEADLESS"] = "0"
os.environ["HAMMER_OFFICE_INSTANT"] = "0"
os.environ.setdefault("HAMMER_OFFICE_SLOW_MO", "400")
# Never auto-close Chromium after submit (overrides server/.env KEEP_OPEN_AFTER_SUBMIT=0)
os.environ["HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT"] = "1"
os.environ["HAMMER_OFFICE_KEEP_OPEN"] = "0"

from agreement_approvals import record_agreement_approval
from hammer_office import hammer_office_configured, use_playwright
from hammer_office_session import fill_hammer_account_field, open_hammer_account_form

DEALERSHIP = os.environ.get("HAMMER_TEST_DEALERSHIP", "Tyler's Auto").strip()
EMAIL = os.environ.get("HAMMER_TEST_EMAIL", "tbennett6025@gmail.com").strip().lower()
FILL_PAUSE_SEC = float(os.environ.get("HAMMER_TEST_FILL_PAUSE", "2"))


def main() -> int:
    if not hammer_office_configured():
        print("ERROR: Set HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD in server/.env")
        return 1
    if not use_playwright():
        print("ERROR: Set HAMMER_OFFICE_USE_PLAYWRIGHT=1")
        return 1

    print(f"Dealership / legal / display name: {DEALERSHIP!r}")
    print(f"Email: {EMAIL}")
    print("Chromium should open on your desktop — watch fields fill like the voice agent.\n")

    record_agreement_approval(EMAIL, reply_text="I approve", source="test_script")

    open_result = open_hammer_account_form(
        EMAIL,
        dealership_name=DEALERSHIP,
        display_name=DEALERSHIP,
        name="Tyler Bennett",
    )
    print("open_form:", json.dumps(open_result, indent=2))

    steps: list[tuple[str, str]] = [
        ("business_type", "Limited Liability Corporation"),
        ("phone", "5128831336"),
        ("website", "tylersauto.com"),
        ("address", "123 Main St, Austin, TX 78701"),
        ("role", "owner"),
    ]
    try:
        for field, value in steps:
            print(f"\n--- fill {field} = {value!r} ---")
            time.sleep(FILL_PAUSE_SEC)
            result = fill_hammer_account_field(EMAIL, field, value)
            print(json.dumps(result, indent=2))
            if result.get("account_created"):
                _wait_for_user_before_exit(success=True, result=result)
                return 0
            if result.get("submit_error"):
                _wait_for_user_before_exit(success=False, result=result)
                return 2
    except Exception as exc:
        print(f"\nERROR: {exc}")
        _wait_for_user_before_exit(success=False, result={"error": str(exc)})
        return 2

    _wait_for_user_before_exit(success=False, result=None)
    return 2


def _wait_for_user_before_exit(*, success: bool, result: dict | None) -> None:
    if success:
        print("\nSUCCESS — account created")
        if result:
            print("account_url:", result.get("account_url"))
    else:
        print("\nFAILED — account was not created")
        if result and result.get("submit_error"):
            print("submit_error:", result.get("submit_error"))
        elif result and result.get("error"):
            print(result.get("error"))
        print("Check Chromium — on 'company name taken' the server should append 3 digits and resubmit.")
    print("\nChromium is left open. Review the Hammer Office page.")
    print("Press Enter in this terminal when you are done (closes the browser and exits).")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    from hammer_office_session import close_hammer_office_session

    close_hammer_office_session(EMAIL)


if __name__ == "__main__":
    raise SystemExit(main())
