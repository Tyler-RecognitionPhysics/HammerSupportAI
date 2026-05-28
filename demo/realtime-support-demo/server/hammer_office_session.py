"""Persistent Playwright session for incremental Hammer Office account form filling."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from address_timezone import address_billing_context, infer_billing_currency_from_address, infer_hammer_timezone
from agreement_approvals import agreement_approval_status
from hammer_office import (
    HammerAccountRequest,
    HammerAccountResult,
    HammerOfficeError,
    account_create_succeeded,
    account_url_from_submit,
    fill_all_company_identity_fields,
    create_hammer_account,
    hammer_office_configured,
    hammer_office_serverless,
    office_base_url,
    office_dry_run,
    office_login_email,
    office_login_password,
    playwright_clear_address,
    playwright_fill_address,
    playwright_fill_text,
    playwright_headless,
    playwright_submit_new_account,
    keep_browser_open_on_submit_failure,
    playwright_keep_browser_after_submit,
    playwright_keep_open_ms,
    playwright_select_option,
    playwright_slow_mo,
    read_address_field,
    require_agreement_approval,
    resolve_business_type_value,
    use_playwright,
    _click_login_button,
)

# Aliases the voice agent may use → canonical field key
FIELD_ALIASES: dict[str, str] = {
    "full_address": "address",
    "business_address": "address",
    "street_address": "address",
    "mailing_address": "address",
    "full_business_address": "address",
    "business_phone": "phone",
    "primary_phone": "phone",
    "cell": "cell_phone",
    "mobile_phone": "cell_phone",
    "url": "website",
    "site": "website",
    "entity_type": "business_type",
    "company_type": "business_type",
}


def normalize_field_key(field: str) -> str:
    key = field.strip().lower().replace(" ", "_").replace("-", "_")
    return FIELD_ALIASES.get(key, key)


# Voice tool field name → Hammer Office input name
FIELD_TO_FORM: dict[str, str] = {
    "email": "account[email]",
    "name": "account[owner_name]",
    "owner_name": "account[owner_name]",
    "first_name": "account[owner_name]",
    "last_name": "account[owner_name]",
    "legal_name": "account[legal_name]",
    "display_name": "account[name]",
    "dealership_name": "account[name]",
    "phone": "account[phone_str]",
    "cell_phone": "account[mobile_str]",
    "mobile": "account[mobile_str]",
    "website": "account[website_url]",
    "address": "account[address]",
    "business_type": "account[business_type]",
    "currency": "account[currency]",
    "timezone": "account[timezone]",
    "gst_hst": "account[gst_hst_number]",
    "qst": "account[qst_number]",
    "role": "account[role]",
    "title": "account[role]",
    "position": "account[role]",
}

# Collected incrementally via fill_hammer_account_field; auto-submit when all are set.
# Note: "role" is intentionally excluded — it is always inferred by _ensure_implicit_role
# and filled silently; it is never asked aloud or treated as a blocking missing field.
_SUBMIT_REQUIRED_KEYS: tuple[str, ...] = (
    "email",
    "dealership_name",
    "name",
    "business_type",
    "phone",
    "website",
    "address",
    # currency intentionally excluded — the Hammer Office form defaults to USD and we
    # fill it explicitly when inferable from the address.  Requiring it here blocked
    # auto-submit whenever voice transcription produced an address without a
    # recognisable state abbreviation (e.g. "123 Main St, Houston, Texas" with no zip).
)


@dataclass
class _LiveSession:
    email: str
    browser: Any | None = None
    page: Any | None = None
    playwright: Any | None = None
    values: dict[str, str] = field(default_factory=dict)
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    submitted: bool = False
    account_url: str | None = None
    applied_fields: set[str] = field(default_factory=set)
    # Only fields the visitor answered on this call (fill_hammer_account_field) may submit.
    confirmed_fields: set[str] = field(default_factory=set)
    ready: threading.Event = field(default_factory=threading.Event)
    opening: bool = False
    pending_fills: list[tuple[str, str]] = field(default_factory=list)
    open_error: str | None = None
    submit_in_progress: bool = False
    submit_error: str | None = None


_manager_lock = threading.Lock()
_sessions: dict[str, _LiveSession] = {}
_submitted_accounts: dict[str, str | None] = {}
# Playwright sync API: browser/page must be used from ONE worker thread only.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hammer-office")
_log = logging.getLogger(__name__)

# All session keys written to the live form immediately before Create account is clicked.
_SYNC_TO_PAGE_KEYS: tuple[str, ...] = (
    "email",
    "name",
    "legal_name",
    "display_name",
    "dealership_name",
    "phone",
    "website",
    "address",
    "business_type",
    "currency",
    "timezone",
    "role",
    "gst_hst",
    "qst",
)


def _instant_mode() -> bool:
    """When True, voice/debug HTTP returns immediately; Playwright runs on the worker thread."""
    return os.environ.get("HAMMER_OFFICE_INSTANT", "1").strip().lower() not in ("0", "false", "no")


def _submit_wait_seconds() -> float:
    """How long fill_hammer_account_field waits for Create-account click (visible browser / local debug)."""
    raw = os.environ.get("HAMMER_OFFICE_SUBMIT_WAIT_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    if not playwright_headless():
        return 45.0
    return 0.0


def _account_created_fill_response(email: str) -> dict[str, str | bool] | None:
    """If submit finished (even after HTTP timeout), return the voice tool success payload."""
    done, url = account_already_created(_norm_email(email))
    if not done:
        return None
    return {
        "ok": True,
        "account_created": True,
        "account_url": url,
        "message": (
            "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived; "
            "do not mention activate, password, or card yet; do not call create_hammer_account"
        ),
    }


def _poll_account_created_after_submit(email: str, *, max_seconds: float = 18.0) -> dict[str, str | bool] | None:
    """Brief grace poll when Playwright submit outlasts the HTTP wait (visible browser)."""
    import time

    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        snap = _account_created_fill_response(email)
        if snap:
            return snap
        time.sleep(0.4)
    return None


def _session_debug_snapshot(sess: _LiveSession | None, email: str) -> dict[str, Any]:
    """Local troubleshooting — browser state, missing fields, last submit error."""
    from agreement_approvals import agreement_approval_status

    approved = bool(agreement_approval_status(_norm_email(email), wait_seconds=0).get("approved"))
    if not sess:
        return {
            "email": _norm_email(email),
            "session_open": False,
            "agreement_approved": approved,
        }
    missing = _missing_for_submit_session(sess)
    return {
        "email": sess.email,
        "session_open": True,
        "browser_ready": sess.ready.is_set() and bool(sess.page) and not sess.open_error,
        "opening": sess.opening,
        "open_error": sess.open_error,
        "submitted": sess.submitted,
        "account_url": sess.account_url,
        "submit_in_progress": sess.submit_in_progress,
        "submit_error": sess.submit_error,
        "agreement_approved": approved,
        "missing_for_submit": missing,
        "confirmed_fields": sorted(sess.confirmed_fields),
        "applied_fields": sorted(sess.applied_fields),
        "collected_keys": sorted(k for k, v in sess.values.items() if str(v).strip()),
    }


_SERVERLESS_SIGNUP_DIR = Path("/tmp/realtime-sales-demo/hammer_signup")


def clear_signup_submission_state(email: str) -> None:
    """Drop stale submitted flags so a new signup attempt is not treated as complete."""
    key = _norm_email(email)
    if not key:
        return
    with _manager_lock:
        _submitted_accounts.pop(key, None)
        sess = _sessions.get(key)
        if sess:
            sess.submitted = False
            sess.account_url = None
            sess.submit_error = None
            sess.submit_in_progress = False
    if hammer_office_serverless():
        rec = _serverless_load_record(key)
        if rec.get("submitted") or rec.get("account_url"):
            confirmed = rec.get("confirmed_fields") or []
            confirmed_set = {normalize_field_key(str(k)) for k in confirmed if str(k).strip()}
            _serverless_save_record(
                key,
                values=dict(rec.get("values") or {}),
                submitted=False,
                account_url=None,
                confirmed_fields=confirmed_set,
            )


def reset_voice_signup_session(
    email: str,
    *,
    dealership_name: str = "",
    display_name: str = "",
    name: str = "",
) -> None:
    """Reset Phase B + submission state for a fresh browser signup (same email retest)."""
    key = _norm_email(email)
    if not key:
        return
    clear_signup_submission_state(key)
    if hammer_office_serverless():
        sess = _serverless_session(key)
        _reset_phase_b_for_new_signup(sess)
        _seed_open_values(sess, email, dealership_name, display_name, name)
        if not (sess.values.get("dealership_name") or "").strip() and dealership_name.strip():
            _optimistic_store(sess, "dealership_name", dealership_name)
        _serverless_save_record(
            sess.email,
            values=sess.values,
            submitted=False,
            account_url=None,
            confirmed_fields=sess.confirmed_fields,
        )
    else:
        with _manager_lock:
            sess = _sessions.get(key)
        if sess:
            _reset_phase_b_for_new_signup(sess)
            _seed_open_values(sess, email, dealership_name, display_name, name)


def _serverless_signup_path(email: str) -> Path:
    safe = re.sub(r"[^\w@.-]+", "_", _norm_email(email))
    return _SERVERLESS_SIGNUP_DIR / f"{safe}.json"


def _serverless_load_record(email: str) -> dict[str, Any]:
    path = _serverless_signup_path(email)
    if not path.is_file():
        return {"values": {}, "submitted": False, "account_url": None}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    values = data.get("values")
    if not isinstance(values, dict):
        values = {}
    return {
        "values": {str(k): str(v) for k, v in values.items() if v is not None},
        "submitted": bool(data.get("submitted")),
        "account_url": data.get("account_url"),
        "confirmed_fields": [
            str(k) for k in (data.get("confirmed_fields") or []) if str(k).strip()
        ],
    }


def _serverless_save_record(
    email: str,
    *,
    values: dict[str, str],
    submitted: bool = False,
    account_url: str | None = None,
    confirmed_fields: set[str] | None = None,
) -> None:
    path = _serverless_signup_path(email)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "values": values,
            "submitted": submitted,
            "account_url": account_url,
        }
        if confirmed_fields is not None:
            payload["confirmed_fields"] = sorted(confirmed_fields)
        path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        _log.warning("serverless signup store write failed for %s: %s", email, exc)


def _serverless_session(email: str) -> _LiveSession:
    rec = _serverless_load_record(email)
    sess = _LiveSession(email=_norm_email(email), values=dict(rec["values"]))
    stored_confirmed = rec.get("confirmed_fields") or []
    if isinstance(stored_confirmed, list) and stored_confirmed:
        for key in stored_confirmed:
            sess.confirmed_fields.add(normalize_field_key(str(key)))
    if rec.get("submitted"):
        sess.submitted = True
        sess.account_url = rec.get("account_url")
    return sess


def _serverless_submit_if_ready(sess: _LiveSession) -> dict[str, str | bool] | None:
    """On Vercel, submit synchronously via httpx when all fields are collected."""
    if sess.submitted:
        return {
            "ok": True,
            "account_created": True,
            "account_url": sess.account_url,
            "message": "account already created — PHASE C only",
        }
    missing = _missing_for_submit_session(sess)
    # Currency is inferred from address; if parsing failed (ambiguous address), default to USD
    # so the account can still submit rather than blocking indefinitely.
    if missing == ["currency"] and (sess.values.get("address") or "").strip():
        sess.values["currency"] = "USD"
        missing = []
    if missing:
        return None
    req = _request_from_session(sess.values)
    try:
        result = create_hammer_account(req)
    except HammerOfficeError as exc:
        return {
            "ok": False,
            "account_created": False,
            "message": (
                f"submit failed — {str(exc)[:200]}; apologize once for the technical hiccup and "
                "retry by re-calling fill_hammer_account_field on the last field or create_hammer_account. "
                "Do NOT say a live rep will complete setup — you finish on this call."
            ),
        }
    if result.dry_run:
        msg = (
            "ok (dry run) — account created; PHASE C.1 only: did Welcome to Hammer email arrive? "
            "(one step — no activate/password/card yet)"
        )
    else:
        msg = (
            "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived; "
            "do not mention activate, password, or card yet; do not call create_hammer_account"
        )
    sess.submitted = True
    sess.account_url = result.account_url
    key = sess.email
    with _manager_lock:
        _submitted_accounts[key] = result.account_url
    _serverless_save_record(
        key,
        values=sess.values,
        submitted=True,
        account_url=result.account_url,
        confirmed_fields=sess.confirmed_fields,
    )
    return {
        "ok": True,
        "account_created": True,
        "account_url": result.account_url,
        "dry_run": result.dry_run,
        "message": msg,
    }


def _fill_hammer_account_field_serverless(email: str, field: str, value: str) -> dict[str, str | bool]:
    from hammer_office import address_is_hammer_placeholder

    key = normalize_field_key(field)
    if key == "address" and address_is_hammer_placeholder(value):
        raise HammerOfficeError(
            "address looks like the form placeholder — ask the dealer for their real street address"
        )
    sess = _serverless_session(email)
    if sess.submitted:
        stale_missing = _missing_for_submit_session(sess)
        if stale_missing:
            clear_signup_submission_state(email)
            sess = _serverless_session(email)
        else:
            return {
                "ok": True,
                "field": field,
                "account_created": True,
                "account_url": sess.account_url,
                "message": "account already created — PHASE C only",
            }

    # Re-seed critical required fields that may be absent after a /tmp cold start.
    # email is always available from the function signature.
    if not (sess.values.get("email") or "").strip():
        _optimistic_store(sess, "email", email)
    # dealership_name is written to the approval store at capture_lead time — recover it from there.
    if not (sess.values.get("dealership_name") or "").strip():
        from agreement_approvals import agreement_approval_status as _approval_status
        approval = _approval_status(_norm_email(email), wait_seconds=0)
        dealer = str(approval.get("dealership", "") or "").strip()
        if dealer:
            _optimistic_store(sess, "dealership_name", dealer)

    _optimistic_store(sess, key, value)
    _mark_field_confirmed(sess, field)
    _serverless_save_record(sess.email, values=sess.values, confirmed_fields=sess.confirmed_fields)
    out = _instant_fill_response(sess, field, key, value)
    if out.get("ready_to_submit"):
        submitted = _serverless_submit_if_ready(sess)
        if submitted and submitted.get("account_created"):
            submitted["field"] = field
            return submitted
        if submitted and not submitted.get("account_created"):
            submitted["field"] = field
            return submitted
        out.pop("ready_to_submit", None)
        still_missing = _missing_for_submit_session(sess)
        if still_missing:
            out["message"] = f"ok — still need: {', '.join(still_missing)}"
        else:
            out["message"] = (
                "ok — account setup still in progress; do NOT ask Welcome to Hammer yet — "
                "collect any remaining account fields or retry submit"
            )
    return out


def _open_hammer_account_form_serverless(
    email: str,
    *,
    dealership_name: str = "",
    display_name: str = "",
    name: str = "",
) -> dict[str, str | bool]:
    require_agreement_approval(email)
    sess = _serverless_session(email)
    prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
    _serverless_save_record(sess.email, values=sess.values, confirmed_fields=sess.confirmed_fields)
    return {
        "ok": True,
        "browser_open": False,
        "prefilled": prefilled,
        "message": "form ready — ask next field now",
    }


def _norm_email(email: str) -> str:
    return email.strip().lower()


def get_session_values_for_summary(email: str) -> dict[str, str]:
    """Latest collected account fields for end-of-call Slack summary (read-only)."""
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
        if not sess:
            return {}
        return dict(sess.values)


def _normalize_website(value: str) -> str:
    website = value.strip()
    if website and not website.startswith(("http://", "https://")):
        return f"https://{website}"
    return website


def _launch_browser(playwright: Any) -> tuple[Any, Any]:
    headless = playwright_headless()
    launch_opts: dict[str, Any] = {"headless": headless}
    slow_mo = playwright_slow_mo()
    if slow_mo > 0:
        launch_opts["slow_mo"] = slow_mo
    try:
        browser = playwright.chromium.launch(**launch_opts)
    except Exception as exc:
        raise HammerOfficeError(
            f"Chromium failed to launch — server needs ~1.5 GB RAM free: {exc}"
        ) from exc
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    return browser, page


def _login_page(page: Any, base: str) -> None:
    try:
        page.goto(f"{base}/accounts/new", wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        raise HammerOfficeError(f"Could not reach Hammer Office: {exc}") from exc
    if not page.locator("#user_email").count():
        return
    page.fill("#user_email", office_login_email())
    page.fill("#user_password", office_login_password())
    try:
        _click_login_button(page)
        page.wait_for_load_state("domcontentloaded", timeout=45_000)
    except HammerOfficeError:
        raise
    except Exception as exc:
        raise HammerOfficeError(f"Login step failed: {exc}") from exc
    if page.locator("#user_email").count():
        raise HammerOfficeError(
            "Hammer Office login failed — check HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD"
        )


def _infer_role_from_values(values: dict[str, str]) -> str:
    """Hammer Office needs account[role]; never ask on the voice call — infer or default."""
    existing = (values.get("role") or "").strip()
    if existing:
        return existing
    blob = " ".join(str(values.get(k) or "") for k in ("name", "dealership_name")).lower()
    if re.search(r"\b(general\s+manager|gm)\b", blob):
        return "General Manager"
    if re.search(r"\bsales\s+manager\b", blob):
        return "Sales Manager"
    if re.search(r"\b(owner|dealer\s+principal|principal)\b", blob):
        return "Owner"
    return "Owner"


def _ensure_implicit_role(values: dict[str, str]) -> None:
    if not (values.get("role") or "").strip():
        values["role"] = _infer_role_from_values(values)


def _ensure_implicit_currency(values: dict[str, str]) -> None:
    """Default currency to USD so submit never blocks on an un-inferred address."""
    if not (values.get("currency") or "").strip():
        values["currency"] = "USD"


_PHASE_B_VOICE_KEYS: tuple[str, ...] = (
    "name",
    "business_type",
    "phone",
    "cell_phone",
    "website",
    "address",
    "gst_hst",
    "qst",
    "timezone",
    "currency",
    "billing_country",
    "role",
)


def _mark_field_confirmed(sess: _LiveSession, field: str) -> None:
    key = normalize_field_key(field)
    sess.confirmed_fields.add(key)
    if key == "phone":
        sess.confirmed_fields.add("cell_phone")
    if key in ("first_name", "last_name") and (sess.values.get("name") or "").strip():
        sess.confirmed_fields.add("name")
    if key == "name":
        sess.confirmed_fields.add("name")


def _reset_phase_b_for_new_signup(sess: _LiveSession) -> None:
    """New agreement email — drop leftover debug/prior-test values for this email."""
    for key in _PHASE_B_VOICE_KEYS:
        sess.values.pop(key, None)
        sess.applied_fields.discard(key)
    sess.confirmed_fields.clear()
    sess.submit_error = None
    sess.submit_in_progress = False
    sess.submitted = False
    sess.account_url = None


def _missing_for_submit(values: dict[str, str], confirmed: set[str] | None = None) -> list[str]:
    _ensure_implicit_role(values)
    _ensure_implicit_currency(values)
    check_confirmed = confirmed is not None
    confirmed_set = confirmed or set()
    missing: list[str] = []
    for key in _SUBMIT_REQUIRED_KEYS:
        if not (values.get(key) or "").strip():
            missing.append(key)
            continue
        if key in ("email", "dealership_name"):
            continue
        if check_confirmed and key not in confirmed_set:
            missing.append(key)
    return missing


def _missing_for_submit_session(sess: _LiveSession) -> list[str]:
    return _missing_for_submit(sess.values, sess.confirmed_fields)


def _merge_request_into_session(sess: _LiveSession, req: HammerAccountRequest) -> None:
    """Ensure backup create_hammer_account payload is in session before submit."""
    pairs: list[tuple[str, str]] = [
        ("email", req.email),
        ("name", req.name),
        ("legal_name", req.legal_name or req.dealership_name),
        ("display_name", req.display_name or req.dealership_name),
        ("dealership_name", req.dealership_name),
        ("phone", req.phone),
        ("cell_phone", req.cell_phone or req.phone),
        ("website", req.website),
        ("address", req.address),
        ("business_type", req.business_type),
        ("currency", req.currency),
        ("role", req.role),
        ("gst_hst", req.gst_hst),
        ("qst", req.qst),
    ]
    for key, val in pairs:
        if val and str(val).strip():
            _optimistic_store(sess, key, str(val).strip())


def _request_from_session(values: dict[str, str]) -> HammerAccountRequest:
    _ensure_implicit_role(values)
    _ensure_implicit_currency(values)
    dealership = (values.get("dealership_name") or values.get("legal_name") or "").strip()
    phone = (values.get("phone") or values.get("cell_phone") or "").strip()
    return HammerAccountRequest(
        email=values.get("email", "").strip().lower(),
        name=values.get("name", "").strip(),
        legal_name=(values.get("legal_name") or dealership).strip(),
        display_name=(values.get("display_name") or dealership).strip(),
        phone=phone,
        cell_phone=(values.get("cell_phone") or phone).strip(),
        website=values.get("website", "").strip(),
        address=values.get("address", "").strip(),
        business_type=values.get("business_type", "").strip(),
        timezone=values.get("timezone", "").strip(),
        currency=values.get("currency", "").strip(),
        gst_hst=values.get("gst_hst", "").strip(),
        qst=values.get("qst", "").strip(),
        dealership_name=dealership,
        role=values.get("role", "").strip(),
        selected_plan=values.get("selected_plan", "").strip(),
    )


def _prefilled_from_values(values: dict[str, str]) -> list[str]:
    labels: list[str] = []
    if values.get("email"):
        labels.append("email")
    if values.get("dealership_name") or values.get("legal_name"):
        labels.extend(["display_name", "legal_name"])
    if values.get("name"):
        labels.append("name")
    return labels


def _seed_open_values(
    sess: _LiveSession,
    email: str,
    dealership_name: str,
    display_name: str,
    name: str,
) -> list[str]:
    """Store PHASE A fields in session immediately (instant mode — before browser is ready)."""
    if email.strip():
        _optimistic_store(sess, "email", email)
    dn = (dealership_name or display_name).strip()
    if dn:
        _optimistic_store(sess, "display_name", dn)
    if name.strip():
        _optimistic_store(sess, "name", name)
        _mark_field_confirmed(sess, "name")
    return _prefilled_from_values(sess.values)


def _optimistic_store(sess: _LiveSession, key: str, value: str) -> None:
    """Update session values without Playwright (instant voice tool response)."""
    val = value.strip()
    if not val:
        return
    if key == "website":
        val = _normalize_website(val)
    if key == "email":
        val = val.lower()
    if key == "business_type":
        val = resolve_business_type_value(val)
    store = sess.values
    if key in ("phone", "cell_phone"):
        store["phone"] = val
        store["cell_phone"] = val
        return
    if key == "first_name":
        store["first_name"] = val
        last = (store.get("last_name") or "").strip()
        store["name"] = f"{val} {last}".strip()
        return
    if key == "last_name":
        store["last_name"] = val
        first = (store.get("first_name") or "").strip()
        store["name"] = f"{first} {val}".strip()
        return
    store[key] = val
    if key in ("display_name", "legal_name", "dealership_name"):
        store["dealership_name"] = val
        store["legal_name"] = val
        store["display_name"] = val
    if key == "address":
        tz = infer_hammer_timezone(val)
        if tz:
            store["timezone"] = tz
        currency = infer_billing_currency_from_address(val)
        if currency:
            store["currency"] = currency
        ctx = address_billing_context(val)
        if ctx.get("country"):
            store["billing_country"] = str(ctx["country"])


def _instant_fill_response(sess: _LiveSession, field: str, key: str, value: str) -> dict[str, str | bool]:
    out: dict[str, str | bool] = {"ok": True, "field": field, "message": "ok"}
    if key == "address":
        ctx = address_billing_context(value)
        if sess.values.get("timezone"):
            out["timezone_set"] = str(sess.values["timezone"])
        if sess.values.get("currency"):
            out["currency_set"] = str(sess.values["currency"])
        country = ctx.get("country")
        if country:
            out["billing_country"] = str(country)
            out["region_code"] = str(ctx.get("region_code") or "")
            out["is_quebec"] = bool(ctx.get("is_quebec"))
            out["tax_field"] = str(ctx.get("tax_field") or "none")
            tax_prompt = str(ctx.get("tax_prompt") or "")
            parts = [f"ok — {country}", f"currency {sess.values.get('currency', '')}".strip()]
            if out.get("timezone_set"):
                parts.append(str(out["timezone_set"]))
            if tax_prompt:
                parts.append(tax_prompt)
            out["message"] = " — ".join(p for p in parts if p)
        else:
            out["message"] = "ok — confirm US or Canada"
    missing = _missing_for_submit_session(sess)
    if missing:
        if key != "address":
            out["message"] = f"ok — still need: {', '.join(missing)}"
    else:
        _ensure_implicit_role(sess.values)
        _ensure_implicit_currency(sess.values)
        out["ready_to_submit"] = True
        out["message"] = (
            "ok — all account fields collected; role set silently — account submitting now"
        )
    return out


def _open_worker(
    key: str,
    email: str,
    dealership_name: str,
    display_name: str,
    name: str,
    *,
    require_approval: bool,
) -> None:
    try:
        if require_approval:
            require_agreement_approval(email)
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser, page = _launch_browser(pw)
        base = office_base_url()
        _login_page(page, base)
        playwright_clear_address(page)
        with _manager_lock:
            sess = _sessions.get(key)
            if not sess:
                browser.close()
                pw.stop()
                return
            sess.browser = browser
            sess.page = page
            sess.playwright = pw
            pending = list(sess.pending_fills)
            sess.pending_fills.clear()
        if email.strip():
            _apply_field_to_page(page, "email", email, sess.values, applied=sess.applied_fields)
        dn = (dealership_name or display_name).strip()
        if dn:
            fill_all_company_identity_fields(page, dn, sess.values)
            sess.applied_fields.update({"display_name", "legal_name", "dealership_name"})
        if name.strip():
            _apply_field_to_page(page, "name", name, sess.values, applied=sess.applied_fields)
        for fld, val in pending:
            _apply_field_to_page(page, fld, val, sess.values, applied=sess.applied_fields)
            _mark_field_confirmed(sess, fld)
        with _manager_lock:
            sess.opening = False
        sess.ready.set()
        if not _missing_for_submit_session(sess) and not sess.submitted:
            try:
                _maybe_submit_complete_session(email, sess)
            except Exception:
                pass
    except Exception as exc:
        _log.error("Hammer Office browser open failed for %s: %s", email, exc)
        with _manager_lock:
            sess = _sessions.get(key)
            if sess:
                sess.open_error = str(exc)
                sess.opening = False
        if sess:
            sess.ready.set()


def _background_fill(email: str, field: str, value: str) -> None:
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
    if not sess or not sess.ready.wait(timeout=120):
        return
    if sess.open_error or not sess.page:
        return
    try:
        _apply_field_to_page(
            sess.page, field, value, sess.values, applied=sess.applied_fields
        )
        _mark_field_confirmed(sess, field)
        _maybe_submit_complete_session(email, sess)
    except Exception:
        _log.exception("background_fill failed for %s field %s", email, field)


def _maybe_submit_complete_session(email: str, sess: _LiveSession) -> None:
    """If all required fields are in session, sync form and click Create (Playwright thread)."""
    with _manager_lock:
        if sess.submitted or sess.submit_in_progress:
            return
        missing = _missing_for_submit_session(sess)
        if missing:
            return
        sess.submit_in_progress = True
    try:
        req = _request_from_session(sess.values)
        _sync_session_values_to_page(sess, req)
        result = submit_hammer_account_form(email, req)
        if not result.ok:
            raise HammerOfficeError(result.message or "submit failed")
    except Exception as exc:
        _log.exception("auto-submit failed for %s: %s", email, exc)
        with _manager_lock:
            sess.submit_in_progress = False
        raise
    finally:
        with _manager_lock:
            live = _sessions.get(_norm_email(email))
            if live:
                live.submit_in_progress = False


def _sync_session_values_to_page(sess: _LiveSession, req: HammerAccountRequest) -> None:
    """Push every collected value onto the live Hammer form before clicking Create."""
    if not sess.page:
        return
    synced_identity = False
    for key in _SYNC_TO_PAGE_KEYS:
        if key not in ("email", "legal_name", "display_name", "dealership_name", "role") and key not in sess.confirmed_fields:
            continue
        val = _session_value_for_key(req, key, sess.values)
        if not val:
            continue
        if key in ("legal_name", "display_name", "dealership_name"):
            if synced_identity:
                continue
            try:
                identity = (
                    sess.values.get("legal_name")
                    or sess.values.get("dealership_name")
                    or sess.values.get("display_name")
                    or val
                ).strip()
                fill_all_company_identity_fields(sess.page, identity, sess.values)
                synced_identity = True
                sess.applied_fields.update({"legal_name", "display_name", "dealership_name"})
            except HammerOfficeError as exc:
                _log.warning("sync company identity fields: %s", exc)
            continue
        try:
            _apply_field_to_page(
                sess.page,
                key,
                val,
                sess.values,
                applied=sess.applied_fields,
            )
        except HammerOfficeError as exc:
            _log.warning("sync field %s to page: %s", key, exc)


def _fill_then_submit_worker(email: str, field: str, value: str) -> dict[str, str | bool]:
    """
    Runs on the Playwright worker thread: apply last field, sync form, click Create account.
    """
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
    if not sess:
        raise HammerOfficeError("No Hammer Office session")
    if not sess.ready.wait(timeout=120) or sess.open_error or not sess.page:
        raise HammerOfficeError("Hammer Office browser not ready")
    _apply_field_to_page(
        sess.page, field, value, sess.values, applied=sess.applied_fields
    )
    _mark_field_confirmed(sess, field)
    missing = _missing_for_submit_session(sess)
    if missing:
        raise HammerOfficeError(f"Cannot submit — still missing: {', '.join(missing)}")
    req = _request_from_session(sess.values)
    _sync_session_values_to_page(sess, req)
    still = _missing_for_submit_session(sess)
    if still:
        raise HammerOfficeError(f"Cannot submit — still missing after sync: {', '.join(still)}")
    result = submit_hammer_account_form(email, req)
    if not result.ok or result.dry_run:
        raise HammerOfficeError(result.message or "Account submit failed")
    return {
        "account_created": True,
        "account_url": result.account_url,
        "message": "account created — PHASE C now",
    }


def _background_fill_then_submit(email: str, field: str, value: str) -> None:
    """Queue fill+submit on the Playwright thread (legacy — prefer _fill_then_submit_worker)."""
    try:
        _fill_then_submit_worker(email, field, value)
    except Exception as exc:
        _log.exception("fill_then_submit failed for %s: %s", email, exc)


def _background_submit(email: str) -> None:
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
        if not sess or sess.submitted or sess.submit_in_progress:
            return
    if not sess.ready.wait(timeout=120) or sess.open_error or not sess.page:
        _log.warning("background submit aborted: session not ready for %s", email)
        return
    try:
        _maybe_submit_complete_session(email, sess)
    except Exception as exc:
        _log.exception("background submit failed for %s: %s", email, exc)


def _try_auto_submit(sess: _LiveSession) -> HammerAccountResult | None:
    if sess.submitted:
        return HammerAccountResult(
            ok=True,
            message="Hammer Office account already created",
            account_url=sess.account_url,
        )
    if _missing_for_submit_session(sess):
        return None
    req = _request_from_session(sess.values)
    return submit_hammer_account_form(sess.email, req)


def _session_value_for_key(req: HammerAccountRequest, key: str, values: dict[str, str]) -> str:
    dealership = (values.get("dealership_name") or req.dealership_name or req.legal_name or "").strip()
    phone = (values.get("phone") or req.phone or req.cell_phone or "").strip()
    mapping: dict[str, str] = {
        "email": req.email.strip().lower(),
        "dealership_name": dealership,
        "name": (values.get("name") or req.name).strip(),
        "legal_name": (values.get("legal_name") or req.legal_name or dealership).strip(),
        "display_name": (values.get("display_name") or req.display_name or dealership).strip(),
        "business_type": (values.get("business_type") or req.business_type).strip(),
        "phone": phone,
        "cell_phone": (values.get("cell_phone") or req.cell_phone or phone).strip(),
        "website": (values.get("website") or req.website).strip(),
        "address": (values.get("address") or req.address).strip(),
        "currency": (values.get("currency") or req.currency).strip(),
        "timezone": (values.get("timezone") or req.timezone).strip(),
        "gst_hst": (values.get("gst_hst") or req.gst_hst).strip(),
        "qst": (values.get("qst") or req.qst).strip(),
        "role": (values.get("role") or req.role).strip(),
    }
    return mapping.get(key, "").strip()


def _apply_field_to_page(
    page: Any,
    field: str,
    value: str,
    store: dict[str, str],
    *,
    applied: set[str] | None = None,
    force: bool = False,
) -> None:
    key = normalize_field_key(field)
    if key not in FIELD_TO_FORM:
        raise HammerOfficeError(f"Unknown field {field!r} — use: {', '.join(sorted(FIELD_TO_FORM))}")
    form_name = FIELD_TO_FORM[key]
    val = value.strip()
    if not val:
        return

    if key == "website":
        val = _normalize_website(val)
    if key in ("email",):
        val = val.lower()
    if key == "business_type":
        val = resolve_business_type_value(val)

    if not force and applied is not None and key in applied and store.get(key) == val:
        return

    if key == "first_name":
        store["first_name"] = val
        last = (store.get("last_name") or "").strip()
        full_name = f"{val} {last}".strip()
        store["name"] = full_name
        playwright_fill_text(page, "account[owner_name]", full_name, fast=True)
        if applied is not None:
            applied.add("first_name")
            applied.add("name")
        return

    if key == "last_name":
        store["last_name"] = val
        first = (store.get("first_name") or "").strip()
        full_name = f"{first} {val}".strip()
        store["name"] = full_name
        playwright_fill_text(page, "account[owner_name]", full_name, fast=True)
        if applied is not None:
            applied.add("last_name")
            applied.add("name")
        return

    store[key] = val

    if key in ("phone", "cell_phone"):
        if not force and applied is not None and "phone" in applied and store.get("phone") == val:
            return
        playwright_fill_text(page, "account[phone_str]", val, fast=True)
        playwright_fill_text(page, "account[mobile_str]", val, fast=True)
        store["phone"] = val
        store["cell_phone"] = val
        if applied is not None:
            applied.add("phone")
            applied.add("cell_phone")
        return

    if form_name.endswith("[business_type]"):
        try:
            playwright_select_option(page, form_name, val, resolved=val)
        except HammerOfficeError:
            from hammer_office import _playwright_select_first_option
            _playwright_select_first_option(page, form_name)
    elif form_name.endswith("[currency]") or form_name.endswith("[timezone]"):
        playwright_select_option(page, form_name, val)
    elif form_name.endswith("[role]"):
        # account[role] is a required select dropdown — try exact match first,
        # then fall back to selecting the first available option (never text-fill a <select>).
        try:
            playwright_select_option(page, form_name, val)
        except HammerOfficeError:
            from hammer_office import _playwright_select_first_option
            _playwright_select_first_option(page, form_name)
    else:
        playwright_fill_text(page, form_name, val, fast=True)

    if applied is not None:
        applied.add(key)

    if key == "address":
        tz = infer_hammer_timezone(val)
        if tz:
            store["timezone"] = tz
            try:
                playwright_select_option(page, "account[timezone]", tz, resolved=tz)
            except HammerOfficeError:
                pass
            if applied is not None:
                applied.add("timezone")
        currency = infer_billing_currency_from_address(val)
        if currency:
            store["currency"] = currency
            try:
                playwright_select_option(page, "account[currency]", currency, resolved=currency)
            except HammerOfficeError:
                pass
            if applied is not None:
                applied.add("currency")
        ctx = address_billing_context(val)
        if ctx.get("country"):
            store["billing_country"] = str(ctx["country"])


def _close_session(email: str, *, keep_open_ms: int | None = None, force: bool = False) -> None:
    """Close Playwright browser for this email. Visible mode skips unless force=True."""
    from hammer_office import playwright_headless

    if not force and not playwright_headless():
        print(
            "[hammer-office] visible browser left open — close the Chromium window manually "
            "or call close_hammer_office_session(email)",
            flush=True,
        )
        return
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.pop(key, None)
    if not sess:
        return
    wait_ms = 0 if force else (keep_open_ms if keep_open_ms is not None else playwright_keep_open_ms())
    try:
        if wait_ms > 0 and sess.page:
            sess.page.wait_for_timeout(wait_ms)
    finally:
        if sess.browser:
            try:
                sess.browser.close()
            except Exception:
                pass
        if sess.playwright:
            try:
                sess.playwright.stop()
            except Exception:
                pass


def get_phase_b_missing_fields(email: str) -> list[str]:
    """Human-readable PHASE B fields still needed before account submit."""
    key = _norm_email(email)
    labels = {
        "name": "full name",
        "business_type": "legal business structure",
        "phone": "phone",
        "website": "website",
        "address": "full address",
    }
    if not key:
        return list(labels.values())
    if hammer_office_serverless():
        sess = _serverless_session(key)
    else:
        with _manager_lock:
            sess = _sessions.get(key)
        if not sess:
            return list(labels.values())
    missing = _missing_for_submit_session(sess)
    phase_b = [labels.get(k, k) for k in missing if k in labels]
    return phase_b


def signup_ready_for_phase_c(email: str) -> bool:
    """True only when account submit completed and Phase B fields are satisfied."""
    key = _norm_email(email)
    if not key or not account_already_created(key)[0]:
        return False
    return not get_phase_b_missing_fields(key)


def prewarm_hammer_account_form(
    email: str,
    *,
    dealership_name: str = "",
    display_name: str = "",
    name: str = "",
) -> dict[str, str | bool]:
    """Open Hammer Office in the background before I approve (no approval check)."""
    if not hammer_office_configured():
        return {"ok": False, "message": "Hammer Office not configured"}
    key = _norm_email(email)
    with _manager_lock:
        _submitted_accounts.pop(key, None)
    if hammer_office_serverless():
        sess = _serverless_session(email)
        _reset_phase_b_for_new_signup(sess)
        prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
        _serverless_save_record(
            sess.email,
            values=sess.values,
            submitted=False,
            account_url=None,
            confirmed_fields=sess.confirmed_fields,
        )
        return {"ok": True, "message": "prewarming", "prefilled": prefilled}
    if not use_playwright():
        return {"ok": False, "message": "Hammer Office not configured"}
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
        if sess and (sess.ready.is_set() or sess.opening):
            _reset_phase_b_for_new_signup(sess)
            prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
            return {
                "ok": True,
                "message": "already prewarming or ready",
                "prefilled": prefilled,
            }
        if sess:
            _close_session(email, force=True)
    sess = _LiveSession(email=key, opening=True)
    prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
    with _manager_lock:
        _sessions[key] = sess
    _executor.submit(
        _open_worker,
        key,
        email.strip(),
        dealership_name.strip(),
        display_name.strip(),
        name.strip(),
        require_approval=False,
    )
    return {"ok": True, "message": "prewarming", "prefilled": prefilled}


def open_hammer_account_form(
    email: str,
    *,
    dealership_name: str = "",
    display_name: str = "",
    name: str = "",
) -> dict[str, str | bool]:
    if not hammer_office_configured():
        raise HammerOfficeError("Hammer Office is not configured")
    if hammer_office_serverless():
        return _open_hammer_account_form_serverless(
            email,
            dealership_name=dealership_name,
            display_name=display_name,
            name=name,
        )
    if not use_playwright():
        raise HammerOfficeError("Incremental form fill requires HAMMER_OFFICE_USE_PLAYWRIGHT=1")
    require_agreement_approval(email)

    key = _norm_email(email)
    if _instant_mode():
        with _manager_lock:
            sess = _sessions.get(key)
            if sess and sess.ready.is_set() and not sess.open_error:
                prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
                return {
                    "ok": True,
                    "browser_open": True,
                    "prefilled": prefilled,
                    "message": "form ready — ask next field now",
                }
            if sess and sess.opening:
                prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
                return {
                    "ok": True,
                    "browser_open": True,
                    "prefilled": prefilled,
                    "message": "form opening — ask next field now",
                }
            if sess:
                _close_session(email)
        sess = _LiveSession(email=key, opening=True)
        prefilled = _seed_open_values(sess, email, dealership_name, display_name, name)
        with _manager_lock:
            _sessions[key] = sess
        _executor.submit(
            _open_worker,
            key,
            email.strip(),
            dealership_name.strip(),
            display_name.strip(),
            name.strip(),
            require_approval=True,
        )
        return {
            "ok": True,
            "browser_open": True,
            "prefilled": prefilled,
            "message": "form ready — ask next field now",
        }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise HammerOfficeError("Playwright is not installed") from exc

    pw = sync_playwright().start()
    browser, page = _launch_browser(pw)
    base = office_base_url()
    _login_page(page, base)
    playwright_clear_address(page)

    sess = _LiveSession(email=key, browser=browser, page=page, playwright=pw)
    sess.ready.set()
    with _manager_lock:
        _sessions[key] = sess

    filled: list[str] = []
    if email.strip():
        _apply_field_to_page(page, "email", email, sess.values, applied=sess.applied_fields)
        filled.append("email")
    if dealership_name.strip():
        dn = dealership_name.strip()
        fill_all_company_identity_fields(page, dn, sess.values)
        filled.extend(["display_name", "legal_name", "dealership_name"])
    if name.strip():
        _apply_field_to_page(page, "name", name, sess.values, applied=sess.applied_fields)
        filled.append("name")

    return {
        "ok": True,
        "browser_open": True,
        "prefilled": filled,
        "message": "Hammer Office form open — call fill_hammer_account_field after each answer",
    }


def fill_hammer_account_field(email: str, field: str, value: str) -> dict[str, str | bool]:
    if not hammer_office_configured():
        raise HammerOfficeError("Hammer Office is not configured")
    if hammer_office_serverless():
        return _fill_hammer_account_field_serverless(email, field, value)
    key = normalize_field_key(field)
    if key == "address":
        from hammer_office import address_is_hammer_placeholder

        if address_is_hammer_placeholder(value):
            raise HammerOfficeError(
                "address looks like the form placeholder — ask the dealer for their real street address"
            )
    norm_email = _norm_email(email)
    existing = _account_created_fill_response(norm_email)
    if existing:
        existing["field"] = field
        existing["message"] = "account already created — PHASE C only"
        return existing
    with _manager_lock:
        sess = _sessions.get(norm_email)
    if not sess:
        try:
            _log.info("Dynamically prewarming Hammer Office session for %s in fill_hammer_account_field", norm_email)
            prewarm_hammer_account_form(norm_email)
            with _manager_lock:
                sess = _sessions.get(norm_email)
        except Exception as exc:
            _log.warning("Dynamic prewarm failed for %s: %s", norm_email, exc)
    if not sess:
        raise HammerOfficeError(
            "No open Hammer Office form for this email — call capture_lead first (prewarms the browser), "
            "then open_hammer_account_form after I approve; use the exact same email on every tool call"
        )

    if _instant_mode():
        with _manager_lock:
            if sess.submitted:
                return {
                    "ok": True,
                    "field": field,
                    "account_created": True,
                    "account_url": sess.account_url,
                    "message": "account already created — PHASE C only",
                }
            # Surface a previously stored submit error so the AI can act on it.
            if sess.submit_error:
                err = sess.submit_error
                sess.submit_error = None
                return {
                    "ok": False,
                    "field": field,
                    "account_created": False,
                    "message": (
                        f"account submit failed — {err[:200]}; "
                        "retry: call create_hammer_account with all collected fields"
                    ),
                }
            _optimistic_store(sess, key, value)
            _mark_field_confirmed(sess, field)
            if not sess.ready.is_set():
                sess.pending_fills.append((field, value))
            ready = sess.ready.is_set()
        out = _instant_fill_response(sess, field, key, value)
        if out.get("ready_to_submit"):
            try:
                require_agreement_approval(norm_email)
            except HammerOfficeError as exc:
                out.pop("ready_to_submit", None)
                out["message"] = (
                    f"blocked — {str(exc)[:160]}; confirm I approve on the agreement email, "
                    "then call check_agreement_approval; local debug: Approve email on /debug/hammer-account"
                )
                return out
            if not session_browser_ready(norm_email):
                try:
                    direct = direct_create_account_for_voice(norm_email, field)
                    out.pop("ready_to_submit", None)
                    out.update(direct)
                    return out
                except HammerOfficeError as exc:
                    out.pop("ready_to_submit", None)
                    out["ok"] = False
                    out["account_created"] = False
                    out["message"] = (
                        f"account submit failed — {str(exc)[:200]}; "
                        "retry create_hammer_account with collected fields"
                    )
                    return out
            future = _executor.submit(_fill_then_submit_worker, norm_email, field, value)
            wait_s = _submit_wait_seconds()
            if wait_s > 0:
                try:
                    submit_out = future.result(timeout=wait_s)
                    out.pop("ready_to_submit", None)
                    out.update(submit_out)
                    return out
                except TimeoutError:
                    _log.warning(
                        "account submit still running after %.0fs for %s — polling for success",
                        wait_s,
                        norm_email,
                    )
                    late = _poll_account_created_after_submit(norm_email)
                    if late:
                        late["field"] = field
                        out.pop("ready_to_submit", None)
                        out.update(late)
                        return out
                except Exception as exc:
                    late = _poll_account_created_after_submit(norm_email, max_seconds=6.0)
                    if late:
                        late["field"] = field
                        out.pop("ready_to_submit", None)
                        out.update(late)
                        return out
                    _log.error("account submit failed for %s: %s", norm_email, exc)
                    try:
                        direct = direct_create_account_for_voice(norm_email, field)
                        out.pop("ready_to_submit", None)
                        out.update(direct)
                        return out
                    except HammerOfficeError as direct_exc:
                        _log.error(
                            "direct account create fallback failed for %s: %s",
                            norm_email,
                            direct_exc,
                        )
                    out.pop("ready_to_submit", None)
                    out["ok"] = False
                    out["account_created"] = False
                    out["message"] = (
                        f"account submit failed — {str(exc)[:200]}; "
                        "retry create_hammer_account or fix the form in Chromium"
                    )
                    return out
            else:

                def _on_submit_done(f: Any, _email: str = norm_email) -> None:
                    exc = f.exception()
                    if exc:
                        _log.error("background submit failed for %s: %s", _email, exc)
                        with _manager_lock:
                            s = _sessions.get(_email)
                            if s and not s.submitted:
                                s.submit_error = str(exc)
                                s.submit_in_progress = False

                future.add_done_callback(_on_submit_done)
            out.pop("ready_to_submit", None)
            out["account_created"] = False
            out["message"] = (
                "ok — account submitting in background — call fill_hammer_account_field again in a few seconds "
                "to confirm account_created; do not promise Welcome email until confirmed"
            )
        elif ready:
            _executor.submit(_background_fill, norm_email, field, value)
        return out

    if not sess.page:
        raise HammerOfficeError("Hammer Office form is still opening — try again in a moment")
    _apply_field_to_page(sess.page, field, value, sess.values, applied=sess.applied_fields)
    _mark_field_confirmed(sess, field)
    key = normalize_field_key(field)

    try:
        auto = _try_auto_submit(sess)
    except HammerOfficeError as exc:
        return {
            "ok": False,
            "field": field,
            "account_created": False,
            "submit_error": str(exc),
            "browser_open": bool(sess.page),
            "message": (
                f"submit failed — {exc}. "
                "Browser left open; fix the form or retry. "
                "Duplicate company names get 3 random digits appended automatically on resubmit."
            ),
        }
    if auto is not None:
        return {
            "ok": True,
            "field": field,
            "account_created": True,
            "account_url": auto.account_url,
            "dry_run": auto.dry_run,
            "message": "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived; wait before activate/password/card",
        }

    return _instant_fill_response(sess, field, key, value)


def submit_hammer_account_form(
    email: str,
    req: HammerAccountRequest | None = None,
) -> HammerAccountResult:
    require_agreement_approval(email)
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)

    if not sess and req is None:
        raise HammerOfficeError("No open form — open_hammer_account_form or pass full account payload")

    if sess and sess.submitted:
        return HammerAccountResult(
            ok=True,
            message="Hammer Office account already created",
            account_url=sess.account_url,
        )

    if req is None and sess:
        req = _request_from_session(sess.values)

    if req and sess:
        _merge_request_into_session(sess, req)

    if not sess:
        if req:
            from hammer_office import create_hammer_account

            return create_hammer_account(req)
        raise HammerOfficeError("Session lost — reopen form")

    if not sess.page:
        if not sess.ready.wait(timeout=120):
            raise HammerOfficeError("Hammer Office browser session is not ready — reopen the form")
        if not sess.page:
            raise HammerOfficeError("Hammer Office browser session lost — reopen the form")

    if req:
        _sync_session_values_to_page(sess, req)

    page = sess.page
    addr = sess.values.get("address", "").strip()
    if addr and "address" not in sess.applied_fields:
        on_form = read_address_field(page)
        probe = addr[: min(12, len(addr))].lower()
        if not on_form or (probe and probe not in on_form.lower()):
            _apply_field_to_page(
                sess.page,
                "address",
                addr,
                sess.values,
                applied=sess.applied_fields,
                force=True,
            )
    still_missing = _missing_for_submit_session(sess)
    if still_missing:
        raise HammerOfficeError(
            f"Cannot submit yet — still missing: {', '.join(still_missing)}"
        )

    if office_dry_run():
        from hammer_office import playwright_headless

        if playwright_headless():
            _close_session(email, force=True)
        return HammerAccountResult(ok=True, message="dry run — form filled, not submitted", dry_run=True)

    with _manager_lock:
        if sess:
            sess.submit_in_progress = True
    try:
        url = playwright_submit_new_account(page, sess.values)
    except HammerOfficeError:
        with _manager_lock:
            live = _sessions.get(key)
            if live:
                live.submit_in_progress = False
        if keep_browser_open_on_submit_failure():
            raise
        _close_session(email, force=True)
        raise
    finally:
        with _manager_lock:
            live = _sessions.get(key)
            if live:
                live.submit_in_progress = False

    if not account_create_succeeded(url):
        legal = (sess.values.get("legal_name") or "").strip()
        raise HammerOfficeError(
            "Account was not created — still on the signup form or an error page. "
            f"url={url!r} legal_name={legal!r}. "
            "If you saw 'company name taken', the server should retry with 3 random digits on "
            "legal, company, and display fields."
        )

    account_url = account_url_from_submit(url)
    with _manager_lock:
        live = _sessions.get(key)
        if live:
            live.submitted = True
            live.account_url = account_url
        _submitted_accounts[key] = account_url
    if playwright_keep_browser_after_submit():
        return HammerAccountResult(
            ok=True,
            message=f"Hammer Office account created — browser left open (legal name may include suffix): {legal!r}",
            account_url=account_url,
        )
    keep_ms = playwright_keep_open_ms()
    _close_session(email, keep_open_ms=keep_ms)
    legal = (sess.values.get("legal_name") or "").strip()
    return HammerAccountResult(
        ok=True,
        message=f"Hammer Office account created (company name: {legal})",
        account_url=account_url,
    )


def close_hammer_office_session(email: str, *, wait_ms: int | None = None, force: bool = True) -> None:
    """Close browser session. Forced close runs on the Playwright worker thread."""
    key = _norm_email(email)
    with _manager_lock:
        has_live = key in _sessions
    if has_live and force:
        try:
            _executor.submit(_close_session, email, keep_open_ms=wait_ms, force=True).result(timeout=90)
            return
        except Exception as exc:
            _log.warning("executor close failed for %s: %s", email, exc)
    _close_session(email, keep_open_ms=wait_ms, force=force)


def session_active(email: str) -> bool:
    return _norm_email(email) in _sessions


def session_browser_ready(email: str) -> bool:
    """True only when the persistent Playwright session is open and usable."""
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
    if not sess:
        return False
    return bool(sess.page) and sess.ready.is_set() and not sess.open_error


def get_session_values(email: str) -> dict[str, str]:
    key = _norm_email(email)
    if hammer_office_serverless():
        rec = _serverless_load_record(key)
        return dict(rec.get("values") or {})
    with _manager_lock:
        sess = _sessions.get(key)
        if not sess:
            return {}
        return dict(sess.values)


def record_account_created(email: str, account_url: str | None) -> None:
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
        if sess:
            sess.submitted = True
            sess.account_url = account_url
            sess.submit_in_progress = False
            sess.submit_error = None
        _submitted_accounts[key] = account_url
    if account_url:
        try:
            from voice_dashboard_store import update_account_url_by_email
            update_account_url_by_email(key, account_url)
        except Exception:
            pass


def _voice_account_created_response(field: str, account_url: str | None) -> dict[str, str | bool]:
    return {
        "ok": True,
        "field": field,
        "account_created": True,
        "account_url": account_url or "",
        "message": (
            "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived; "
            "do not mention activate, password, or card yet; do not call create_hammer_account"
        ),
    }


def direct_create_account_for_voice(email: str, field: str = "") -> dict[str, str | bool]:
    """Create account via one-shot Playwright/HTTP when the persistent browser is not ready."""
    key = _norm_email(email)
    with _manager_lock:
        sess = _sessions.get(key)
    if not sess:
        raise HammerOfficeError(
            "No signup session — call capture_lead first, then fill account fields"
        )
    missing = _missing_for_submit_session(sess)
    if missing:
        raise HammerOfficeError(f"Cannot create account — still missing: {', '.join(missing)}")
    req = _request_from_session(sess.values)
    from hammer_office import create_hammer_account

    result = create_hammer_account(req, prefer_direct=True)
    if not result.ok or result.dry_run:
        raise HammerOfficeError(result.message or "Account creation failed")
    return _voice_account_created_response(field, result.account_url)


def account_already_created(email: str) -> tuple[bool, str | None]:
    key = _norm_email(email)
    if hammer_office_serverless():
        rec = _serverless_load_record(key)
        if rec.get("submitted"):
            url = rec.get("account_url")
            return True, str(url) if url else None
        
        # On serverless (Vercel), if not found locally, query the persistent Fly.io server
        try:
            import httpx
            from agreement_approvals import _fly_approval_api_base
            base = _fly_approval_api_base()
            response = httpx.get(f"{base}/api/hammer/account-url", params={"email": key}, timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                if data.get("account_created"):
                    return True, data.get("account_url")
        except Exception:
            pass
    with _manager_lock:
        sess = _sessions.get(key)
        if sess and sess.submitted:
            return True, sess.account_url
        if key in _submitted_accounts:
            return True, _submitted_accounts[key]
    
    # Query persistent SQLite database on Fly.io
    try:
        from voice_dashboard_store import find_account_url_by_email
        url = find_account_url_by_email(key)
        if url:
            return True, url
    except Exception:
        pass
        
    return False, None
