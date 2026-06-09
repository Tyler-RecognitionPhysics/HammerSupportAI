"""Create HubSpot contacts and support tickets (write API)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from hubspot_budget import HubSpotBudgetExceeded, consume as _consume_hubspot_budget

_log = logging.getLogger(__name__)

_HUBSPOT_API = "https://api.hubapi.com"


def _hubspot_token() -> str:
    return (
        os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        or os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    )


def _portal_id() -> str:
    return os.environ.get("HUBSPOT_PORTAL_ID", "3355079").strip()


def _pipeline_id() -> str:
    return os.environ.get("HUBSPOT_NEW_TICKET_PIPELINE_ID", "").strip()


def _stage_id() -> str:
    return os.environ.get("HUBSPOT_NEW_TICKET_STAGE_ID", "").strip()


def _source_property() -> str:
    return os.environ.get("HUBSPOT_TICKET_SOURCE_PROPERTY", "").strip()


def _ticket_create_enabled() -> bool:
    return os.environ.get("SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def hubspot_ticket_create_configured() -> bool:
    return bool(_ticket_create_enabled() and _hubspot_token() and _pipeline_id() and _stage_id())


def hubspot_ticket_url(ticket_id: str) -> str:
    portal = _portal_id()
    tid = str(ticket_id).strip()
    if portal and tid:
        return f"https://app.hubspot.com/contacts/{portal}/ticket/{tid}"
    return ""


async def _search_contact_by_email(
    client: httpx.AsyncClient, headers: dict[str, str], email: str
) -> str | None:
    _consume_hubspot_budget(1)
    resp = await client.post(
        f"{_HUBSPOT_API}/crm/v3/objects/contacts/search",
        headers=headers,
        json={
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "phone", "company"],
            "limit": 1,
        },
    )
    if resp.status_code >= 400:
        _log.warning("HubSpot contact search failed: %s %s", resp.status_code, resp.text[:300])
        return None
    results = resp.json().get("results") or []
    if not results:
        return None
    return str(results[0].get("id") or "").strip() or None


async def _upsert_contact(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    email: str,
    first_name: str,
    last_name: str,
    phone: str,
    dealership: str,
) -> tuple[str | None, str | None]:
    props: dict[str, str] = {
        "email": email,
        "firstname": first_name,
        "lastname": last_name,
        "phone": phone,
        "company": dealership,
    }
    contact_id = await _search_contact_by_email(client, headers, email)
    if contact_id:
        _consume_hubspot_budget(1)
        resp = await client.patch(
            f"{_HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}",
            headers=headers,
            json={"properties": {k: v for k, v in props.items() if v}},
        )
        if resp.status_code >= 400:
            return None, f"HubSpot contact update failed ({resp.status_code})"
        return contact_id, None

    _consume_hubspot_budget(1)
    resp = await client.post(
        f"{_HUBSPOT_API}/crm/v3/objects/contacts",
        headers=headers,
        json={"properties": props},
    )
    if resp.status_code >= 400:
        return None, f"HubSpot contact create failed ({resp.status_code}): {resp.text[:200]}"
    return str(resp.json().get("id") or "").strip() or None, None


async def create_hubspot_support_ticket(
    *,
    dealership_name: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    issue_summary: str,
    session_id: str = "",
    channel: str = "",
    resolved: bool = False,
    issue_category: str = "",
) -> dict[str, Any]:
    """
    Upsert contact, create ticket, associate contact to ticket.
    Returns { ok, hubspot_ticket_id, hubspot_contact_id, ticket_url, error }.
    """
    token = _hubspot_token()
    pipeline = _pipeline_id()
    stage = _stage_id()
    if not _ticket_create_enabled():
        return {"ok": False, "error": "SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE is not enabled"}
    if not token:
        return {"ok": False, "error": "HUBSPOT_PRIVATE_APP_TOKEN not configured"}
    if not pipeline or not stage:
        return {
            "ok": False,
            "error": "Set HUBSPOT_NEW_TICKET_PIPELINE_ID and HUBSPOT_NEW_TICKET_STAGE_ID",
        }

    outcome = "Resolved by AI" if resolved else "Needs support follow-up"
    subject = f"{outcome} — {dealership_name} — {first_name} {last_name}".strip()
    content_lines = [
        issue_summary.strip(),
        "",
        f"Outcome: {outcome}",
        f"Channel: {channel or 'unknown'}",
        f"Resolved by AI: {'yes' if resolved else 'no'}",
    ]
    if issue_category:
        content_lines.append(f"Category: {issue_category}")
    if session_id:
        content_lines.append(f"Session: {session_id}")
    content = "\n".join(content_lines)

    ticket_props: dict[str, str] = {
        "subject": subject[:255],
        "content": content[:65536],
        "hs_pipeline": pipeline,
        "hs_pipeline_stage": stage,
    }
    if issue_category:
        ticket_props["hs_ticket_category"] = issue_category[:100]

    source_prop = _source_property()
    if source_prop and channel:
        ticket_props[source_prop] = channel[:100]

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            contact_id, contact_err = await _upsert_contact(
                client,
                headers,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                dealership=dealership_name,
            )
            if contact_err:
                return {"ok": False, "error": contact_err}

            _consume_hubspot_budget(1)
            resp = await client.post(
                f"{_HUBSPOT_API}/crm/v3/objects/tickets",
                headers=headers,
                json={"properties": ticket_props},
            )
            if resp.status_code == 401:
                return {"ok": False, "error": "HubSpot auth failed — check private app token and scopes"}
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"HubSpot ticket create failed ({resp.status_code}): {resp.text[:300]}",
                }

            ticket_id = str(resp.json().get("id") or "").strip()
            if not ticket_id:
                return {"ok": False, "error": "HubSpot ticket create returned no id"}

            if contact_id:
                _consume_hubspot_budget(1)
                assoc = await client.put(
                    f"{_HUBSPOT_API}/crm/v3/objects/tickets/{ticket_id}/associations/contacts/{contact_id}/ticket_to_contact",
                    headers=headers,
                )
                if assoc.status_code >= 400:
                    _log.warning(
                        "HubSpot ticket-contact association failed ticket=%s contact=%s: %s",
                        ticket_id,
                        contact_id,
                        assoc.text[:200],
                    )

            return {
                "ok": True,
                "hubspot_ticket_id": ticket_id,
                "hubspot_contact_id": contact_id or "",
                "ticket_url": hubspot_ticket_url(ticket_id),
            }
    except HubSpotBudgetExceeded as exc:
        _log.warning("HubSpot ticket create skipped — daily budget reached: %s", exc)
        return {
            "ok": False,
            "budget_paused": True,
            "error": "Daily HubSpot API budget reached — ticket not created (will not retry today).",
        }
