"""Create Hammer Office accounts at https://office.hammer-corp.com/accounts/new.

Requires an authenticated Hammer staff session (HAMMER_OFFICE_EMAIL / HAMMER_OFFICE_PASSWORD).
Uses httpx + HTML form parsing by default; set HAMMER_OFFICE_USE_PLAYWRIGHT=1 for browser automation.
"""

from __future__ import annotations

import logging
import os
import random
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

from address_timezone import infer_hammer_timezone
from agreement_approvals import agreement_approval_status
from lead_zapier import normalize_phone_e164, split_name

_log = logging.getLogger(__name__)


class HammerOfficeError(RuntimeError):
    """Account creation failed or is not configured."""


@dataclass(frozen=True)
class HammerAccountRequest:
    email: str
    name: str = ""
    legal_name: str = ""
    display_name: str = ""
    phone: str = ""
    cell_phone: str = ""
    website: str = ""
    address: str = ""
    business_type: str = ""
    timezone: str = ""
    currency: str = ""
    gst_hst: str = ""
    qst: str = ""
    dealership_name: str = ""
    role: str = ""
    selected_plan: str = ""


@dataclass(frozen=True)
class HammerAccountResult:
    ok: bool
    message: str
    account_url: str | None = None
    dry_run: bool = False


def hammer_office_configured() -> bool:
    return bool(office_login_email() and office_login_password())


def office_base_url() -> str:
    return os.environ.get("HAMMER_OFFICE_BASE_URL", "https://office.hammer-corp.com").rstrip("/")


def office_login_email() -> str:
    return os.environ.get("HAMMER_OFFICE_EMAIL", "").strip()


def office_login_password() -> str:
    return os.environ.get("HAMMER_OFFICE_PASSWORD", "").strip()


