"""Outbound phone callbacks via Twilio Voice → OpenAI Realtime SIP."""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from lead_zapier import normalize_phone_e164

logger = logging.getLogger("outbound_telephony")

CORRELATION_TTL_S = 300.0
RATE_LIMIT_PHONE_H = 3
RATE_LIMIT_IP_H = 10


def env_truthy(raw: str | None) -> bool:
    v = (raw or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def outbound_enabled() -> bool:
    if not env_truthy(os.environ.get("TWILIO_OUTBOUND_ENABLED", "")):
        return False
    return bool(
        os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        and os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        and os.environ.get("DEMO_PHONE_NUMBER", "").strip()
    )


def telephony_public_base_url() -> str:
    explicit = os.environ.get("TELEPHONY_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    fly_app = os.environ.get("FLY_APP_NAME", "").strip()
    if fly_app:
        return f"https://{fly_app}.fly.dev"
    return os.environ.get("REALTIME_SALES_PUBLIC_BASE_URL", "").strip().rstrip("/")


def outbound_api_public_url() -> str | None:
    base = telephony_public_base_url()
    if not base:
        return None
    return f"{base}/api/telephony/callback"


@dataclass
class OutboundCallRecord:
    cid: str
    phone: str
    created_at: float
    status: str = "queued"
    twilio_call_sid: str = ""
    updated_at: float = field(default_factory=time.time)


_correlations: dict[str, OutboundCallRecord] = {}
_rate_phone: dict[str, list[float]] = {}
_rate_ip: dict[str, list[float]] = {}


def _prune_rates(store: dict[str, list[float]], now: float, window: float = 3600.0) -> None:
    for key in list(store.keys()):
        store[key] = [t for t in store[key] if now - t < window]
        if not store[key]:
            store.pop(key, None)


def _check_rate_limit(store: dict[str, list[float]], key: str, limit: int, now: float) -> bool:
    _prune_rates(store, now)
    return len(store.get(key, [])) < limit


def _record_rate_hit(store: dict[str, list[float]], key: str, now: float) -> None:
    store.setdefault(key, []).append(now)


def _prune_correlations(now: float | None = None) -> None:
    now = now or time.time()
    for cid in list(_correlations.keys()):
        if now - _correlations[cid].created_at > CORRELATION_TTL_S:
            _correlations.pop(cid, None)


def _allowlist() -> set[str]:
    raw = os.environ.get("TWILIO_OUTBOUND_ALLOWLIST", "").strip()
    if not raw:
        return set()
    return {normalize_phone_e164(p.strip()) for p in raw.split(",") if p.strip()}


def _new_cid() -> str:
    return secrets.token_urlsafe(16)


def get_twilio_client() -> Any:
    from twilio.rest import Client

    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        raise RuntimeError("Twilio credentials not configured")
    return Client(sid, token)


def validate_callback_request(*, phone: str, consent: bool, client_ip: str) -> str:
    """Return normalized E.164 or raise ValueError."""
    if not consent:
        raise ValueError("Consent is required to receive a call")
    normalized = normalize_phone_e164(phone)
    digits = normalized.replace("+", "")
    if len(digits) < 10:
        raise ValueError("Enter a valid phone number")
    allow = _allowlist()
    if allow and normalized not in allow:
        raise ValueError("This number is not allowed for outbound callbacks in this environment")
    now = time.time()
    if not _check_rate_limit(_rate_phone, normalized, RATE_LIMIT_PHONE_H, now):
        raise ValueError("Too many callback requests for this number. Try again later.")
    ip_key = client_ip or "unknown"
    if not _check_rate_limit(_rate_ip, ip_key, RATE_LIMIT_IP_H, now):
        raise ValueError("Too many callback requests. Try again later.")
    _record_rate_hit(_rate_phone, normalized, now)
    _record_rate_hit(_rate_ip, ip_key, now)
    return normalized


def voice_phone_disclosure_enabled() -> bool:
    """Toggle Twilio pre-connect disclosure without removing VOICE_PHONE_DISCLOSURE text."""
    raw = os.environ.get("VOICE_PHONE_DISCLOSURE_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def voice_phone_disclosure() -> str:
    """Audible notice read by Twilio <Say> at the start of every phone call.

    Set VOICE_PHONE_DISCLOSURE in env to customise wording (have counsel review).
    Set VOICE_PHONE_DISCLOSURE_ENABLED=0 to skip playback while keeping the text for later.
    """
    if not voice_phone_disclosure_enabled():
        return ""
    return os.environ.get(
        "VOICE_PHONE_DISCLOSURE",
        "This call is from Hammer. It may be recorded and uses an artificial intelligence voice assistant.",
    ).strip()


def voice_phone_disclosure_audio_url() -> str:
    """Optional pre-rendered disclosure audio for lower and more consistent Twilio latency."""
    return os.environ.get("VOICE_PHONE_DISCLOSURE_AUDIO_URL", "").strip()


def _disclosure_element(text: str) -> str:
    """Return a Twilio disclosure element, preferring <Play> over TTS when configured."""
    if not voice_phone_disclosure_enabled():
        return ""
    audio_url = voice_phone_disclosure_audio_url()
    if audio_url:
        return f"<Play>{escape(audio_url)}</Play>"
    if not text:
        return ""
    return f'<Say voice="Polly.Joanna">{escape(text)}</Say>'


def build_bridge_twiml(*, phone: str, sip_uri: str) -> str:
    """Outbound TwiML: play disclosure then bridge callee to OpenAI SIP."""
    phone_header = escape(phone, {'"': "&quot;"})
    sip_target = escape(sip_uri, {'"': "&quot;"})
    say = _disclosure_element(voice_phone_disclosure())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{say}"
        '<Dial answerOnBridge="true">'
        f'<Sip sipHeaders="X-Customer-Phone={phone_header}">{sip_target}</Sip>'
        "</Dial>"
        "</Response>"
    )


def build_inbound_connect_twiml(*, sip_uri: str) -> str:
    """Inbound TwiML: play disclosure then connect caller to OpenAI SIP.

    Use this as the Twilio Voice URL on your demo number so every inbound call
    hears the legal notice before Hannah answers.
    """
    sip_target = escape(sip_uri, {'"': "&quot;"})
    say = _disclosure_element(voice_phone_disclosure())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{say}"
        '<Dial answerOnBridge="true">'
        f"<Sip>{sip_target}</Sip>"
        "</Dial>"
        "</Response>"
    )


def get_record(cid: str) -> OutboundCallRecord | None:
    _prune_correlations()
    return _correlations.get(cid)


def record_status(cid: str, *, status: str, twilio_call_sid: str = "") -> None:
    rec = _correlations.get(cid)
    if not rec:
        return
    rec.status = status.strip().lower() or rec.status
    rec.updated_at = time.time()
    if twilio_call_sid:
        rec.twilio_call_sid = twilio_call_sid


def callback_status_public(cid: str) -> dict[str, Any] | None:
    rec = get_record(cid)
    if not rec:
        return None
    return {
        "cid": rec.cid,
        "status": rec.status,
        "phone": rec.phone,
        "twilio_call_sid": rec.twilio_call_sid or None,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


def demo_phone_number_e164() -> str:
    """Twilio outbound caller ID / demo line (not the visitor's cell)."""
    raw = os.environ.get("DEMO_PHONE_NUMBER", "").strip()
    if not raw:
        return ""
    try:
        return normalize_phone_e164(raw)
    except ValueError:
        return raw


def _latest_active_outbound_callee(*, now: float | None = None) -> str:
    """Phone the visitor entered on the site for the newest in-flight outbound call."""
    now = now or time.time()
    _prune_correlations(now)
    active = (
        "queued",
        "initiated",
        "ringing",
        "in-progress",
        "answered",
        "completed",
    )
    candidates = [
        r
        for r in _correlations.values()
        if r.status in active and now - r.created_at < 120.0
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda r: r.updated_at, reverse=True)
    return candidates[0].phone or ""


def sip_phone_needs_outbound_lookup(sip_phone: str) -> bool:
    """True when SIP headers likely show Twilio trunk / demo ID, not the visitor."""
    raw = (sip_phone or "").strip()
    if not raw:
        return True
    if "twilio" in raw.lower():
        return True
    demo = demo_phone_number_e164()
    if not demo:
        return False
    try:
        return normalize_phone_e164(raw) == demo
    except ValueError:
        return raw == demo


def resolve_outbound_caller_phone(sip_phone: str) -> str:
    """Prefer site-entered callee when SIP shows trunk/demo caller ID (outbound Call me)."""
    if not sip_phone_needs_outbound_lookup(sip_phone):
        return sip_phone
    callee = _latest_active_outbound_callee()
    if callee:
        return callee
    return sip_phone


def is_active_outbound_callee(phone: str) -> bool:
    """True when this number is the destination of a recent Call-me callback."""
    raw = (phone or "").strip()
    if not raw:
        return False
    try:
        normalized = normalize_phone_e164(raw)
    except ValueError:
        normalized = raw
    now = time.time()
    _prune_correlations(now)
    active = (
        "queued",
        "initiated",
        "ringing",
        "in-progress",
        "answered",
        "completed",
    )
    for rec in _correlations.values():
        if rec.phone != normalized:
            continue
        if rec.status not in active:
            continue
        if now - rec.created_at > CORRELATION_TTL_S:
            continue
        return True
    return False


def customer_phone_from_dynamic_variables(dyn: dict[str, Any] | None) -> str:
    """Visitor cell from Twilio X-Customer-Phone (if ElevenLabs exposes it)."""
    if not dyn:
        return ""
    for key in (
        "X-Customer-Phone",
        "x_customer_phone",
        "x-customer-phone",
        "customer_phone",
        "Customer-Phone",
    ):
        val = str(dyn.get(key) or "").strip()
        if val:
            try:
                return normalize_phone_e164(val)
            except ValueError:
                return val
    return ""


def resolve_sip_caller_for_summary(sip_phone: str) -> tuple[str, str]:
    """
    Rep-facing phone + direction for Slack summaries.
    Returns (phone, "inbound"|"outbound"). Mirrors sip_realtime inbound handling.
    """
    raw = (sip_phone or "").strip()
    if not raw:
        return "", ""

    if sip_phone_needs_outbound_lookup(raw):
        return resolve_outbound_caller_phone(raw), "outbound"

    try:
        normalized = normalize_phone_e164(raw)
    except ValueError:
        normalized = raw

    if is_active_outbound_callee(normalized):
        return normalized, "outbound"

    return normalized, "inbound"


def validate_twilio_signature(url: str, params: dict[str, str], signature: str) -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not token or not signature:
        return False
    from twilio.request_validator import RequestValidator

    return RequestValidator(token).validate(url, params, signature)


def initiate_callback(*, phone: str, consent: bool, client_ip: str = "", sip_uri: str) -> dict[str, Any]:
    if not outbound_enabled():
        raise RuntimeError("Outbound calling is not configured")
    if not sip_uri:
        raise RuntimeError("OpenAI SIP URI is not configured (set OPENAI_PROJECT_ID)")

    normalized = validate_callback_request(phone=phone, consent=consent, client_ip=client_ip)
    base = telephony_public_base_url()
    if not base:
        raise RuntimeError("TELEPHONY_PUBLIC_BASE_URL or FLY_APP_NAME is required for outbound callbacks")

    from_number = os.environ.get("DEMO_PHONE_NUMBER", "").strip()
    cid = _new_cid()
    now = time.time()
    rec = OutboundCallRecord(cid=cid, phone=normalized, created_at=now)
    _correlations[cid] = rec
    _prune_correlations(now)

    bridge_url = f"{base}/api/twilio/voice/outbound-bridge?cid={cid}"
    status_url = f"{base}/api/twilio/voice/status?cid={cid}"

    client = get_twilio_client()
    call = client.calls.create(
        to=normalized,
        from_=from_number,
        url=bridge_url,
        method="POST",
        status_callback=status_url,
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    rec.twilio_call_sid = str(call.sid or "")
    rec.status = "initiated"
    rec.updated_at = time.time()

    logger.info(
        "outbound callback initiated cid=%s call_sid=%s phone=...%s",
        cid,
        rec.twilio_call_sid,
        normalized[-4:] if len(normalized) >= 4 else "?",
    )
    return {
        "ok": True,
        "cid": cid,
        "status": rec.status,
        "call_sid": rec.twilio_call_sid,
    }


def reset_outbound_state_for_tests() -> None:
    _correlations.clear()
    _rate_phone.clear()
    _rate_ip.clear()
