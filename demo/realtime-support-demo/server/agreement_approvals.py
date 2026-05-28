"""Persistent store for agreement-email \"I approve\" replies (Zapier Gmail → POST /api/zapier/approval)."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parent
_DEFAULT_STORE = _SERVER_DIR / ".data" / "agreement_approvals.json"

_store_lock = Lock()


def _is_serverless() -> bool:
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _store_path() -> Path:
    override = os.environ.get("REALTIME_SALES_APPROVALS_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_serverless():
        # Vercel/Lambda: repo .data/ is read-only — use /tmp for approval polling state.
        return Path("/tmp/realtime-sales-demo/agreement_approvals.json")
    return _DEFAULT_STORE


def normalize_email(email: str) -> str:
    """Lowercase; strip Gmail/Zap 'Display Name <addr@domain>' to addr@domain."""
    raw = email.strip()
    angle = re.search(r"<([^>]+)>", raw)
    if angle:
        raw = angle.group(1).strip()
    return raw.lower()


def normalize_reply_text(reply_text: str | None) -> str:
    """Decode HTML entities from Gmail/Zapier snippets before phrase matching."""
    if not reply_text:
        return ""
    text = html.unescape(reply_text.strip())
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def reply_indicates_approval(reply_text: str | None) -> bool:
    text = normalize_reply_text(reply_text)
    if not text:
        return False
    lowered = text.lower()
    if re.search(r"\bdisapprove\b", lowered):
        return False
    return bool(
        re.search(r"\bi\s+approve\b", lowered)
        or re.search(r"\bi\s+approved\b", lowered)
        or re.search(r"\bapprove\b", lowered)
        or re.search(r"\bapproved\b", lowered)
        or re.search(r"approved\s+the\s+terms", lowered)
        or re.search(r"\byes\b.*\bapprove", lowered)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_store() -> dict[str, Any]:
    path = _store_path()
    if not path.is_file():
        return {"pending": {}, "approved": {}}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pending", {})
    data.setdefault("approved", {})
    if not isinstance(data["pending"], dict):
        data["pending"] = {}
    if not isinstance(data["approved"], dict):
        data["approved"] = {}
    return data


def _save_store(data: dict[str, Any]) -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        # Do not block Zapier / agreement email if approval store is unavailable.
        pass


def _fly_approval_secret() -> str:
    return os.environ.get("ZAPIER_APPROVAL_CALLBACK_SECRET", "").strip()


def _post_fly_approval_api(path: str, payload: dict[str, Any]) -> bool:
    """POST to Fly durable approval store (Vercel /tmp is not shared across instances)."""
    if not _use_fly_approval_store():
        return False
    secret = _fly_approval_secret()
    if not secret:
        return False
    import httpx

    url = f"{_fly_approval_api_base()}{path}"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"X-Zapier-Secret": secret},
            timeout=12.0,
        )
        return response.is_success
    except Exception as exc:
        print(f"[agreement_approvals] Fly POST {path} failed: {exc}", flush=True)
        return False


def reset_agreement_approval(email: str, *, sync_fly: bool = True) -> bool:
    """Remove an email from pending and approved so a fresh agreement can be sent."""
    key = normalize_email(email)
    if not key:
        return False
    had_local = False
    with _store_lock:
        data = _load_store()
        had_pending = key in data["pending"]
        had_approved = key in data["approved"]
        data["pending"].pop(key, None)
        data["approved"].pop(key, None)
        if had_pending or had_approved:
            _save_store(data)
            had_local = True
    if sync_fly and _use_fly_approval_store():
        if _post_fly_approval_api("/api/zapier/reset-approval", {"email": key}):
            return True
    return had_local


def voice_approve_on_call_enabled() -> bool:
    """Browser demo fallback when Gmail→Zapier has not synced I approve yet."""
    raw = os.environ.get("REALTIME_SALES_VOICE_APPROVE_ON_CALL", "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    return _is_serverless()


def sync_pending_agreement_to_fly(
    email: str,
    *,
    dealership: str = "",
    selected_plan: str = "",
) -> None:
    payload: dict[str, Any] = {"email": normalize_email(email)}
    if dealership:
        payload["dealership"] = dealership
    if selected_plan:
        payload["selected_plan"] = selected_plan
    _post_fly_approval_api("/api/zapier/register-pending", payload)


def ensure_voice_call_approval(email: str) -> dict[str, str | bool]:
    """
    When agreement is pending and the visitor confirmed I approve on the call,
    record approval locally and on Fly so account creation is not blocked by Zap lag.
    """
    key = normalize_email(email)
    status = agreement_approval_status(key, wait_seconds=0)
    if status.get("approved"):
        return status
    if not voice_approve_on_call_enabled():
        return status
    if not status.get("pending"):
        return status
    record_agreement_approval(
        key,
        approved=True,
        reply_text="I approve",
        source="voice_call",
    )
    _post_fly_approval_api(
        "/api/zapier/approval",
        {"email": key, "approved": True, "reply_text": "I approve"},
    )
    return agreement_approval_status(key, wait_seconds=0)


def agreement_email_already_queued(email: str) -> bool:
    """True when an agreement email was already sent and is pending or approved."""
    key = normalize_email(email)
    if not key:
        return False
    status = agreement_approval_status(key, wait_seconds=0)
    return bool(status.get("pending") or status.get("approved"))


def register_pending_agreement(
    email: str,
    *,
    dealership: str = "",
    product_line: str = "",
    selected_plan: str = "",
) -> dict[str, str | bool]:
    """Called when agreement email is sent — voice agent waits for email reply."""
    key = normalize_email(email)
    entry: dict[str, str | bool] = {
        "email": key,
        "approved": False,
        "agreementSentAt": _utc_now(),
    }
    if dealership:
        entry["dealership"] = dealership
    if product_line:
        entry["productLine"] = product_line
    if selected_plan:
        entry["selectedPlan"] = selected_plan
    with _store_lock:
        data = _load_store()
        data["pending"][key] = entry
        data["approved"].pop(key, None)
        _save_store(data)
    return entry


def record_agreement_approval(
    email: str,
    *,
    approved: bool = True,
    reply_text: str | None = None,
    source: str = "zapier",
) -> dict[str, str | bool]:
    key = normalize_email(email)
    # Zap 2 sets approved=true on the Gmail step; only override to False when the
    # body clearly is not an approval (reply_text present and no approve phrase).
    # Zap 2 already filters Gmail for I approve — trust approved=true from Zapier.
    if source in ("zapier", "voice_call") and approved:
        is_approved = True
    elif reply_text is not None and normalize_reply_text(reply_text):
        if reply_indicates_approval(reply_text):
            is_approved = True
        else:
            is_approved = bool(approved)
    else:
        is_approved = bool(approved)
    entry: dict[str, str | bool] = {
        "email": key,
        "approved": is_approved,
        "source": source,
    }
    if is_approved:
        entry["approvedAt"] = _utc_now()
    if reply_text:
        entry["replyText"] = reply_text.strip()[:2000]
    with _store_lock:
        data = _load_store()
        pending = data["pending"].pop(key, None)
        if pending and isinstance(pending, dict):
            for field in ("dealership", "productLine", "selectedPlan", "agreementSentAt"):
                if field in pending and field not in entry:
                    entry[field] = pending[field]
        if is_approved:
            data["approved"][key] = entry
        else:
            data["pending"][key] = {**dict(pending or {}), **entry, "approved": False}
        _save_store(data)
    return entry


def _poll_max_wait_seconds() -> int:
    raw = os.environ.get("AGREEMENT_APPROVAL_POLL_MAX_SECONDS", "20").strip()
    try:
        return max(0, min(60, int(raw)))
    except ValueError:
        return 20


def _poll_interval_seconds() -> float:
    raw = os.environ.get("AGREEMENT_APPROVAL_POLL_INTERVAL_SECONDS", "1.0").strip()
    try:
        return max(0.25, min(5.0, float(raw)))
    except ValueError:
        return 1.0


def just_replied_poll_wait_seconds() -> int:
    """How long check_agreement_approval polls when the visitor says they just replied."""
    raw = os.environ.get("AGREEMENT_APPROVAL_JUST_REPLIED_WAIT_SECONDS", "12").strip()
    try:
        return max(0, min(_poll_max_wait_seconds(), int(raw)))
    except ValueError:
        return 12


def _fly_approval_api_base() -> str:
    """Fly telephony host — Zap 2 POST /api/zapier/approval lands here in production."""
    explicit = os.environ.get("FLY_APPROVAL_API_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    telephony = os.environ.get("TELEPHONY_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if telephony:
        return telephony
    health = os.environ.get(
        "FLY_TELEPHONY_HEALTH_URL",
        "https://hammer-voice-telephony.fly.dev/api/health",
    ).strip()
    if "/api/health" in health:
        return health.split("/api/health", 1)[0].rstrip("/")
    if health.startswith("http"):
        return health.rstrip("/")
    return "https://hammer-voice-telephony.fly.dev"


def _use_fly_approval_store() -> bool:
    """Vercel/serverless has isolated /tmp; production Zap writes approvals on Fly."""
    if _is_serverless():
        return True
    return bool(os.environ.get("FLY_APPROVAL_API_BASE_URL", "").strip())


def _agreement_approval_status_from_fly(
    key: str,
    *,
    wait_seconds: int,
    max_wait_seconds: int,
) -> dict[str, str | bool]:
    import httpx

    capped = min(max(0, wait_seconds), max(0, max_wait_seconds))
    url = f"{_fly_approval_api_base()}/api/zapier/approval-status"
    try:
        response = httpx.get(
            url,
            params={"email": key, "wait": capped},
            timeout=20.0 + float(capped),
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return {
                    "approved": bool(data.get("approved")),
                    "email": str(data.get("email") or key),
                    "pending": bool(data.get("pending")),
                }
        print(
            f"[agreement_approvals] Fly approval-status HTTP {response.status_code} "
            f"for {key} (wait={capped}s)",
            flush=True,
        )
    except Exception as exc:
        print(
            f"[agreement_approvals] Fly approval-status request failed for {key}: {exc}",
            flush=True,
        )
    return {"approved": False, "email": key, "pending": False}


def agreement_approval_status(
    email: str,
    *,
    wait_seconds: int = 0,
    poll_interval_seconds: float | None = None,
    max_wait_seconds: int | None = None,
) -> dict[str, str | bool]:
    """Return approval state; optionally poll until wait_seconds elapses (Gmail→Zap lag)."""
    import time

    key = normalize_email(email)
    max_wait = _poll_max_wait_seconds() if max_wait_seconds is None else max(0, max_wait_seconds)
    capped = min(max(0, wait_seconds), max_wait)

    if _use_fly_approval_store():
        return _agreement_approval_status_from_fly(
            key,
            wait_seconds=capped,
            max_wait_seconds=max_wait,
        )

    interval = _poll_interval_seconds() if poll_interval_seconds is None else poll_interval_seconds
    deadline = time.monotonic() + capped
    while True:
        status = _approval_status_once(key)
        if status.get("approved") or capped <= 0:
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(min(interval, max(0.25, deadline - time.monotonic())))


def _approval_status_once(key: str) -> dict[str, str | bool]:
    with _store_lock:
        data = _load_store()
        approved_entry = data["approved"].get(key)
        pending_entry = data["pending"].get(key)
    if approved_entry and approved_entry.get("approved"):
        return {"approved": True, "email": key, **approved_entry}
    if pending_entry:
        return {"approved": False, "email": key, "pending": True, **pending_entry}
    return {"approved": False, "email": key, "pending": False}