def office_dry_run() -> bool:
    return os.environ.get("HAMMER_OFFICE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def use_playwright() -> bool:
    return os.environ.get("HAMMER_OFFICE_USE_PLAYWRIGHT", "").strip().lower() in ("1", "true", "yes")


def hammer_office_serverless() -> bool:
    """Vercel/Lambda — no persistent Playwright sessions or background threads after response."""
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in ("1", "true", "yes")


def use_playwright_for_account_create() -> bool:
    """Playwright one-shot create is for long-running hosts (local/Fly), not Vercel serverless."""
    return use_playwright() and not hammer_office_serverless()


def _runtime_is_cloud_host() -> bool:
    """True on Vercel, Fly, or other serverless/production cloud hosts."""
    if os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("VERCEL", "").strip() == "1":
        return True
    if os.environ.get("VERCEL_ENV", "").strip():
        return True
    if os.environ.get("FLY_APP_NAME", "").strip():
        return True
    return False


def hammer_office_debug_mode() -> bool:
    """Local-only: visible Chromium, slower fills, debug routes."""
    if _runtime_is_cloud_host():
        return False
    if os.environ.get("REALTIME_SALES_FORCE_LOCAL", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("HAMMER_OFFICE_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        return True
    # server/.env with HAMMER_OFFICE_HEADLESS=0 counts as local debug (no extra flag needed)
    if os.environ.get("HAMMER_OFFICE_HEADLESS", "1").strip().lower() in ("0", "false", "no"):
        return True
    return False


def hammer_office_runtime_is_deployed() -> bool:
    """
    True on Vercel, Fly, or other serverless hosts.
    Deployed runtimes always use headless Chromium (no desktop window).
    Local visible-browser testing: HAMMER_OFFICE_DEBUG=1 or HAMMER_OFFICE_HEADLESS=0.
    """
    if hammer_office_debug_mode():
        return False
    if os.environ.get("HAMMER_OFFICE_ALLOW_VISIBLE_BROWSER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return _runtime_is_cloud_host()


def playwright_headless() -> bool:
    """False opens a visible Chromium window (local testing only)."""
    if hammer_office_debug_mode():
        return False
    if hammer_office_runtime_is_deployed():
        return True
    return os.environ.get("HAMMER_OFFICE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")


def playwright_slow_mo() -> int:
    """Milliseconds between Playwright actions — auto-enabled when the browser is visible."""
    raw = os.environ.get("HAMMER_OFFICE_SLOW_MO", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 0 if playwright_headless() else 150


def playwright_keep_open_ms() -> int:
    """How long to leave the browser open after filling (visible local testing only)."""
    if hammer_office_runtime_is_deployed() or playwright_headless():
        return 0
    raw = os.environ.get("HAMMER_OFFICE_KEEP_OPEN", "").strip().lower()
    if raw in ("", "0", "false", "no"):
        return 0
    if raw in ("1", "true", "yes"):
        return 120_000
    try:
        return max(0, int(float(raw) * 1000))
    except ValueError:
        return 120_000


def _name_taken_phrases_present(page_text: str) -> bool:
    t = " ".join(page_text.lower().split())
    if not t:
        return False
    taken_phrases = (
        "already been taken",
        "already taken",
        "has already been taken",
        "is already taken",
        "already in use",
        "must be unique",
        "name is taken",
        "name has been taken",
    )
    return any(p in t for p in taken_phrases)


def company_name_already_taken(page_text: str) -> bool:
    """True when Hammer Office rejected submit because account[legal_name] / company name exists."""
    if not _name_taken_phrases_present(page_text):
        return False
    t = " ".join(page_text.lower().split())
    if "company name" in t or "legal name" in t:
        return True
    if "problem creating the account" in t and "company" in t:
        return True
    return False


def display_name_already_taken(page_text: str) -> bool:
    """True when Hammer Office rejected submit because the public/display name exists."""
    if company_name_already_taken(page_text):
        return False
    t = " ".join(page_text.lower().split())
    if not t:
        return False
    if _name_taken_phrases_present(page_text):
        if "display" in t or "account[name]" in t:
            return True
        if "name" in t and "company name" not in t:
            return True
    if "display name" in t and _name_taken_phrases_present(page_text):
        return True
    return False


def uniquify_account_name(name: str, *, fallback: str = "Dealership") -> str:
    """Append three random digits to bypass duplicate company or display names."""
    base = re.sub(r"\d{3}$", "", name.strip())
    if not base:
        base = fallback
    return f"{base}{random.randint(100, 999)}"


def uniquify_display_name(display: str) -> str:
    """Append three random digits to bypass duplicate public display names."""
    return uniquify_account_name(display, fallback="Dealership")


def uniquify_legal_name(legal: str) -> str:
    """Append three random digits to bypass duplicate legal / company names."""
    return uniquify_account_name(legal, fallback="Dealership LLC")


def _legal_name_from_store_or_page(page: Any, store: dict[str, str]) -> str:
    current = (store.get("legal_name") or store.get("dealership_name") or "").strip()
    if not current and page.locator('input[name="account[legal_name]"]').count():
        try:
            current = page.locator('input[name="account[legal_name]"]').first.input_value().strip()
        except Exception:
            pass
    return current


def _display_name_from_store_or_page(page: Any, store: dict[str, str]) -> str:
    current = (store.get("display_name") or store.get("dealership_name") or "").strip()
    if not current and page.locator('input[name="account[name]"]').count():
        try:
            current = page.locator('input[name="account[name]"]').first.input_value().strip()
        except Exception:
            pass
    return current


_COMPANY_IDENTITY_INPUTS: tuple[str, ...] = (
    "account[legal_name]",
    "account[company]",
    "account[name]",
    "account[display_name]",
)


def fill_all_company_identity_fields(page: Any, name: str, store: dict[str, str]) -> None:
    """Set legal, company, and display name inputs — 'Company name taken' often targets account[company]."""
    name = name.strip()
    if not name:
        return
    store["legal_name"] = name
    store["dealership_name"] = name
    store["display_name"] = name
    for field_name in _COMPANY_IDENTITY_INPUTS:
        loc = page.locator(f'input[name="{field_name}"]:visible')
        if loc.count():
            playwright_fill_text(page, field_name, name, fast=True)


def account_create_succeeded(url: str) -> bool:
    """True only when Hammer navigated to a created account — not merely off /accounts/new."""
    from urllib.parse import urlparse

    path = (urlparse(url).path or "").rstrip("/")
    if not path or path.endswith("/accounts/new"):
        return False
    if path == "/accounts":
        return False
    # Match both numeric IDs (/accounts/123) and UUID-style IDs (/accounts/uuid-here)
    return bool(re.search(r"/accounts/[\w-]+", path))


def account_url_from_submit(url: str) -> str | None:
    return url if account_create_succeeded(url) else None


def _page_form_feedback_text(page: Any) -> str:
    """Visible validation/errors (Turbo alerts + body text), not raw HTML."""
    chunks: list[str] = []
    for selector in (
        ".alert",
        ".alert-danger",
        ".alert-error",
        "#error_explanation",
        ".field_with_errors",
        '[role="alert"]',
        ".flash",
        ".notification",
    ):
        try:
            loc = page.locator(selector)
            if loc.count():
                text = loc.first.inner_text(timeout=2_000)
                if text and text.strip():
                    chunks.append(text.strip())
        except Exception:
            continue
    try:
        body = page.locator("body").inner_text(timeout=5_000)
        if body and body.strip():
            chunks.append(body.strip())
    except Exception:
        pass
    return "\n".join(chunks)


def _wait_for_account_form_after_submit(page: Any) -> None:
    """Let Turbo/validation render duplicate-name errors before reading feedback text."""
    page.wait_for_timeout(800)
    try:
        page.wait_for_function(
            """() => {
              if (!location.pathname.includes('/accounts/new')) return true;
              const t = (document.body && document.body.innerText || '').toLowerCase();
              if (t.includes('already been taken') || t.includes('already taken')
                  || t.includes('is already taken') || t.includes('already in use')
                  || t.includes('problem creating the account')
                  || t.includes('must be unique')) return true;
              return !!document.querySelector('.field_with_errors, .alert, .alert-danger, #error_explanation');
            }""",
            timeout=6_000,
        )
    except Exception:
        page.wait_for_timeout(1_200)


def _apply_legal_uniquify_retry(page: Any, store: dict[str, str]) -> bool:
    """Append 3 random digits to company/legal/display fields. Returns False if no name to fix."""
    current_legal = _legal_name_from_store_or_page(page, store)
    if not current_legal:
        return False
    new_legal = uniquify_legal_name(current_legal)
    print(
        f"[hammer-office] company name taken — retry with {new_legal!r} (was {current_legal!r})",
        flush=True,
    )
    fill_all_company_identity_fields(page, new_legal, store)
    return True


def _apply_display_uniquify_retry(page: Any, store: dict[str, str]) -> bool:
    current = _display_name_from_store_or_page(page, store)
    if not current:
        return False
    new_display = uniquify_display_name(current)
    print(
        f"[hammer-office] display name taken — retry with {new_display!r} (was {current!r})",
        flush=True,
    )
    fill_all_company_identity_fields(page, new_display, store)
    return True


def _retry_duplicate_name_on_form(page: Any, store: dict[str, str], feedback: str) -> bool:
    """Apply legal/display suffix retries when Hammer shows a duplicate-name error."""
    if company_name_already_taken(feedback):
        return _apply_legal_uniquify_retry(page, store)
    if display_name_already_taken(feedback):
        return _apply_display_uniquify_retry(page, store)
    if _name_taken_phrases_present(feedback):
        if _apply_legal_uniquify_retry(page, store):
            return True
        return _apply_display_uniquify_retry(page, store)
    return False


def playwright_keep_browser_after_submit() -> bool:
    """
    When True, do not call browser.close() after account creation.
    Visible Chromium (local HAMMER_OFFICE_HEADLESS=0) stays open for manual review.
    Deployed runtimes always close immediately (no visible window, no idle browser).
    """
    if hammer_office_runtime_is_deployed():
        return False
    if not playwright_headless():
        return True
    raw = os.environ.get("HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT", "1").strip().lower()
    return raw not in ("0", "false", "no")


def keep_browser_open_on_submit_failure() -> bool:
    """Visible Chromium stays open when submit fails (local testing only)."""
    if hammer_office_runtime_is_deployed():
        return False
    return not playwright_headless()


BUSINESS_TYPE_ALIASES: dict[str, str] = {
    "llc": "Limited Liability Corporation",
    "l.l.c": "Limited Liability Corporation",
    "limited liability company": "Limited Liability Corporation",
    "limited liability corporation": "Limited Liability Corporation",
    "ltd": "Limited Liability Corporation",
    "limited": "Limited Liability Corporation",
    "sole proprietorship": "Sole Proprietorship",
    "sole prop": "Sole Proprietorship",
    "sole proprietor": "Sole Proprietorship",
    "partnership": "Partnership",
    "llp": "Partnership",
    "corp": "Corporation",
    "corporation": "Corporation",
    "inc": "Corporation",
    "incorporated": "Corporation",
    "s-corp": "Corporation",
    "s corp": "Corporation",
    "c-corp": "Corporation",
    "c corp": "Corporation",
    "co-operative": "Co-operative",
    "cooperative": "Co-operative",
    "co-op": "Co-operative",
    "non-profit": "Non-profit Corporation",
    "nonprofit": "Non-profit Corporation",
    "non profit": "Non-profit Corporation",
}


_DEALERSHIP_CATEGORY_AS_BUSINESS_TYPE_RE = re.compile(
    r"\b("
    r"auto(?:motive)?|cars?|trucks?|motorcycles?|power\s*sports?|powersports?|"
    r"rv|marine|boats?|dealer|dealership|franchise|franchised|independent|"
    r"used\s+cars?|new\s+cars?"
    r")\b",
    re.I,
)


def resolve_business_type_value(spoken: str) -> str:
    raw = spoken.strip()
    if not raw:
        return ""
    key = raw.lower().replace(".", "").strip()
    if key in BUSINESS_TYPE_ALIASES:
        return BUSINESS_TYPE_ALIASES[key]
    for alias, value in BUSINESS_TYPE_ALIASES.items():
        if alias in key or key in alias:
            return value
    for official in (
        "Sole Proprietorship",
        "Partnership",
        "Limited Liability Corporation",
        "Co-operative",
        "Non-profit Corporation",
        "Corporation",
    ):
        official_key = official.lower()
        if official_key == key or key in official_key or official_key in key:
            return official
    if _DEALERSHIP_CATEGORY_AS_BUSINESS_TYPE_RE.search(key):
        raise HammerOfficeError(
            "business type needs legal structure, not dealership category — ask: "
            "Is it an LLC, corporation, partnership, or sole proprietorship?"
        )
    return raw


def require_agreement_approval(email: str) -> None:
    from agreement_approvals import ensure_voice_call_approval

    status = agreement_approval_status(email.strip(), wait_seconds=0)
    if status.get("approved"):
        return
    if status.get("pending"):
        fallback = ensure_voice_call_approval(email.strip())
        if fallback.get("approved"):
            return
    raise HammerOfficeError(
        "Agreement email not approved yet — visitor must reply I approve before account creation"
    )


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._in_form = False
        self._in_textarea = False
        self._textarea_name: str | None = None
        self._textarea_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._in_form = True
            self._current = {
                "action": attr.get("action", ""),
                "method": (attr.get("method") or "get").lower(),
                "fields": {},
            }
            return
        if not self._in_form or self._current is None:
            return
        if tag == "input":
            name = attr.get("name")
            if not name:
                return
            input_type = (attr.get("type") or "text").lower()
            if input_type in ("submit", "button", "image"):
                return
            value = attr.get("value", "")
            if input_type in ("checkbox", "radio"):
                if attr.get("checked") is not None:
                    self._current["fields"][name] = value
            else:
                self._current["fields"][name] = value
        elif tag == "select":
            self._current["_select_name"] = attr.get("name", "")
            self._current["_select_options"] = []
        elif tag == "option" and "_select_options" in self._current:
            val = attr.get("value", "")
            label = ""
            self._current["_select_options"].append((val, label, attr.get("selected") is not None))
        elif tag == "textarea":
            self._in_textarea = True
            self._textarea_name = attr.get("name")
            self._textarea_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            select_name = self._current.pop("_select_name", None)
            options = self._current.pop("_select_options", None)
            if select_name and options:
                chosen = next((v for v, _, sel in options if sel), None)
                if chosen is None and options:
                    chosen = options[0][0]
                if chosen is not None:
                    self._current["fields"][select_name] = chosen
            self.forms.append(self._current)
            self._current = None
            self._in_form = False
        elif tag == "textarea" and self._in_textarea and self._textarea_name:
            self._current["fields"][self._textarea_name] = "".join(self._textarea_chunks)
            self._in_textarea = False
            self._textarea_name = None
            self._textarea_chunks = []
        elif tag == "select" and self._current is not None:
            select_name = self._current.pop("_select_name", None)
            options = self._current.pop("_select_options", [])
            if select_name and options:
                chosen = next((v for v, _, sel in options if sel), options[0][0])
                self._current["fields"][select_name] = chosen

    def handle_data(self, data: str) -> None:
        if self._in_textarea:
            self._textarea_chunks.append(data)


def parse_forms(html: str) -> list[dict[str, Any]]:
    parser = _FormParser()
    parser.feed(html)
    return parser.forms


def extract_csrf_token(html: str) -> str:
    match = re.search(
        r'<input[^>]+name="_csrf_token"[^>]+value="([^"]+)"',
        html,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<input[^>]+value="([^"]+)"[^>]+name="_csrf_token"',
            html,
            re.IGNORECASE,
        )
    if not match:
        raise HammerOfficeError("Could not find CSRF token on Hammer Office page")
    return match.group(1)


def _pick_account_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    for form in forms:
        action = str(form.get("action", "")).lower()
        fields = form.get("fields") or {}
        if "account" in action or any("account" in k for k in fields):
            return form
    for form in forms:
        fields = form.get("fields") or {}
        if any("email" in k.lower() for k in fields) and "user[password]" not in fields:
            return form
    return forms[0] if forms else None


def _field_key(name: str) -> str | None:
    n = name.lower()
    if "user[password]" in n or n.endswith("[password]") and "reset" not in n:
        return None
    if "csrf" in n:
        return None
    if "email" in n and "confirm" not in n:
        return "email"
    if "first" in n or n.endswith("[fname]"):
        return "first_name"
    if "last" in n or "lname" in n or "surname" in n:
        return "last_name"
    if "hubspot" in n:
        return None
    if "ein" in n or "tax_id" in n or "taxid" in n:
        return None
    if "legal" in n and "name" in n:
        return "legal_name"
    if "display" in n and "name" in n:
        return "display_name"
    if "mobile" in n or "cell" in n:
        return "cell_phone"
    if "phone" in n:
        return "phone"
    if "website" in n or "domain" in n or "url" in n:
        return "website"
    if "address" in n or "street" in n or "city" in n or "postal" in n or "zip" in n:
        return "address"
    if "timezone" in n or "time_zone" in n:
        return "timezone"
    if "currency" in n:
        return "currency"
    if "business_type" in n or "entity_type" in n or "company_type" in n:
        return "business_type"
    if "gst" in n or "hst" in n:
        return "gst_hst"
    if "qst" in n:
        return "qst"
    if any(x in n for x in ("dealership", "dealer_name", "company", "account_name", "store")):
        return "dealership_name"
    if "role" in n or "title" in n or "position" in n:
        return "role"
    if "plan" in n or "product" in n or "subscription" in n:
        return "selected_plan"
    if "name" in n and "user" not in n and "account" in n:
        return "dealership_name"
    return None


def apply_account_fields(form: dict[str, Any], req: HammerAccountRequest) -> dict[str, str]:
    fields: dict[str, str] = {k: str(v) for k, v in (form.get("fields") or {}).items()}
    first, last = split_name(req.name)
    phone = normalize_phone_e164(req.phone) if req.phone.strip() else ""
    cell = normalize_phone_e164(req.cell_phone) if req.cell_phone.strip() else phone
    website = req.website.strip()
    if website and not website.startswith(("http://", "https://")):
        website = f"https://{website}"

    legal = (req.legal_name or req.dealership_name or req.name).strip()
    display = (req.display_name or req.dealership_name or legal).strip()
    address = req.address.strip()
    timezone = req.timezone.strip() or infer_hammer_timezone(address)

    values: dict[str, str | None] = {
        "email": req.email.strip().lower(),
        "first_name": first,
        "last_name": last,
        "legal_name": legal,
        "display_name": display,
        "phone": phone,
        "cell_phone": cell,
        "website": website,
        "address": address,
        "business_type": resolve_business_type_value(req.business_type.strip()),
        "timezone": timezone,
        "currency": req.currency.strip(),
        "gst_hst": req.gst_hst.strip(),
        "qst": req.qst.strip(),
        "dealership_name": req.dealership_name.strip() or display,
        "role": req.role.strip(),
        "selected_plan": req.selected_plan.strip(),
    }

    for name, current in list(fields.items()):
        key = _field_key(name)
        if not key:
            continue
        val = values.get(key)
        if val:
            fields[name] = val

    # Hammer Office /accounts/new field names (verified on office.hammer-corp.com)
    owner = req.name.strip() or f"{first} {last}".strip()
    phone_display = req.phone.strip() or phone
    cell_display = req.cell_phone.strip() or cell
    explicit: dict[str, str | None] = {
        "account[email]": values["email"],
        "account[owner_name]": owner,
        "account[name]": values["display_name"],
        "account[legal_name]": values["legal_name"],
        "account[display_name]": values["display_name"],
        "account[phone_str]": phone_display,
        "account[mobile_str]": cell_display,
        "account[website_url]": website,
        "account[first_name]": values["first_name"],
        "account[last_name]": values["last_name"],
        "account[phone]": values["phone"],
        "account[website]": values["website"],
        "account[company]": values["dealership_name"],
        "account[role]": values["role"],
        "account[address]": values["address"],
        "account[business_type]": values["business_type"],
        "account[timezone]": values["timezone"],
        "account[currency]": values["currency"],
        "account[gst_hst]": values["gst_hst"],
        "account[qst]": values["qst"],
    }
    for name, val in explicit.items():
        if name in fields and val:
            fields[name] = val

    if not any(values["email"] and values["email"] in v for v in fields.values()):
        for name in fields:
            if _field_key(name) == "email" and values["email"]:
                fields[name] = values["email"]
                break

    return fields


def _login(client: httpx.Client, base: str) -> None:
    login_url = f"{base}/accounts/new"
    r = client.get(login_url, follow_redirects=True)
    r.raise_for_status()
    csrf = extract_csrf_token(r.text)
    continue_url = f"{base}/accounts/new"
    post_url = urljoin(str(r.url), "/session")
    resp = client.post(
        post_url,
        data={
            "_csrf_token": csrf,
            "user[email]": office_login_email(),
            "user[password]": office_login_password(),
            "continue": continue_url,
        },
        follow_redirects=True,
    )
    resp.raise_for_status()
    if "Login to Hammer" in resp.text and "user[email]" in resp.text:
        raise HammerOfficeError("Hammer Office login failed — check HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD")


def _click_login_button(page: Any) -> None:
    """Click the login submit button using multiple selector strategies."""
    _LOGIN_BUTTON_SELECTORS = [
        'form[action="/session"] button[type="submit"]',
        'form[action="/session"] input[type="submit"]',
        'form[action="/session"] button',
        '#new_user button[type="submit"]',
        '#new_user input[type="submit"]',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for selector in _LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(selector)
            if loc.count():
                loc.first.click(timeout=10_000)
                return
        except Exception:
            continue
    raise HammerOfficeError(
        "Hammer Office login button not found — login form may have changed"
    )


def _submit_account_form(
    client: httpx.Client,
    base: str,
    html: str,
    req: HammerAccountRequest,
) -> HammerAccountResult:
    forms = parse_forms(html)
    form = _pick_account_form(forms)
    if not form:
        raise HammerOfficeError("No account form found at /accounts/new — is the staff user allowed to create accounts?")

    action = str(form.get("action") or "").strip() or "/accounts"
    method = str(form.get("method") or "post").lower()
    if method != "post":
        raise HammerOfficeError(f"Unexpected account form method: {method}")

    post_url = urljoin(f"{base}/accounts/new", action)
    fields = apply_account_fields(form, req)
    if office_dry_run():
        return HammerAccountResult(
            ok=True,
            message="dry run — form filled but not submitted",
            dry_run=True,
        )

    display_key = "account[name]"
    legal_key = "account[legal_name]"
    final_url = ""
    body = ""
    for _attempt in range(5):
        try:
            resp = client.post(post_url, data=fields)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _log.error(
                "HTTP SUBMIT ERROR: status=%s, url=%s, response_text=%s, headers=%s",
                exc.response.status_code,
                exc.response.url,
                exc.response.text[:2000],
                dict(exc.response.headers),
            )
            raise
        final_url = str(resp.url)
        body = resp.text
        if "/accounts/new" not in final_url or "Login to Hammer" in body:
            break
        if company_name_already_taken(body) and legal_key in fields:
            old_legal = fields[legal_key]
            new_legal = uniquify_legal_name(old_legal)
            fields[legal_key] = new_legal
            fields[display_key] = new_legal
            if "account[company]" in fields:
                fields["account[company]"] = new_legal
            if "account[display_name]" in fields:
                fields["account[display_name]"] = new_legal
            continue
        if display_name_already_taken(body) and display_key in fields:
            fields[display_key] = uniquify_display_name(fields[display_key])
            if "account[display_name]" in fields:
                fields["account[display_name]"] = fields[display_key]
            continue
        break

    if "error" in body.lower() and "account" in body.lower() and "/accounts/new" in final_url:
        snippet = re.sub(r"\s+", " ", body)[:280]
        raise HammerOfficeError(f"Hammer Office rejected account creation: {snippet}")

    if "/accounts/new" in final_url and "Login to Hammer" not in body:
        raise HammerOfficeError("Account form may not have submitted — still on new account page")

    if not account_create_succeeded(final_url):
        raise HammerOfficeError(
            f"Hammer Office did not navigate to a created account (url={final_url})"
        )

    return HammerAccountResult(
        ok=True,
        message="Hammer Office account created",
        account_url=account_url_from_submit(final_url),
    )


def _create_via_fly_proxy(req: HammerAccountRequest) -> HammerAccountResult:
    """Vercel serverless cannot run Playwright or sticky httpx sessions — use Fly."""
    from agreement_approvals import _fly_approval_api_base

    import httpx

    base = _fly_approval_api_base()
    dealership = req.dealership_name.strip() or req.display_name.strip() or req.legal_name.strip()
    phone = req.phone.strip() or req.cell_phone.strip()
    payload = {
        "email": req.email.strip(),
        "name": req.name.strip(),
        "legal_name": (req.legal_name or dealership).strip(),
        "display_name": (req.display_name or dealership).strip(),
        "phone": phone,
        "cell_phone": (req.cell_phone or phone).strip(),
        "website": req.website.strip(),
        "address": req.address.strip(),
        "business_type": req.business_type.strip(),
        "timezone": req.timezone.strip(),
        "currency": (req.currency or "USD").strip(),
        "gst_hst": req.gst_hst.strip(),
        "qst": req.qst.strip(),
        "dealership_name": dealership,
        "role": (req.role or "Owner").strip(),
        "selected_plan": req.selected_plan.strip(),
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{base}/api/hammer/create-account", json=payload)
    except httpx.HTTPError as exc:
        raise HammerOfficeError(f"Hammer Office create via Fly failed: {exc}") from exc
    if response.status_code == 403:
        detail = response.json().get("detail", response.text[:200]) if response.text else "not approved"
        raise HammerOfficeError(str(detail))
    if not response.is_success:
        detail = response.text[:300] if response.text else f"HTTP {response.status_code}"
        try:
            detail = response.json().get("detail", detail)
        except Exception:
            pass
        raise HammerOfficeError(f"Hammer Office create via Fly returned {response.status_code}: {detail}")
    data = response.json()
    account_url = str(data.get("account_url") or "").strip() or None
    message = str(data.get("message") or "Hammer Office account created")
    dry_run = bool(data.get("dry_run"))
    if account_url and not dry_run:
        from hammer_office_session import record_account_created

        record_account_created(req.email.strip().lower(), account_url)
    return HammerAccountResult(
        ok=True,
        message=message,
        account_url=account_url,
        dry_run=dry_run,
    )


def _create_via_http(req: HammerAccountRequest) -> HammerAccountResult:
    base = office_base_url()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; HammerRealtimeSalesDemo/1.0; +https://www.hammer-corp.com)"
        ),
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=45.0) as client:
        _login(client, base)
        page = client.get(f"{base}/accounts/new")
        page.raise_for_status()
        if "Login to Hammer" in page.text and "user[password]" in page.text:
            raise HammerOfficeError("Not authenticated after login — staff account may lack access")
        return _submit_account_form(client, base, page.text, req)


def create_hammer_account(
    req: HammerAccountRequest,
    *,
    skip_approval_check: bool = False,
    prefer_direct: bool = False,
) -> HammerAccountResult:
    if not hammer_office_configured():
        raise HammerOfficeError(
            "Hammer Office automation is not configured — set HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD"
        )

    email = req.email.strip().lower()
    if not email:
        raise HammerOfficeError("email is required")

    if not skip_approval_check:
        require_agreement_approval(email)

    if not req.dealership_name.strip() and not req.name.strip():
        raise HammerOfficeError("dealership_name or name is required")

    if hammer_office_serverless() and not prefer_direct:
        return _create_via_fly_proxy(req)

    if use_playwright_for_account_create() and not prefer_direct:
        from hammer_office_session import record_account_created, session_browser_ready, submit_hammer_account_form

        if session_browser_ready(email):
            try:
                return submit_hammer_account_form(email, req)
            except HammerOfficeError:
                pass  # fall through to one-shot create below

    last_exc: HammerOfficeError | None = None
    for creator in (_create_with_playwright, _create_via_http):
        try:
            result = creator(req)
            if result.ok and not result.dry_run:
                from hammer_office_session import record_account_created

                record_account_created(email, result.account_url)
            return result
        except HammerOfficeError as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise HammerOfficeError("Account creation failed")


def _apply_display_name_on_page(page: Any, display: str, store: dict[str, str]) -> None:
    """Update Hammer public name fields (account[name]) after a duplicate-name rejection."""
    display = display.strip()
    if not display:
        return
    store["display_name"] = display
    playwright_fill_text(page, "account[name]", display, fast=True)
    if page.locator('input[name="account[display_name]"]:visible').count():
        playwright_fill_text(page, "account[display_name]", display, fast=True)


def _apply_legal_name_on_page(page: Any, legal: str, store: dict[str, str]) -> None:
    """Update account[legal_name] after duplicate company name rejection."""
    legal = legal.strip()
    if not legal:
        return
    store["legal_name"] = legal
    playwright_fill_text(page, "account[legal_name]", legal, fast=True)


def _account_creation_form(page: Any) -> Any:
    """The new-account form (has account[email]), not the login form."""
    loc = page.locator('form:has(input[name="account[email]"])')
    if loc.count():
        return loc.first
    return page.locator("form").first


def _find_create_account_submit(page: Any) -> Any:
    """Locate the Create account submit control inside the account creation form."""
    form = _account_creation_form(page)
    scoped = [
        form.get_by_role("button", name=re.compile(r"create\s+account", re.I)),
        form.locator('input[type="submit"]'),
        form.locator('button[type="submit"]'),
        form.locator('button:has-text("Create account")'),
        form.locator('button:has-text("Create Account")'),
    ]
    for loc in scoped:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    # Page-wide fallbacks
    for loc in [
        page.get_by_role("button", name=re.compile(r"create\s+account", re.I)),
        page.locator('input[type="submit"][value*="Create"]'),
        page.locator('button:has-text("Create account")'),
    ]:
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def _click_create_account_button(page: Any) -> None:
    """Click Create account using several strategies (Playwright sync API — same thread only)."""
    submit = _find_create_account_submit(page)
    form = _account_creation_form(page)

    if submit is not None:
        submit.scroll_into_view_if_needed()
        try:
            submit.click(force=True, timeout=15_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
            return
        except Exception:
            pass

    # JavaScript: requestSubmit / submit on the account form
    clicked = page.evaluate(
        """() => {
          const email = document.querySelector('input[name="account[email]"]');
          const form = email ? email.closest('form') : null;
          if (!form) return false;
          const btn = form.querySelector('button[type="submit"], input[type="submit"]');
          if (btn) {
            btn.click();
            return true;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return true;
          }
          form.submit();
          return true;
        }"""
    )
    if clicked:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=90_000)
        except Exception:
            pass
        return

    raise HammerOfficeError(
        "Create account button not found on Hammer Office form — cannot submit"
    )


def playwright_submit_new_account(
    page: Any,
    store: dict[str, str],
    *,
    max_attempts: int = 8,
) -> str:
    """
    Click Create on /accounts/new; if still on the form because company or display name
    is taken, append three random digits to legal_name or display name and retry.
    """
    last_url = page.url
    for attempt in range(max_attempts):
        _click_create_account_button(page)
        _wait_for_account_form_after_submit(page)
        last_url = page.url
        if account_create_succeeded(last_url):
            return last_url
        feedback = _page_form_feedback_text(page)
        if _retry_duplicate_name_on_form(page, store, feedback):
            print(f"[hammer-office] duplicate name — resubmit attempt {attempt + 2}/{max_attempts}", flush=True)
            continue
        if "/accounts/new" in last_url and _name_taken_phrases_present(feedback):
            if _apply_legal_uniquify_retry(page, store):
                continue
        break
    if "/accounts/new" in last_url:
        snippet = _page_form_feedback_text(page)[:400].replace("\n", " ")
        raise HammerOfficeError(
            "Account form may not have submitted — still on new account page "
            f"(company or display name may still be taken after retries). Page: {snippet}"
        )
    return last_url


def playwright_fill_text(page: Any, field_name: str, value: str, *, fast: bool = False) -> None:
    if not value:
        return
    if field_name == "account[address]":
        playwright_fill_address(page, value, fast=fast)
        return
    try:
        loc = page.locator(
            f'input[name="{field_name}"]:visible, textarea[name="{field_name}"]:visible'
        )
        if not loc.count():
            return
        target = loc.first
        target.scroll_into_view_if_needed()
        if fast or playwright_headless():
            target.fill(value)
            return
        target.evaluate(
            "el => { el.style.outline = '3px solid #c91e1e'; el.style.outlineOffset = '2px'; }"
        )
        target.click()
        target.fill("")
        target.press_sequentially(value, delay=45)
        page.wait_for_timeout(350)
        target.evaluate("el => { el.style.outline = ''; el.style.outlineOffset = ''; }")
    except HammerOfficeError:
        raise
    except Exception as exc:
        raise HammerOfficeError(f"Field fill failed for {field_name!r}: {exc}") from exc


def playwright_fill_address(page: Any, value: str, *, fast: bool = False) -> None:
    """Fill account[address] — uses fill() (not slow typing) and verifies the value stuck."""
    addr = value.strip()
    if not addr:
        return
    try:
        loc = page.locator(
            '#account_address:visible, input[name="account[address]"]:visible'
        )
        if not loc.count():
            raise HammerOfficeError("Address field account[address] not found on Hammer Office form")
        target = loc.first
        target.scroll_into_view_if_needed()
        if not playwright_headless():
            target.evaluate(
                "el => { el.style.outline = '3px solid #c91e1e'; el.style.outlineOffset = '2px'; }"
            )
        target.click()
        target.fill(addr)
        target.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        settle_ms = 50 if fast else 300
        page.wait_for_timeout(settle_ms)
        if not playwright_headless():
            target.evaluate("el => { el.style.outline = ''; el.style.outlineOffset = ''; }")
        current = target.input_value().strip()
        # Autocomplete widgets sometimes trim; require a substantial match
        probe = addr[: min(12, len(addr))].lower()
        if probe and probe not in current.lower():
            target.fill("")
            target.fill(addr)
            page.wait_for_timeout(50 if fast else 200)
            current = target.input_value().strip()
        if probe and probe not in current.lower():
            raise HammerOfficeError(
                f"Address did not stick in Hammer Office form (expected snippet {probe!r}, got {current!r})"
            )
    except HammerOfficeError:
        raise
    except Exception as exc:
        raise HammerOfficeError(f"Address fill failed: {exc}") from exc


def read_address_field(page: Any) -> str:
    loc = page.locator('#account_address, input[name="account[address]"]')
    if not loc.count():
        return ""
    return loc.first.input_value().strip()


def address_is_hammer_placeholder(address: str) -> bool:
    """True if address is empty or matches Hammer Office's example placeholder text."""
    a = " ".join(address.strip().lower().split())
    if not a:
        return True
    if a == "123 main street, seattle, wa 98134":
        return True
    return "123 main street" in a and "98134" in a


def playwright_clear_address(page: Any) -> None:
    """Clear address so nothing is pre-filled before the dealer provides their real address."""
    loc = page.locator('#account_address:visible, input[name="account[address]"]:visible')
    if not loc.count():
        return
    target = loc.first
    target.scroll_into_view_if_needed()
    target.click()
    target.fill("")
    target.evaluate(
        """el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )


def _playwright_select_first_option(page: Any, field_name: str) -> None:
    """Select the first non-empty option in a <select> — fallback when no label/value matches."""
    try:
        loc = page.locator(f'select[name="{field_name}"]:visible')
        if not loc.count():
            return
        target = loc.first
        for opt in target.locator("option").all():
            opt_val = (opt.get_attribute("value") or "").strip()
            if opt_val:
                target.select_option(value=opt_val)
                return
    except Exception:
        pass


def playwright_select_option(page: Any, field_name: str, spoken: str, *, resolved: str | None = None) -> None:
    try:
        loc = page.locator(f'select[name="{field_name}"]:visible')
        if not loc.count():
            raise HammerOfficeError(f"Dropdown not found: {field_name}")
        target = loc.first
        target.scroll_into_view_if_needed()
        value = resolved if resolved is not None else spoken
        if field_name == "account[business_type]":
            value = resolve_business_type_value(spoken)
        try:
            target.select_option(value=value)
            return
        except Exception:
            pass
        try:
            target.select_option(label=value)
            return
        except Exception:
            pass
        for opt in target.locator("option").all():
            opt_val = (opt.get_attribute("value") or "").strip()
            opt_label = opt.inner_text().strip()
            if not opt_val:
                continue
            combined = f"{opt_val} {opt_label}".lower()
            if value.lower() in combined or spoken.lower() in combined:
                target.select_option(value=opt_val)
                return
        raise HammerOfficeError(
            f"Could not select {field_name!r} — no matching option for {spoken!r} (tried {value!r})"
        )
    except HammerOfficeError:
        raise
    except Exception as exc:
        raise HammerOfficeError(f"Dropdown select failed for {field_name!r}: {exc}") from exc


def _create_with_playwright(req: HammerAccountRequest) -> HammerAccountResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HammerOfficeError(
            "Playwright is not installed — pip install playwright && playwright install chromium"
        ) from exc

    base = office_base_url()
    owner = req.name.strip()
    phone_display = req.phone.strip()
    website = req.website.strip()
    if website and not website.startswith(("http://", "https://")):
        website = f"https://{website}"
    dealership = req.dealership_name.strip() or owner

    headless = playwright_headless()
    slow_mo = playwright_slow_mo()
    keep_open_ms = playwright_keep_open_ms()
    if not headless and keep_open_ms == 0:
        keep_open_ms = 120_000

    launch_opts: dict[str, Any] = {"headless": headless}
    if slow_mo > 0:
        launch_opts["slow_mo"] = slow_mo

    url = ""
    store: dict[str, str] = {}

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(**launch_opts)
            except Exception as exc:
                raise HammerOfficeError(
                    f"Chromium failed to launch — server needs ~1.5 GB RAM free: {exc}"
                ) from exc

            page = browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                page.goto(f"{base}/accounts/new", wait_until="networkidle", timeout=60_000)
            except Exception as exc:
                raise HammerOfficeError(f"Could not reach Hammer Office: {exc}") from exc

            if page.locator("#user_email").count():
                page.fill("#user_email", office_login_email())
                page.fill("#user_password", office_login_password())
                try:
                    _click_login_button(page)
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except HammerOfficeError:
                    raise
                except Exception as exc:
                    raise HammerOfficeError(f"Login step failed: {exc}") from exc

            if page.locator("#user_email").count():
                raise HammerOfficeError(
                    "Hammer Office login failed — check HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD"
                )

            display = (req.display_name or dealership).strip()
            legal = (req.legal_name or dealership).strip()
            playwright_fill_text(page, "account[email]", req.email.strip().lower())
            playwright_fill_text(page, "account[owner_name]", owner)
            playwright_fill_text(page, "account[name]", display)
            playwright_fill_text(page, "account[legal_name]", legal)
            playwright_fill_text(page, "account[phone_str]", phone_display)
            playwright_fill_text(page, "account[mobile_str]", req.cell_phone.strip() or phone_display)
            playwright_fill_text(page, "account[website_url]", website)
            if req.address.strip():
                playwright_fill_address(page, req.address.strip())
                tz = req.timezone.strip() or infer_hammer_timezone(req.address)
                if tz:
                    try:
                        playwright_select_option(page, "account[timezone]", tz, resolved=tz)
                    except HammerOfficeError:
                        pass
            if req.business_type.strip():
                business_type = resolve_business_type_value(req.business_type.strip())
                try:
                    playwright_select_option(page, "account[business_type]", business_type, resolved=business_type)
                except HammerOfficeError:
                    _playwright_select_first_option(page, "account[business_type]")
            if req.currency.strip():
                playwright_select_option(page, "account[currency]", req.currency.strip())
            if req.gst_hst.strip():
                playwright_fill_text(page, "account[gst_hst_number]", req.gst_hst.strip())
            if req.qst.strip():
                playwright_fill_text(page, "account[qst_number]", req.qst.strip())

            # Role is a required select dropdown — must be filled INSIDE the playwright context.
            role_val = req.role.strip() or "Owner"
            try:
                playwright_select_option(page, "account[role]", role_val)
            except HammerOfficeError:
                # Exact match failed — pick the first non-empty option rather than
                # falling back to a text fill (which silently does nothing on a <select>).
                _playwright_select_first_option(page, "account[role]")

            if office_dry_run():
                if keep_open_ms > 0:
                    page.wait_for_timeout(keep_open_ms)
                browser.close()
                return HammerAccountResult(ok=True, message="dry run — Playwright form filled", dry_run=True)

            store = {
                "display_name": display,
                "legal_name": legal,
                "dealership_name": dealership,
            }
            fill_all_company_identity_fields(page, legal, store)
            url = playwright_submit_new_account(page, store)
            # keep_open is a courtesy for local review — swallow errors if browser was closed
            if keep_open_ms > 0:
                try:
                    page.wait_for_timeout(keep_open_ms)
                except Exception:
                    pass
            if playwright_keep_browser_after_submit():
                pass  # leave browser open for local review
            else:
                try:
                    browser.close()
                except Exception:
                    pass

    except HammerOfficeError:
        raise
    except Exception as exc:
        raise HammerOfficeError(f"Playwright account creation failed: {exc}") from exc

    if not account_create_succeeded(url):
        raise HammerOfficeError(
            f"Playwright may not have submitted the account form (url={url})"
        )
    final_legal = store.get("legal_name", legal)
    return HammerAccountResult(
        ok=True,
        message=f"Hammer Office account created (Playwright) — {final_legal}",
        account_url=account_url_from_submit(url),
    )
