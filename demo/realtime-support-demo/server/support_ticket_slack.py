"""Slack alerts when AI creates a support ticket."""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)


def _bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "").strip()


def _channel_id() -> str:
    return (
        os.environ.get("SUPPORT_TICKET_SLACK_CHANNEL_ID", "").strip()
        or os.environ.get("SLACK_SUPPORT_CHANNEL_ID", "").strip()
    )


def slack_ticket_notify_configured() -> bool:
    return bool(_bot_token() and _channel_id())


def post_new_support_ticket_alert(
    *,
    dealership_name: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    issue_summary: str,
    channel: str,
    resolved: bool,
    hubspot_ticket_id: str = "",
    ticket_url: str = "",
    session_id: str = "",
) -> bool:
    """Post to Slack. Returns True if posted."""
    token = _bot_token()
    channel = _channel_id()
    if not token or not channel:
        _log.debug("support ticket slack skipped: token or channel not configured")
        return False

    resolved_label = "Yes" if resolved else "No — follow-up needed"
    title = "*Resolved support session (AI)*" if resolved else "*Support follow-up needed (AI)*"
    name = f"{first_name} {last_name}".strip()
    lines = [
        title,
        f"*{dealership_name}* · {name}",
        f"{phone} · {email}",
        f"Channel: {channel or 'unknown'} · Resolved by AI: {resolved_label}",
        "",
        f"*Issue*\n{issue_summary.strip()}",
    ]
    if ticket_url:
        lines.append(f"\n<{ticket_url}|Open in HubSpot>")
    elif hubspot_ticket_id:
        lines.append(f"\nHubSpot ticket ID: {hubspot_ticket_id}")
    if session_id:
        lines.append(f"Session: `{session_id}`")

    text = "\n".join(lines)

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError

        client = WebClient(token=token)
        client.chat_postMessage(channel=channel, text=text, unfurl_links=False, unfurl_media=False)
        return True
    except SlackApiError as exc:
        _log.warning("Slack ticket alert failed: %s", exc.response.get("error", exc))
        return False
    except Exception:
        _log.exception("Slack ticket alert failed")
        return False


def post_callback_scheduled_alert(
    *,
    dealership_name: str,
    contact_name: str,
    phone: str,
    email: str,
    when_label: str,
    reason: str,
    channel: str = "",
    source: str = "ai",
) -> bool:
    """Alert the team when a customer callback/appointment is scheduled."""
    token = _bot_token()
    ch = _channel_id()
    if not token or not ch:
        return False

    who = " (manual)" if source == "manual" else " (AI)"
    lines = [
        f"*Callback requested{who}*",
        f"*{dealership_name or 'Unknown dealership'}* · {contact_name or 'Unknown'}",
        f"{phone or 'no phone'} · {email or 'no email'}",
        f"*When:* {when_label or 'unspecified'}",
    ]
    if channel:
        lines.append(f"Channel: {channel}")
    if reason.strip():
        lines.extend(["", f"*Needs help with*\n{reason.strip()[:1500]}"])

    try:
        from slack_sdk import WebClient

        WebClient(token=token).chat_postMessage(
            channel=ch, text="\n".join(lines), unfurl_links=False, unfurl_media=False
        )
        return True
    except Exception:
        _log.exception("Slack callback alert failed")
        return False


def post_incomplete_session_alert(
    *,
    session_id: str,
    channel: str,
    interaction_summary: str = "",
) -> bool:
    """Alert when a voice/chat session ended without a HubSpot ticket."""
    token = _bot_token()
    ch = _channel_id()
    if not token or not ch:
        return False

    lines = [
        "*Support session ended without ticket*",
        f"Session: `{session_id}` · Channel: {channel or 'unknown'}",
        "Required contact fields or create_support_ticket was not completed.",
    ]
    if interaction_summary.strip():
        lines.extend(["", f"*Summary*\n{interaction_summary.strip()[:1500]}"])

    try:
        from slack_sdk import WebClient

        WebClient(token=token).chat_postMessage(
            channel=ch, text="\n".join(lines), unfurl_links=False
        )
        return True
    except Exception:
        _log.exception("Slack incomplete-session alert failed")
        return False
