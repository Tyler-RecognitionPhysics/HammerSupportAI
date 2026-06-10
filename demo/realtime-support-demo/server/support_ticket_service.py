"""Orchestrate HubSpot ticket creation, Slack alerts, and local audit storage."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from agreement_approvals import normalize_email
from hubspot_ticket_create import create_hubspot_support_ticket, hubspot_ticket_create_configured
from lead_zapier import normalize_phone_e164
from support_ticket_slack import post_new_support_ticket_alert, slack_ticket_notify_configured

_log = logging.getLogger(__name__)


def ticket_creation_enabled() -> bool:
    """Master switch for ALL support ticket creation (HubSpot + local DB + Slack).

    Set SUPPORT_ENABLE_TICKET_CREATION to 0/false/no to fully turn ticket
    creation off across the AI tool, the voice call-end flow, and the manual
    form. Defaults to enabled to preserve prior behavior.
    """
    raw = os.environ.get("SUPPORT_ENABLE_TICKET_CREATION", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True

_TICKET_RESOLVED_MESSAGE = "Thanks — I've logged this support session as resolved."
_TICKET_FOLLOWUP_MESSAGE = (
    "Thanks — I've logged your support ticket. A Hammer representative will follow up as soon as possible."
)


def ticket_success_message(resolved: bool) -> str:
    return _TICKET_RESOLVED_MESSAGE if resolved else _TICKET_FOLLOWUP_MESSAGE


@dataclass
class SupportTicketPayload:
    dealership_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    issue_summary: str
    session_id: str = ""
    channel: str = ""
    resolved: bool = False
    issue_category: str = ""


def _validate_payload(payload: SupportTicketPayload) -> str | None:
    if not payload.dealership_name.strip():
        return "dealership_name is required"
    if not payload.first_name.strip():
        return "first_name is required"
    if not payload.last_name.strip():
        return "last_name is required"
    email = normalize_email(payload.email.strip())
    if "@" not in email:
        return "valid email is required"
    # Phone is optional — never block ticket creation (or force the customer to
    # email us) just because they did not share a number. Validate only if given.
    phone_raw = payload.phone.strip()
    if phone_raw:
        phone = normalize_phone_e164(phone_raw)
        if len("".join(c for c in phone if c.isdigit())) < 10:
            return "phone number looks invalid — include country code or leave it blank"
    if not payload.issue_summary.strip():
        return "issue_summary is required"
    return None


def payload_from_dict(data: dict[str, Any], *, session: Any = None) -> SupportTicketPayload:
    channel = str(data.get("channel") or "").strip()
    session_id = str(data.get("session_id") or "").strip()
    if session is not None:
        if not channel:
            channel = str(getattr(session, "channel", "") or "").strip()
        if not session_id:
            session_id = str(getattr(session, "call_id", "") or "").strip()

    resolved_raw = data.get("resolved", False)
    if isinstance(resolved_raw, str):
        resolved = resolved_raw.strip().lower() in ("true", "yes", "1")
    else:
        resolved = bool(resolved_raw)

    return SupportTicketPayload(
        dealership_name=str(data.get("dealership_name") or data.get("dealership") or "").strip(),
        first_name=str(data.get("first_name") or "").strip(),
        last_name=str(data.get("last_name") or "").strip(),
        email=normalize_email(str(data.get("email") or "").strip()),
        phone=normalize_phone_e164(str(data.get("phone") or "").strip()),
        issue_summary=str(data.get("issue_summary") or data.get("message") or "").strip(),
        session_id=session_id,
        channel=channel,
        resolved=resolved,
        issue_category=str(data.get("issue_category") or "").strip(),
    )


async def create_and_notify_ticket(
    data: dict[str, Any] | SupportTicketPayload,
    *,
    session: Any = None,
) -> dict[str, Any]:
    """
    Idempotent ticket creation for a session.
    Returns dict with ok, message, hubspot_ticket_id, local_ticket_id, slack_posted, error.
    """
    if not ticket_creation_enabled():
        _log.info("Ticket creation skipped — SUPPORT_ENABLE_TICKET_CREATION is off")
        return {
            "ok": False,
            "disabled": True,
            "error": "Ticket creation is currently turned off.",
            "message": "Thanks — I've noted your issue. Ticket creation is currently turned off.",
        }

    payload = data if isinstance(data, SupportTicketPayload) else payload_from_dict(data, session=session)

    err = _validate_payload(payload)
    if err:
        return {"ok": False, "error": err}

    from support_dashboard_store import (
        get_ticket_for_session,
        record_support_ticket,
        update_session_ticket_state,
    )

    if payload.session_id:
        existing = get_ticket_for_session(payload.session_id)
        if existing:
            return {
                "ok": True,
                "already_exists": True,
                "message": ticket_success_message(payload.resolved),
                "hubspot_ticket_id": existing.get("hubspot_ticket_id") or "",
                "local_ticket_id": existing.get("id"),
                "ticket_url": existing.get("ticket_url") or "",
            }

    if session is not None and getattr(session, "ticket_created", False):
        hid = str(getattr(session, "hubspot_ticket_id", "") or "").strip()
        if hid or payload.session_id:
            existing = get_ticket_for_session(payload.session_id) if payload.session_id else None
            return {
                "ok": True,
                "already_exists": True,
                "message": ticket_success_message(payload.resolved),
                "hubspot_ticket_id": hid or (existing or {}).get("hubspot_ticket_id", ""),
                "local_ticket_id": (existing or {}).get("id"),
            }

    hubspot_result: dict[str, Any] = {"ok": False, "error": "HubSpot not configured"}
    if hubspot_ticket_create_configured():
        hubspot_result = await create_hubspot_support_ticket(
            dealership_name=payload.dealership_name,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            issue_summary=payload.issue_summary,
            session_id=payload.session_id,
            channel=payload.channel,
            resolved=payload.resolved,
            issue_category=payload.issue_category,
        )
    else:
        _log.warning("HubSpot ticket create skipped — disabled or not fully configured")

    hubspot_id = str(hubspot_result.get("hubspot_ticket_id") or "").strip()
    ticket_url = str(hubspot_result.get("ticket_url") or "").strip()

    if hubspot_ticket_create_configured() and not hubspot_result.get("ok"):
        return {
            "ok": False,
            "error": hubspot_result.get("error") or "HubSpot ticket creation failed",
        }

    # Fire-and-forget: the Slack round trip (~0.3-1s) must never delay the AI's
    # spoken/typed confirmation to the customer.
    slack_posted = False
    if slack_ticket_notify_configured():
        slack_posted = True
        threading.Thread(
            target=post_new_support_ticket_alert,
            kwargs=dict(
                dealership_name=payload.dealership_name,
                first_name=payload.first_name,
                last_name=payload.last_name,
                email=payload.email,
                phone=payload.phone,
                issue_summary=payload.issue_summary,
                channel=payload.channel,
                resolved=payload.resolved,
                hubspot_ticket_id=hubspot_id,
                ticket_url=ticket_url,
                session_id=payload.session_id,
            ),
            daemon=True,
        ).start()

    local = record_support_ticket(
        dealership=payload.dealership_name,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        phone=payload.phone,
        message=payload.issue_summary,
        session_id=payload.session_id,
        channel=payload.channel,
        resolved=payload.resolved,
        hubspot_ticket_id=hubspot_id,
        issue_category=payload.issue_category,
    )

    if payload.session_id:
        update_session_ticket_state(
            payload.session_id,
            ticket_created=True,
            hubspot_ticket_id=hubspot_id,
            dealership=payload.dealership_name,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            resolved=payload.resolved,
        )

    if session is not None:
        session.ticket_created = True
        session.hubspot_ticket_id = hubspot_id
        session.dealership_name = payload.dealership_name
        session.first_name = payload.first_name
        session.last_name = payload.last_name
        session.email = payload.email
        session.phone = payload.phone
        if payload.resolved:
            session.resolved = True
        if payload.issue_category:
            session.issue_category = payload.issue_category

    return {
        "ok": True,
        "message": ticket_success_message(payload.resolved),
        "hubspot_ticket_id": hubspot_id,
        "ticket_url": ticket_url,
        "local_ticket_id": local.get("ticket_id"),
        "slack_posted": slack_posted,
        "hubspot_configured": hubspot_ticket_create_configured(),
    }
