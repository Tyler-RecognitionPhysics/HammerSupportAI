"""ElevenLabs post-call webhook — incomplete ticket fallback for support AI."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import HTTPException

_log = logging.getLogger(__name__)


def _verify_el_webhook_signature(raw_body: bytes, sig_header: str, secret: str) -> bool:
    try:
        parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p)}
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")
        if not timestamp or not signature:
            return False
        signed = f"{timestamp}.{raw_body.decode('utf-8', errors='replace')}"
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


async def handle_support_elevenlabs_call_end(raw_body: bytes, sig_header: str | None, event: dict) -> dict:
    """
    POST /api/elevenlabs/call-end — alert Slack if session ended without create_support_ticket.
    """
    secret = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "").strip()
    strict = os.environ.get("ELEVENLABS_WEBHOOK_STRICT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if secret:
        if not sig_header:
            msg = "support elevenlabs call-end: missing signature header"
            if strict:
                _log.warning("%s — rejecting", msg)
                raise HTTPException(401, "Missing ElevenLabs-Signature header")
            _log.warning("%s — processing anyway", msg)
        elif not _verify_el_webhook_signature(raw_body, sig_header, secret):
            msg = "support elevenlabs call-end: invalid HMAC"
            if strict:
                _log.warning(msg)
                raise HTTPException(401, "Invalid webhook signature")
            _log.warning("%s — processing anyway", msg)

    event_type = event.get("type", "")
    if event_type != "post_call_transcription":
        return {"status": "ignored", "type": event_type}

    data = event.get("data") or {}
    conv_id = str(data.get("conversation_id") or "unknown").strip()
    _log.info("support elevenlabs call-end: conversation_id=%s", conv_id)

    from support_dashboard_store import get_session, session_ticket_created
    from support_ticket_slack import post_incomplete_session_alert, slack_ticket_notify_configured

    if session_ticket_created(conv_id):
        return {"status": "ok", "ticket_created": True, "conversation_id": conv_id}

    session_data = get_session(conv_id) or {}
    channel = session_data.get("channel") or "browser_voice"
    summary = str(session_data.get("interaction_summary") or "").strip()

    slack_posted = False
    if slack_ticket_notify_configured():
        slack_posted = post_incomplete_session_alert(
            session_id=conv_id,
            channel=channel,
            interaction_summary=summary,
        )

    _log.warning(
        "support elevenlabs call-end: no ticket for conversation_id=%s slack_alert=%s",
        conv_id,
        slack_posted,
    )
    return {
        "status": "ok",
        "ticket_created": False,
        "incomplete_alert_posted": slack_posted,
        "conversation_id": conv_id,
    }
