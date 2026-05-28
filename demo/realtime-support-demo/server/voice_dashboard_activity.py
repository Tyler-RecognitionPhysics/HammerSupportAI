"""Human-readable labels for the voice admin live activity feed."""

from __future__ import annotations

import re
from typing import Any

_TOOL_CATEGORIES: dict[str, str] = {
    "capture_lead": "Agreement",
    "check_agreement_approval": "Approval",
    "open_hammer_account_form": "Account setup",
    "fill_hammer_account_field": "Account setup",
    "create_hammer_account": "Account created",
    "book_appointment": "Callback",
    "check_availability": "Calendar",
    "search_wiki": "Product info",
    "begin_hammer_signup": "Hammer handoff",
    "skip_pen_challenge": "Pen challenge",
    "set_buyer_product": "Product",
}

_SKIP_EVENT_TYPES = frozenset({"latency"})


def _short_call_id(call_id: str | None) -> str:
    cid = (call_id or "").strip()
    if not cid:
        return ""
    if len(cid) <= 10:
        return cid
    return f"…{cid[-8:]}"


def _channel_phrase(channel: str) -> str:
    ch = (channel or "").lower()
    if "phone" in ch or ch == "voice":
        return "Phone call"
    if "browser" in ch or "webrtc" in ch:
        return "Website voice"
    if "elevenlabs" in ch:
        return "Voice AI call"
    return "Voice call"


def _tone_from_preview(preview: str) -> str:
    text = (preview or "").lower()
    if any(x in text for x in ("error", "failed", "blocked", "not configured")):
        return "error"
    if any(x in text for x in ("pending", "not approved", "not available yet", "waiting")):
        return "warn"
    if any(x in text for x in ("ok", "approved", "account created", "booked", "queued", "sent")):
        return "success"
    return "neutral"


def _describe_tool(tool: str, preview: str, detail: dict[str, Any]) -> tuple[str, str]:
    """Return (title, tone)."""
    p = (preview or "").strip()
    pl = p.lower()
    email = str(detail.get("email") or "").strip()
    field = str(detail.get("field") or "").strip()
    dealer = str(detail.get("dealership_name") or "").strip()
    email_bit = f" to {email}" if email else ""
    dealer_bit = f" for {dealer}" if dealer and not email else ""

    if tool == "capture_lead":
        if "ok" in pl or "queued" in pl or "sent" in pl:
            return (f"Sent agreement email{email_bit}{dealer_bit}", "success")
        if "already sent" in pl or "already queued" in pl:
            return (f"Agreement email was already sent{email_bit}", "warn")
        return (f"Attempted to send agreement email{email_bit}", _tone_from_preview(p))

    if tool == "check_agreement_approval":
        if "approved" in pl and "not approved" not in pl[:48]:
            return ("Visitor replied I approve — agreement confirmed", "success")
        if "pending" in pl or "not approved" in pl:
            return ("Still waiting for I approve on the agreement email", "warn")
        return ("Checked whether the agreement was approved", "neutral")

    if tool == "open_hammer_account_form":
        return (f"Opened Hammer account form{email_bit}{dealer_bit}", "success")

    if tool == "fill_hammer_account_field":
        label = field.replace("_", " ") if field else "field"
        if "account created" in pl:
            return (f"Finished account fields — Hammer account created{email_bit}", "success")
        if field.lower() == "role":
            return ("Set account role silently (owner/GM)", "neutral")
        value_hint = ""
        if p.startswith("ok"):
            return (f"Saved {label} on the account form{email_bit}", "success")
        return (f"Collecting account info: {label}{email_bit}", _tone_from_preview(p))

    if tool == "create_hammer_account":
        if "account created" in pl or pl.startswith("ok"):
            return (f"Created Hammer Office account{email_bit}{dealer_bit}", "success")
        if "already created" in pl:
            return (f"Hammer account already exists{email_bit}", "warn")
        if "blocked" in pl or "not approved" in pl:
            return ("Blocked from creating account — need I approve first", "warn")
        return (f"Tried to create Hammer account{email_bit}", _tone_from_preview(p))

    if tool == "book_appointment":
        if "booked" in pl or pl.startswith("ok"):
            return (f"Booked a rep callback{email_bit}", "success")
        return ("Scheduled a callback with the sales team", _tone_from_preview(p))

    if tool == "check_availability":
        if "open" in pl or "available" in pl:
            return ("Checked calendar — slot is open", "success")
        return ("Checked rep calendar availability", "neutral")

    if tool == "search_wiki":
        q = str(detail.get("query") or "").strip()
        return (f"Looked up Hammer product info{f' ({q})' if q else ''}", "neutral")

    if tool == "begin_hammer_signup":
        return ("Moved from pen challenge into Hammer signup", "success")

    if tool == "skip_pen_challenge":
        return ("Skipped pen challenge — Hammer demo mode", "success")

    if tool == "set_buyer_product":
        product = str(detail.get("product") or detail.get("buyer_product") or "").strip()
        return (
            f"Visitor interested in {product}" if product else "Set Hammer product interest",
            "neutral",
        )

    nice = tool.replace("_", " ").strip().capitalize()
    if p:
        short = re.sub(r"\s+", " ", p)[:100]
        return (f"{nice}: {short}", _tone_from_preview(p))
    return (nice, "neutral")


