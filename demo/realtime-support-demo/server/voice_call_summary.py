"""End-of-call voice demo summary → Zapier Catch Hook (Slack). Ephemeral — not persisted."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from lead_zapier import (
    normalize_email,
    normalize_phone_e164,
    role_title,
    split_name,
)
from hammer_agreement import dealership_display_name, enrich_agreement_payload

_log = logging.getLogger(__name__)

_MAX_INTERACTION_SUMMARY_CHARS = 4000
_MAX_SESSION_LOG_LINES = 80
_MIN_PHONE_DIGITS = 5

_FIELD_ALIASES: dict[str, str] = {
    "dealership_name": "dealership_name",
    "dealership": "dealership_name",
    "display_name": "display_name",
    "legal_name": "legal_name",
    "selected_plan": "selected_plan",
    "lot_size": "lot_size",
    "seat_count": "seat_count",
    "business_type": "business_type",
    "cell_phone": "phone",
    "buyer_product": "product_interest",
    "product": "product_interest",
    "appointment_time": "appointment_time",
    "appointment_link": "appointment_link",
}


@dataclass
class VoiceCallLeadAccumulator:
    call_id: str = ""
    channel: str = "voice"
    call_direction: str = ""  # "inbound" | "outbound" | "" (browser/unknown)
    started_at: str = ""
    ended_at: str = ""
    values: dict[str, str] = field(default_factory=dict)
    session_log: list[str] = field(default_factory=list)
    interaction_summary: str = ""
    capture_lead_fired: bool = False
    agreement_email_sent: bool = False
    i_approve_approved: bool = False
    account_created: bool = False
    pen_challenge_skipped: bool = False
    pen_hammer_close_active: bool = False
    summary_sent: bool = False

    def touch_started(self) -> None:
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def append_log(self, line: str) -> None:
        text = " ".join((line or "").split()).strip()
        if not text:
            return
        self.session_log.append(text)
        if len(self.session_log) > _MAX_SESSION_LOG_LINES:
            self.session_log = self.session_log[-_MAX_SESSION_LOG_LINES :]

    def set_value(self, key: str, value: str | None) -> None:
        if value is None:
            return
        val = str(value).strip()
        if not val:
            return
        norm = _FIELD_ALIASES.get(key.strip().lower(), key.strip().lower())
        if norm == "email":
            val = normalize_email(val)
        if norm in ("phone", "cell_phone"):
            self.values["phone"] = val
            return
        if norm in ("display_name", "legal_name", "dealership_name"):
            self.values["dealership_name"] = val
            self.values.setdefault("display_name", val)
            self.values.setdefault("legal_name", val)
            return
        self.values[norm] = val

    def merge_from_dict(self, data: dict[str, Any]) -> None:
        for key, val in data.items():
            if val is None:
                continue
            if isinstance(val, bool):
                if key == "capture_lead_fired" and val:
                    self.capture_lead_fired = True
                    self.agreement_email_sent = True
                elif key == "agreement_email_sent" and val:
                    self.agreement_email_sent = True
                elif key == "i_approve_approved" and val:
                    self.i_approve_approved = True
                elif key == "account_created" and val:
                    self.account_created = True
                elif key == "pen_challenge_skipped" and val:
                    self.pen_challenge_skipped = True
                elif key == "pen_hammer_close_active" and val:
                    self.pen_hammer_close_active = True
                continue
            if key == "session_log" and isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        self.append_log(item)
                continue
            if key == "interaction_summary" or key == "interactionSummary":
                self.interaction_summary = str(val).strip()
                continue
            if key in ("call_id", "channel", "call_direction", "started_at", "ended_at"):
                setattr(self, key, str(val).strip())
                continue
            self.set_value(key, str(val))

    def phone_digits(self) -> str:
        return re.sub(r"\D", "", self.values.get("phone", ""))

    def has_minimum_phone(self) -> bool:
        return len(self.phone_digits()) >= _MIN_PHONE_DIGITS

    def has_actionable_contact(self) -> bool:
        """True when any contact info was captured — not phone-only."""
        if self.capture_lead_fired or self.agreement_email_sent:
            return True
        email = (self.values.get("email") or "").strip()
        if email and "@" in email:
            return True
        if self.has_minimum_phone():
            return True
        if (self.values.get("name") or "").strip():
            return True
        if (self.values.get("dealership_name") or "").strip():
            return True
        return False

    def should_post_summary(self) -> bool:
        """Whether to fire the end-of-call Zapier / Slack hook."""
        if self.has_actionable_contact():
            return True
        # ElevenLabs post-call webhook includes transcript lines even on pen-only calls.
        if self.call_id and len(self.session_log) >= 2:
            return True
        if (self.interaction_summary or "").strip():
            return True
        # Browser WebRTC: client often has no tool state (tools run server-side); still notify reps.
        if self.call_id and (self.channel or "").strip().lower() == "browser":
            return True
        return False


def zapier_voice_call_summary_webhook_url() -> str:
    return os.environ.get("ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL", "").strip()


def zapier_voice_call_summary_hook_id() -> str:
    url = zapier_voice_call_summary_webhook_url().rstrip("/")
    if not url or "/hooks/catch/" not in url:
        return ""
    return url.split("/")[-1]


def voice_call_summary_webhook_configured() -> bool:
    return bool(zapier_voice_call_summary_webhook_url())


def _bool_label(flag: bool) -> str:
    return "Yes" if flag else "No"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _parse_iso_timestamp(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _call_duration_minutes(acc: VoiceCallLeadAccumulator) -> int | None:
    start = _parse_iso_timestamp(acc.started_at)
    end = _parse_iso_timestamp(acc.ended_at)
    if not start or not end or end <= start:
        return None
    return max(1, int((end - start).total_seconds() // 60))


_HAMMER_VOICE_AI_LABEL = "Hammer Voice Ai"


def _channel_label(channel: str) -> str:
    ch = (channel or "").strip().lower()
    if ch.startswith("elevenlabs"):
        return _HAMMER_VOICE_AI_LABEL
    if ch == "phone":
        return "Phone"
    if ch == "browser":
        return "Web demo"
    return ch.title() or "Voice"


def _lot_size_ineligible(lot_size: str) -> bool:
    digits = re.sub(r"\D", "", lot_size or "")
    if not digits:
        return False
    try:
        return int(digits[:4]) <= 9
    except ValueError:
        return False


def _build_completeness_notes(acc: VoiceCallLeadAccumulator) -> str:
    """One-line status for Zapier `notes` / Slack preview text."""
    if acc.account_created:
        return "Complete signup on call"
    if _lot_size_ineligible((acc.values.get("lot_size") or "").strip()):
        return "Under 10 vehicles — not eligible"
    if acc.i_approve_approved:
        return "I approve done — account not finished on call"
    if acc.capture_lead_fired:
        return "Agreement sent — awaiting I approve"
    if acc.pen_hammer_close_active or acc.pen_challenge_skipped:
        return "Interested in Hammer — no agreement yet"
    if acc.has_actionable_contact():
        return "Partial lead — needs follow-up"
    return "Minimal capture"


def _build_rep_next_step(acc: VoiceCallLeadAccumulator) -> str:
    lot = (acc.values.get("lot_size") or "").strip()
    if _lot_size_ineligible(lot):
        return (
            "Lot under 10 — not a fit for Drive/AIA. Stay friendly; invite them back when inventory grows."
        )
    if not (acc.values.get("email") or "").strip():
        return "Call back: confirm email, product, and lot size — then send agreement."
    if not acc.capture_lead_fired:
        if acc.pen_hammer_close_active or acc.pen_challenge_skipped:
            return "Hot lead on Hammer — lock email + dealership name and send agreement today."
        return "Early disconnect — re-engage (pen challenge or Hammer discovery)."
    if not acc.i_approve_approved:
        return "Agreement is out — follow up for I approve on the agreement email thread."
    if not acc.account_created:
        return (
            "I approve confirmed — finish Hammer account (address, billing, password) or complete in Office."
        )
    return (
        "Account created — confirm Welcome email, activation link, password, and card on file if they stopped early."
    )


def _visitor_quotes(acc: VoiceCallLeadAccumulator, limit: int = 4) -> list[str]:
    seen: set[str] = set()
    quotes: list[str] = []
    for entry in acc.session_log:
        if not entry.startswith("Visitor:"):
            continue
        text = entry[8:].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        quotes.append(text)
    return quotes[-limit:]


def _summary_header(acc: VoiceCallLeadAccumulator) -> tuple[str, str]:
    """One-line Slack title + optional contact subline."""
    channel = _channel_label(acc.channel)
    direction = {"inbound": "Inbound", "outbound": "Outbound"}.get(acc.call_direction, "")
    duration = _call_duration_minutes(acc)
    dur_suffix = f"{duration} min" if duration else ""

    name = (acc.values.get("name") or "").strip()
    role_raw = (acc.values.get("role") or "").strip()
    title = role_title(role_raw) if role_raw else ""
    phone_raw = (acc.values.get("phone") or "").strip()
    phone_display = normalize_phone_e164(phone_raw) if phone_raw else ""
    email = (acc.values.get("email") or "").strip()
    website = (acc.values.get("website") or "").strip()
    dealer = (acc.values.get("dealership_name") or "").strip()
    if not dealer and website:
        dealer = dealership_display_name(website)

    who = ""
    if dealer and name:
        who = f"{dealer} · {name}" + (f" ({title})" if title else "")
    elif dealer:
        who = dealer
    elif name:
        who = name + (f" ({title})" if title else "")
    elif phone_display:
        who = phone_display
    else:
        who = "Voice call"

    meta: list[str] = [channel]
    if direction:
        meta.append(direction)
    if dur_suffix:
        meta.append(dur_suffix)
    title_line = f"*{who}* · {' · '.join(meta)}"

    contact_parts: list[str] = []
    if phone_display and (dealer or name):
        contact_parts.append(phone_display)
    if email:
        contact_parts.append(normalize_email(email))
    if website and website not in (dealer or ""):
        contact_parts.append(website)
    contact_line = " · ".join(contact_parts)
    return title_line, contact_line


def _summary_paragraph(acc: VoiceCallLeadAccumulator) -> str:
    """Short narrative paragraph (dialer-style Summary section)."""
    raw = (acc.interaction_summary or "").strip()
    if raw:
        lower = raw.lower()
        for marker in (
            "decisions and agreements",
            "decisions & agreements",
            "action items",
            "next steps",
        ):
            idx = lower.find(marker)
            if idx > 80:
                raw = raw[:idx].strip()
                break
        if lower.startswith("summary:"):
            raw = raw[8:].strip()
        para = " ".join(raw.split())
        return _truncate(para, 900)

    name = (acc.values.get("name") or "").strip()
    dealer = (acc.values.get("dealership_name") or "").strip()
    product = (acc.values.get("product_interest") or "").strip()
    plan = (acc.values.get("selected_plan") or "").strip()
    lot = (acc.values.get("lot_size") or "").strip()
    interest = product or plan

    opener = "Voice demo call"
    if name and dealer:
        opener = f"Call with {name} at {dealer}"
    elif name:
        opener = f"Call with {name}"
    elif dealer:
        opener = f"Call with {dealer}"

    details: list[str] = []
    if interest:
        details.append(f"Discussed {interest}.")
    if lot:
        details.append(f"About {lot} vehicles on the lot.")
    if acc.account_created:
        details.append("Finished Hammer account setup on the call.")
    elif acc.i_approve_approved:
        details.append("Confirmed I approve; account setup may still be open.")
    elif acc.capture_lead_fired:
        details.append("Agreement email was sent during the call.")
    elif acc.pen_hammer_close_active:
        details.append("Moved from the pen challenge into Hammer discovery.")
    elif acc.pen_challenge_skipped:
        details.append("Skipped the pen challenge and focused on Hammer.")
    else:
        quotes = _visitor_quotes(acc, limit=2)
        if quotes:
            details.append(f'Visitor noted: "{quotes[-1][:140]}".')
        else:
            details.append(_build_completeness_notes(acc) + ".")

    text = f"{opener}. " + " ".join(details)
    return _truncate(text.strip(), 900)


def _build_decisions(acc: VoiceCallLeadAccumulator) -> list[str]:
    """Dialer-style decisions / agreements — short bullets for Slack."""
    items: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> None:
        key = line.lower()
        if key not in seen:
            seen.add(key)
            items.append(line)

    if _lot_size_ineligible((acc.values.get("lot_size") or "").strip()):
        add("Under 10 vehicles — not eligible for Drive/AIA")
    if acc.account_created:
        add("Hammer account created on the call")
    elif acc.i_approve_approved:
        add("I approve confirmed on the call")
    elif acc.capture_lead_fired:
        add("Agreement email sent — awaiting I approve")
    elif acc.pen_hammer_close_active and not acc.capture_lead_fired:
        add("Interest in Hammer — no agreement sent yet")
    if (acc.values.get("appointment_time") or "").strip():
        add(f"Rep walkthrough scheduled — {acc.values.get('appointment_time', '').strip()}")
    if acc.pen_challenge_skipped:
        add("Skipped pen challenge — went straight to Hammer")

    for quote in _visitor_quotes(acc):
        ql = quote.lower()
        if any(w in ql for w in ("boss", "manager", "owner", "decision")) and any(
            w in ql for w in ("not here", "not available", "ask", "check with", "defer", "they decide")
        ):
            add("Decision deferred — needs manager or owner")
            break
        if "email" in ql and any(w in ql for w in ("send", "forward", "mail me", "email me")):
            add("Requested details by email")
            break
        if any(w in ql for w in ("not interested", "no thanks", "pass")):
            add("Not interested on this call")
            break
        if any(w in ql for w in ("call back", "callback", "later")):
            add("Asked to follow up later")
            break

    email = (acc.values.get("email") or "").strip()
    if acc.capture_lead_fired and email:
        add(f"Agreement thread started — {normalize_email(email)}")

    return items[:5]


def _build_action_items(acc: VoiceCallLeadAccumulator) -> list[str]:
    """Dialer-style next steps — one clear bullet each."""
    items: list[str] = []
    primary = _build_rep_next_step(acc).strip()
    if primary:
        items.append(primary)

    email = normalize_email(acc.values.get("email", "")) if (acc.values.get("email") or "").strip() else ""
    name = (acc.values.get("name") or "").strip()
    dealer = (acc.values.get("dealership_name") or "").strip()
    phone = normalize_phone_e164(acc.values.get("phone", "")) if acc.has_minimum_phone() else ""

    if acc.capture_lead_fired and email and not acc.i_approve_approved:
        follow = f"Follow up for I approve — {email}"
        if follow not in items:
            items.append(follow)
    if not acc.capture_lead_fired and email and (acc.pen_hammer_close_active or acc.has_actionable_contact()):
        send = f"Send agreement and product info to {email}"
        if send not in items:
            items.append(send)
    if not email and not acc.capture_lead_fired and (name or dealer or phone):
        who = name or dealer or phone
        items.append(f"Call back {who} — capture email and send agreement")

    return items[:4]


def build_interaction_summary(acc: VoiceCallLeadAccumulator) -> str:
    """Slack message body: header, Summary, Decisions, Next (dialer-style, simplified)."""
    acc.touch_started()
    title_line, contact_line = _summary_header(acc)

    lines: list[str] = [title_line]
    if contact_line:
        lines.append(contact_line)
    lines.extend(["", "*Summary*", _summary_paragraph(acc)])

    decisions = _build_decisions(acc)
    if decisions:
        lines.extend(["", "*Decisions*"])
        lines.extend(f"• {d}" for d in decisions)

    actions = _build_action_items(acc)
    if actions:
        lines.extend(["", "*Next*"])
        lines.extend(f"• {a}" for a in actions)

    return _truncate("\n".join(lines), _MAX_INTERACTION_SUMMARY_CHARS)


def build_voice_call_summary_payload(acc: VoiceCallLeadAccumulator) -> dict[str, str]:
    acc.touch_started()
    if not acc.ended_at:
        acc.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    name = (acc.values.get("name") or "").strip()
    first, last = split_name(name) if name else ("", "")
    role_raw = (acc.values.get("role") or "").strip()
    title = role_title(role_raw) if role_raw else ""

    website_raw = (acc.values.get("website") or "").strip()
    phone_raw = (acc.values.get("phone") or "").strip()
    dealer_name = (acc.values.get("dealership_name") or "").strip()
    if not dealer_name and website_raw:
        dealer_name = dealership_display_name(website_raw)
    if not dealer_name:
        dealer_name = "your dealership"

    payload: dict[str, str] = {
        "responseId": str(uuid4()),
        "createTime": acc.ended_at,
        "event": "voice_call_summary",
        "channel": _channel_label(acc.channel or "voice"),
        "channelCode": (acc.channel or "voice").strip(),
        "callDirection": acc.call_direction or "",
        "callId": acc.call_id or "",
        "callStartedAt": acc.started_at,
        "callEndedAt": acc.ended_at,
        "fullName": name,
        "firstName": first,
        "lastName": last,
        "email": normalize_email(acc.values.get("email", "")) if (acc.values.get("email") or "").strip() else "",
        "phoneNumber": normalize_phone_e164(phone_raw) if phone_raw else "",
        "website": website_raw,
        "dealership": dealer_name,
        "dealershipName": dealer_name,
        "role": role_raw,
        "roleTitle": title,
        "selectedPlan": (acc.values.get("selected_plan") or "").strip(),
        "lotSize": (acc.values.get("lot_size") or "").strip(),
        "seatCount": (acc.values.get("seat_count") or "").strip(),
        "productInterest": (acc.values.get("product_interest") or "").strip(),
        "address": (acc.values.get("address") or "").strip(),
        "businessType": (acc.values.get("business_type") or "").strip(),
        "currency": (acc.values.get("currency") or "").strip().upper(),
        "leadSource": f"voice demo call end ({acc.channel})",
        "agreementEmailSent": "true" if acc.agreement_email_sent else "false",
        "agreementApproved": "true" if acc.i_approve_approved else "false",
        "accountCreated": "true" if acc.account_created else "false",
        "captureLeadFired": "true" if acc.capture_lead_fired else "false",
        "agreementEmailSentLabel": _bool_label(acc.agreement_email_sent),
        "agreementApprovedLabel": _bool_label(acc.i_approve_approved),
        "accountCreatedLabel": _bool_label(acc.account_created),
        "captureLeadFiredLabel": _bool_label(acc.capture_lead_fired),
        "appointmentTime": (acc.values.get("appointment_time") or "").strip(),
        "appointmentLink": (acc.values.get("appointment_link") or "").strip(),
        "notes": _build_completeness_notes(acc),
        "callSummary": _summary_paragraph(acc),
        "decisionsAndAgreements": "\n".join(f"• {d}" for d in _build_decisions(acc)),
        "actionItems": "\n".join(f"• {a}" for a in _build_action_items(acc)),
        "interactionSummary": build_interaction_summary(acc),
    }
    if payload.get("email") or dealer_name:
        payload = enrich_agreement_payload(
            website=website_raw or dealer_name,
            dealership_name=dealer_name,
            selected_plan=acc.values.get("selected_plan"),
            lot_size=acc.values.get("lot_size"),
            seat_count=acc.values.get("seat_count"),
            payload=payload,
        )
    return payload


def is_hammer_account_created_result(result: str) -> bool:
    """True when a create_hammer_account tool result indicates success."""
    text = (result or "").strip().lower()
    if not text:
        return False
    if "account already created" in text:
        return True
    if "account created" in text:
        return True
    if text.startswith("ok") and not any(
        bad in text for bad in ("blocked", "failed", "error", "not available", "not configured")
    ):
        return True
    return False


def merge_tool_into_accumulator(
    acc: VoiceCallLeadAccumulator,
    tool_name: str,
    args: dict[str, Any],
    result: str = "",
) -> None:
    """Record tool activity and merge known lead fields (SIP sideband)."""
    acc.touch_started()
    if tool_name == "begin_hammer_signup":
        acc.pen_hammer_close_active = True
        product = str(args.get("buyer_product", "")).strip()
        if product:
            acc.set_value("product_interest", product)
        acc.append_log(f"Tool: begin_hammer_signup ({product or 'Hammer'})")
    elif tool_name == "skip_pen_challenge":
        acc.pen_challenge_skipped = True
        acc.pen_hammer_close_active = True
        acc.append_log("Tool: skip_pen_challenge")
    elif tool_name == "set_buyer_product":
        product = str(args.get("product", "")).strip()
        if product:
            acc.set_value("product_interest", product)
        acc.append_log(f"Tool: set_buyer_product ({product})")
    elif tool_name == "capture_lead":
        for key in (
            "email",
            "name",
            "phone",
            "website",
            "role",
            "dealership_name",
            "selected_plan",
            "lot_size",
            "seat_count",
            "currency",
        ):
            if key in args:
                acc.set_value(key, str(args.get(key, "")))
        low = (result or "").strip().lower()
        if low.startswith("ok —") or low.startswith("already sent"):
            acc.capture_lead_fired = True
            acc.agreement_email_sent = True
            acc.append_log("Tool: capture_lead (agreement email queued)")
        elif low.startswith("error") or low.startswith("warning"):
            acc.append_log(f"Tool: capture_lead failed ({str(result)[:80]})")
        else:
            acc.append_log("Tool: capture_lead")
    elif tool_name == "check_agreement_approval":
        if "approved" in (result or "").lower() and "not approved" not in (result or "").lower()[:40]:
            acc.i_approve_approved = True
            acc.append_log("Tool: check_agreement_approval (approved)")
        else:
            acc.append_log("Tool: check_agreement_approval (pending)")
    elif tool_name == "open_hammer_account_form":
        for key in ("email", "name", "dealership_name", "display_name"):
            if key in args:
                acc.set_value(key, str(args.get(key, "")))
        acc.append_log("Tool: open_hammer_account_form")
    elif tool_name == "fill_hammer_account_field":
        field_name = str(args.get("field", "")).strip()
        value = str(args.get("value", "")).strip()
        if field_name and value:
            acc.set_value(field_name, value)
            if field_name.lower() not in ("role",):
                acc.append_log(f"Tool: fill field {field_name}")
            else:
                acc.append_log("Tool: fill field role (silent)")
    elif tool_name == "create_hammer_account":
        for key, val in args.items():
            acc.set_value(str(key), str(val))
        if is_hammer_account_created_result(result):
            acc.account_created = True
        snippet = " ".join((result or "").split())[:120]
        acc.append_log(f"Tool: create_hammer_account ({snippet or 'called'})")
    elif tool_name == "search_wiki":
        q = str(args.get("query", "")).strip()
        acc.append_log(f"Tool: search_wiki ({q})" if q else "Tool: search_wiki")
    elif tool_name == "check_availability":
        when = " ".join(
            part
            for part in (
                str(args.get("date", "")).strip(),
                str(args.get("time", "")).strip(),
            )
            if part
        )
        acc.append_log(f"Tool: check_availability ({when})" if when else "Tool: check_availability")
    elif tool_name == "book_appointment":
        for key in ("email", "name", "appointment_time", "appointment_link"):
            if key in args and str(args.get(key, "")).strip():
                acc.set_value(key, str(args.get(key, "")))
        if "ok — booked" in (result or "").lower():
            for marker in ("booked ", " — calendar invite"):
                if marker in (result or ""):
                    booked = (result or "").split("booked ", 1)[-1].split(" — calendar invite", 1)[0].strip()
                    if booked:
                        acc.set_value("appointment_time", booked)
                        break
            acc.append_log("Tool: book_appointment (confirmed)")
        else:
            acc.append_log("Tool: book_appointment")
    else:
        acc.append_log(f"Tool: {tool_name}")


def merge_hammer_session_values(acc: VoiceCallLeadAccumulator, email: str) -> None:
    """Pull latest PHASE B values from Playwright session if email is known."""
    if not email.strip():
        return
    try:
        from hammer_office_session import get_session_values_for_summary

        extra = get_session_values_for_summary(email)
        for key, val in extra.items():
            acc.set_value(key, val)
    except Exception as exc:
        _log.debug("hammer session merge skipped for %s: %s", email, exc)


def post_voice_call_summary(payload: dict[str, str]) -> None:
    url = zapier_voice_call_summary_webhook_url()
    if not url:
        raise RuntimeError("ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL is not configured")
    with httpx.Client(timeout=20.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()


def maybe_post_voice_call_summary(acc: VoiceCallLeadAccumulator) -> bool:
    """Post once per call if contact info present and webhook configured. Returns True if posted."""
    if acc.summary_sent:
        return False
    if not voice_call_summary_webhook_configured():
        _log.debug("voice call summary skipped: webhook not configured")
        return False
    if not acc.should_post_summary():
        _log.info("voice call summary skipped call_id=%s: no contact or transcript", acc.call_id)
        return False

    email = (acc.values.get("email") or "").strip()
    if email:
        merge_hammer_session_values(acc, email)

    acc.summary_sent = True
    payload = build_voice_call_summary_payload(acc)
    try:
        from voice_dashboard_store import append_call_event, unregister_active_session, upsert_call_record

        if not acc.ended_at:
            acc.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        upsert_call_record(acc)
        append_call_event(
            call_id=acc.call_id,
            event_type="call_summary",
            detail={
                "channel": acc.channel,
                "posted": True,
                "account_created": bool(acc.account_created),
                "agreement_email_sent": bool(acc.agreement_email_sent or acc.capture_lead_fired),
                "i_approve_approved": bool(acc.i_approve_approved),
            },
        )
        unregister_active_session(acc.call_id)
    except Exception:
        _log.exception("voice dashboard persist failed call_id=%s", acc.call_id)
    try:
        post_voice_call_summary(payload)
        _log.info(
            "voice call summary posted call_id=%s channel=%s phone=%s",
            acc.call_id,
            acc.channel,
            payload.get("phoneNumber", "")[:6] + "…" if payload.get("phoneNumber") else "",
        )
        return True
    except Exception:
        _log.exception("voice call summary post failed call_id=%s", acc.call_id)
        acc.summary_sent = False
        return False
