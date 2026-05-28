"""Local-only Hammer Office account-creation debug helpers (visible Chromium)."""

from __future__ import annotations

import time
from typing import Any

from agreement_approvals import agreement_approval_status, record_agreement_approval, register_pending_agreement
from hammer_office import HammerOfficeError, hammer_office_configured, hammer_office_debug_mode, playwright_headless
from hammer_office_session import (
    _session_debug_snapshot,
    close_hammer_office_session,
    fill_hammer_account_field,
    open_hammer_account_form,
    session_active,
)


def debug_hammer_config() -> dict[str, Any]:
    return {
        "debug_mode": hammer_office_debug_mode(),
        "hammer_configured": hammer_office_configured(),
        "headless": playwright_headless(),
        "visible_browser": not playwright_headless(),
    }


def debug_session_status(email: str) -> dict[str, Any]:
    """Live Playwright session + approval state for voice troubleshooting."""
    from hammer_office_session import _manager_lock, _sessions, _norm_email

    key = email.strip().lower()
    with _manager_lock:
        sess = _sessions.get(_norm_email(key))
    snap = _session_debug_snapshot(sess, key)
    snap["session_active"] = session_active(key)
    return snap


def debug_approval_status(email: str) -> dict[str, Any]:
    """Whether Zapier (or debug Approve) recorded I approve for this signup email."""
    from agreement_approvals import agreement_approval_status, normalize_email

    key = normalize_email(email)
    status = agreement_approval_status(key, wait_seconds=0)
    return {
        "email": key,
        "approved": bool(status.get("approved")),
        "pending": bool(status.get("pending")),
        "approved_at": status.get("approvedAt"),
        "dealership": status.get("dealership"),
        "reply_text_preview": (str(status.get("replyText") or "")[:120] or None),
        "local_hint": (
            "If you replied I approve by email but approved is false: run .\\start-ngrok.ps1, "
            "point Zap 2 POST to https://<ngrok-host>/api/zapier/approval, or click Approve email on this panel."
        ),
    }


def debug_approve_email(email: str, *, dealership: str = "") -> dict[str, Any]:
    key = email.strip().lower()
    register_pending_agreement(key, dealership=dealership.strip() or "Debug Motors")
    record_agreement_approval(key, approved=True, source="local_debug")
    status = agreement_approval_status(key, wait_seconds=0)
    return {"ok": True, "email": key, "approved": bool(status.get("approved"))}


def debug_run_sample_flow(
    email: str,
    *,
    dealership: str = "Debug Motors LLC",
    name: str = "Jordan Smith",
    address: str = "1200 Congress Ave, Austin, TX 78701",
    pause_seconds: float = 1.5,
) -> dict[str, Any]:
    """
    Mirror the voice agent PHASE B sequence with pauses so you can watch Chromium fill each field.
    """
    if not hammer_office_debug_mode():
        raise HammerOfficeError("HAMMER_OFFICE_DEBUG=1 required (local API only)")
    if not hammer_office_configured():
        raise HammerOfficeError("Set HAMMER_OFFICE_EMAIL and HAMMER_OFFICE_PASSWORD in server/.env")

    key = email.strip().lower()
    steps: list[dict[str, Any]] = []

    def _step(label: str, fn: Any) -> None:
        result = fn()
        steps.append({"step": label, "result": result})
        if pause_seconds > 0:
            time.sleep(pause_seconds)

    debug_approve_email(key, dealership=dealership)
    steps.append({"step": "approve_email", "result": {"approved": True}})

    _step(
        "open_form",
        lambda: open_hammer_account_form(
            key,
            dealership_name=dealership,
            display_name=dealership,
            name=name,
        ),
    )

    fields: list[tuple[str, str]] = [
        ("name", name),
        ("business_type", "Corporation"),
        ("phone", "5125550199"),
        ("website", "debugmotors.com"),
        ("address", address),
    ]
    for field, value in fields:
        _step(f"fill_{field}", lambda f=field, v=value: fill_hammer_account_field(key, f, v))

    last = steps[-1]["result"] if steps else {}
    return {
        "ok": True,
        "email": key,
        "steps": steps,
        "account_created": bool(last.get("account_created")),
        "account_url": last.get("account_url"),
        "message": str(last.get("message", "")),
    }