def format_activity_event(event: dict[str, Any]) -> dict[str, Any]:
    """Add human-readable fields for the admin activity feed."""
    event_type = str(event.get("event_type") or "")
    if event_type in _SKIP_EVENT_TYPES:
        return {"skip": True}

    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    call_short = _short_call_id(str(event.get("call_id") or ""))
    meta_parts: list[str] = []
    if call_short:
        meta_parts.append(f"Call {call_short}")

    if event_type == "tool":
        tool = str(detail.get("tool") or "action")
        category = _TOOL_CATEGORIES.get(tool, tool.replace("_", " ").title())
        title, tone = _describe_tool(tool, str(detail.get("result_preview") or ""), detail)
        elapsed = detail.get("elapsed_ms")
        if isinstance(elapsed, int) and elapsed > 0:
            meta_parts.append(f"{elapsed} ms")
        return {
            "skip": False,
            "category": category,
            "title": title,
            "subtitle": " · ".join(meta_parts),
            "tone": tone,
        }

    if event_type == "tool_error":
        tool = str(detail.get("tool") or "Tool")
        err = str(detail.get("error") or "Unknown error")[:120]
        return {
            "skip": False,
            "category": "Error",
            "title": f"{tool.replace('_', ' ')} failed — {err}",
            "subtitle": " · ".join(meta_parts),
            "tone": "error",
        }

    if event_type == "call_summary":
        channel = _channel_phrase(str(detail.get("channel") or ""))
        flags: list[str] = []
        if detail.get("account_created"):
            flags.append("account created")
        if detail.get("agreement_email_sent"):
            flags.append("agreement sent")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        return {
            "skip": False,
            "category": "Call ended",
            "title": f"{channel} finished — Slack summary posted{flag_text}",
            "subtitle": " · ".join(meta_parts),
            "tone": "success",
        }

    if event_type == "call_started":
        scenario = str(detail.get("scenario") or "").strip()
        channel = _channel_phrase(str(detail.get("channel") or ""))
        mode = "Pen challenge" if scenario == "pen" else "Hammer sales" if scenario == "hammer" else "Voice AI"
        return {
            "skip": False,
            "category": "Call started",
            "title": f"{channel} connected — {mode} script active",
            "subtitle": " · ".join(meta_parts),
            "tone": "success",
        }

    return {
        "skip": False,
        "category": event_type.replace("_", " ").title(),
        "title": str(detail)[:120] if detail else event_type,
        "subtitle": " · ".join(meta_parts),
        "tone": "neutral",
    }


def enrich_activity_feed(events: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        fmt = format_activity_event(event)
        if fmt.get("skip"):
            continue
        out.append({**event, "activity": fmt})
        if len(out) >= limit:
            break
    return out
