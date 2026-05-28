"""Realtime voice tool schemas and server-side execution (SIP sideband)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

from hammer_office import (
    HammerAccountRequest,
    HammerOfficeError,
    address_is_hammer_placeholder,
    create_hammer_account,
    hammer_office_configured,
)
from hammer_office_session import (
    account_already_created,
    fill_hammer_account_field,
    open_hammer_account_form,
    prewarm_hammer_account_form,
    reset_voice_signup_session,
)
from agreement_approvals import (
    agreement_email_already_queued,
    ensure_voice_call_approval,
    just_replied_poll_wait_seconds,
    register_pending_agreement,
    reset_agreement_approval,
    sync_pending_agreement_to_fly,
    voice_approve_on_call_enabled,
)
from lead_zapier import (
    LeadCaptureRequest,
    agreement_approval_status,
    build_zapier_payload,
    lead_webhook_configured,
    post_lead_to_zapier,
)
from google_calendar import (
    book_appointment,
    check_availability,
    format_book_appointment_result,
    format_check_availability_result,
)
from voice_call_summary import VoiceCallLeadAccumulator, merge_tool_into_accumulator
from voice_instructions import (
    WIKI_PREFETCH_QUERIES,
    build_hammer_knowledge_handoff,
    build_hammer_signup_handoff,
    build_micro_pitch_guidance,
    prefetch_wiki_context,
)


_EMAIL_TOOLS: frozenset[str] = frozenset(
    {
        "check_agreement_approval",
        "open_hammer_account_form",
        "fill_hammer_account_field",
        "create_hammer_account",
        "book_appointment",
    }
)


def _norm_email(raw: str) -> str:
    return str(raw or "").strip().lower()


def _looks_like_spelled_out_local(local: str) -> bool:
    """Detect read-back format like t-b-e-n-n-e-t-t-6-0-2-5 (single chars joined by hyphens)."""
    local = (local or "").strip().lower()
    if "-" not in local:
        return False
    parts = [p for p in local.split("-") if p]
    if len(parts) < 4:
        return False
    single_char = sum(1 for p in parts if len(p) == 1 and p.isalnum())
    return single_char >= max(4, int(len(parts) * 0.75))


def _collapse_spelled_out_local(local: str) -> str:
    """Collapse t-b-e-n-n-e-t-t-6-0-2-5 -> tbennett6025 when pattern matches."""
    if not _looks_like_spelled_out_local(local):
        return local.strip().lower()
    return "".join(p for p in local.split("-") if p)


def _sanitize_capture_email(raw: str) -> tuple[str, str | None]:
    """Normalize email for capture_lead; return (email, warning_if_unusable)."""
    email = _norm_email(raw)
    if "@" not in email:
        return email, None
    local, _, domain = email.partition("@")
    domain = domain.strip()
    if _looks_like_spelled_out_local(local):
        collapsed = _collapse_spelled_out_local(local)
        if collapsed and collapsed != local:
            _log.info("capture_lead collapsed spelled-out email %r -> %r@%s", raw, collapsed, domain)
            email = f"{collapsed}@{domain}"
            local = collapsed
        else:
            return email, (
                "warning — email looks like a spelled-out read-back (hyphens between each letter/digit). "
                "capture_lead needs the normal email string the caller confirmed — e.g. tbennett6025@gmail.com — "
                "NOT the read-back format with hyphens. Retry capture_lead with the plain confirmed address."
            )
    return email, None


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _email_local_part_looks_real(email: str) -> bool:
    """Filter out STT fragments scraped as email candidates from the transcript.

    Real human email local parts always contain at least one letter and are
    longer than a typical year sequence. This rejects garbage like "6025@gmail.com"
    or "a@x.co" that the regex would otherwise match — when Hannah's read-back
    or the caller's spoken email gets transcribed poorly, the conversation can
    contain isolated digit fragments that look like emails.
    """
    raw = (email or "").strip().lower()
    local, _, domain = raw.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if len(local) < 3:
        return False
    if local.isdigit():
        return False
    if _looks_like_spelled_out_local(local):
        return False
    # Require at least one alphabetic character in the local part. Real human
    # email local parts almost always contain letters; pure digit strings are
    # almost always STT fragments (year-substitution misfires, partial captures).
    if not any(c.isalpha() for c in local):
        return False
    return True


_KNOWN_EMAIL_PROVIDERS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "fastmail.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "bellsouth.net",
        "cox.net",
        "charter.net",
        "earthlink.net",
        "rogers.com",
        "shaw.ca",
        "telus.net",
        "bell.net",
    }
)


def _suspicious_capture_warning(*, email: str, dealership: str) -> str:
    """Return a guidance string when capture_lead args look like a STT slip.

    These return strings are seen by the model and instruct Hannah to
    re-confirm via one-breath read-back BEFORE the Zapier email goes out.
    Empty string means "looks good, proceed".
    """
    raw_email = (email or "").strip().lower()
    if "@" not in raw_email or not raw_email:
        return (
            "warning — email is missing @ or empty. Re-ask once, read it back in "
            "one breath with NATO on confusable letters, then retry capture_lead."
        )
    local, _, domain = raw_email.partition("@")
    local = local.strip()
    domain = domain.strip()
    if not local or not domain or "." not in domain:
        return (
            "warning — email looks malformed. Re-read it back in one breath with NATO on "
            "confusable letters and confirm before retrying capture_lead."
        )
    if len(local) <= 1:
        return (
            "warning — local part is a single character; likely a STT slip. "
            "Re-read in one breath with NATO disambiguation, then retry."
        )
    if local.isdigit():
        return (
            "warning — local part is all digits; double-check the spelling once with the "
            "caller (one-breath read-back, NATO on any confusable letters) before retrying."
        )
    if _looks_like_spelled_out_local(local):
        return (
            "warning — email looks like a spelled-out read-back (hyphens between each letter/digit). "
            "capture_lead needs the normal email string the caller confirmed — e.g. tbennett6025@gmail.com — "
            "NOT the read-back format with hyphens. Retry capture_lead with the plain confirmed address."
        )
    # Year-substitution trap: STT models (Whisper, ElevenLabs Scribe) collapse spoken
    # digit strings like "six oh two five" into the nearest familiar year (e.g. "2025").
    # If the local part contains a 20XX sequence, force a re-confirm — the caller may
    # have actually said different digits. Hannah must read each digit individually.
    if re.search(r"20[12][0-9]", local):
        return (
            "warning — email local part contains a 2020-2029 year sequence (e.g. 2024, 2025, 2026). "
            "This is the single most common STT slip — speech-to-text often substitutes spoken digit "
            "strings like 'six oh two five' with the nearest familiar year. Before retrying capture_lead, "
            "you MUST re-confirm the exact digits with the caller, reading each digit individually "
            "(e.g. 'I have six, zero, two, five — that right?'). If they correct any digit, treat the "
            "whole sequence as new and re-read every digit individually. Never assume a 20XX year is correct."
        )
    if domain not in _KNOWN_EMAIL_PROVIDERS and re.search(r"(mn|nm|bd|db|pt|tp|vb|bv)", domain):
        return (
            "warning — custom domain has an ambiguous letter pair "
            "(M/N, B/D, P/T, V/B). One-breath re-read of the domain only, "
            "confirm, then retry capture_lead."
        )
    if dealership and len(dealership.strip()) <= 2:
        return (
            "warning — dealership name looks too short. Re-confirm name once, "
            "then retry capture_lead."
        )
    return ""


def _message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts).strip()
    return ""


def _infer_selected_plan(text: str) -> str:
    low = text.lower()
    if "facebook" in low or "aia" in low:
        return "Facebook AIA"
    if "marketposter" in low or "market poster" in low:
        return "MarketPoster"
    if "connect" in low:
        return "Hammer Connect"
    if "drive" in low or "hammer" in low:
        return "Hammer Drive"
    return ""


def _looks_like_dealership_answer(text: str) -> bool:
    s = text.strip()
    low = s.lower()
    if not s or len(s) > 90:
        return False
    if _EMAIL_RE.search(s):
        return False
    if low in {"yes", "yeah", "yep", "no", "nope", "ok", "okay", "done", "i approve"}:
        return False
    if "approve" in low or "@" in s or "http" in low:
        return False
    if re.fullmatch(r"\d{1,4}", s):
        return False
    return True


def _infer_lot_size(text: str) -> str:
    low = text.lower()
    match = re.search(r"\b(\d{1,4})\s*(?:cars?|vehicles?|units?|inventory|on\s+the\s+lot)?\b", low)
    return match.group(1) if match else ""


_DIGIT_WORDS: dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "to": "2", "too": "2",
    "three": "3", "four": "4", "for": "4",
    "five": "5", "six": "6", "seven": "7",
    "eight": "8", "ate": "8", "nine": "9",
}


def _spoken_digits_to_string(text: str) -> str:
    """Convert mixed words/digits into a digit string, e.g. 'nine seven three 9 0 8'."""
    out: list[str] = []
    for token in re.findall(r"[A-Za-z]+|\d", text.lower()):
        if token.isdigit():
            out.append(token)
        elif token in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[token])
    return "".join(out)


_PHONE_FORMATS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"),
    re.compile(r"\b\d{10}\b"),
    re.compile(r"\b1\s*[\-.]?\s*\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b"),
)


def _phone_digits(text: str) -> str:
    """Pull a US 10-digit phone number out of text — handles spoken digits and punctuation."""
    for pat in _PHONE_FORMATS:
        m = pat.search(text)
        if m:
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10:
                return digits
    spoken = _spoken_digits_to_string(text)
    if len(spoken) == 11 and spoken.startswith("1"):
        spoken = spoken[1:]
    if len(spoken) == 10:
        return spoken
    return ""


_WEBSITE_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.(?:com|net|org|io|co|us|ca|biz|auto|dealer|cars|app|info|store))\b",
    re.I,
)


def _infer_website(text: str) -> str:
    # Strip emails first so 'tbennett@gmail.com' doesn't yield gmail.com as a website.
    cleaned = _EMAIL_RE.sub(" ", text)
    match = _WEBSITE_RE.search(cleaned)
    if not match:
        return ""
    site = match.group(0).strip().rstrip(".,")
    if "@" in site:
        return ""
    return site


_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.\-' ]{1,80}\b(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|boulevard|blvd|way|court|ct|circle|cir|highway|hwy|trail|terrace|parkway|pkwy|place|pl|plaza|square|sq|loop|run|crossing|alley|expressway|fwy|freeway|route|rte)\b[^\n]{0,120}",
    re.I,
)
_STATE_ZIP_RE = re.compile(
    r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|Ontario|Quebec|British Columbia|Alberta|Manitoba|Saskatchewan)\b",
    re.I,
)


def _infer_address(text: str) -> str:
    """Pull a street-style address out of text."""
    s = text.strip()
    if len(s) < 8 or _EMAIL_RE.search(s):
        return ""
    match = _ADDRESS_RE.search(s)
    if match:
        return match.group(0).strip().rstrip(".,")
    # Fallback: starts with a number, contains a US state name/abbrev
    if re.search(r"^\d{1,6}\s+\w", s) and _STATE_ZIP_RE.search(s):
        return s.rstrip(".,")
    return ""


_BUSINESS_TYPE_RE = re.compile(
    r"\b(LLC|L\.L\.C\.?|Inc\.?|Incorporated|Corp\.?|Corporation|Sole Proprietor(?:ship)?|Partnership|S[- ]?Corp|C[- ]?Corp|LLP|Limited)\b",
    re.I,
)


def _infer_business_type(text: str) -> str:
    match = _BUSINESS_TYPE_RE.search(text)
    if not match:
        return ""
    return match.group(0).strip().rstrip(".")


def _scan_user_message_for_fields(
    text: str,
    previous_assistant: str,
    out: dict[str, str],
) -> None:
    """Extract account fields only when the assistant just asked for that field."""
    s = text.strip()
    if not s or not previous_assistant:
        return
    prev = previous_assistant.lower()

    if "business_type" not in out and (
        "legal structure" in prev
        or "business structure" in prev
        or "llc" in prev
        or "corporation" in prev
        or "sole propriet" in prev
        or "partnership" in prev
        or "business type" in prev
    ):
        bt = _infer_business_type(s)
        if bt:
            out["business_type"] = bt

    if "phone" not in out and (
        "phone" in prev
        or "number" in prev
        or "callback" in prev
        or "contact" in prev
        or "reach you" in prev
        or "call you" in prev
    ):
        digits = _phone_digits(s)
        if digits:
            out["phone"] = digits

    if "website" not in out and (
        "website" in prev
        or "web site" in prev
        or "your site" in prev
        or "dot com" in prev
        or "url" in prev
    ):
        site = _infer_website(s)
        if site:
            out["website"] = site

    if "address" not in out and (
        "address" in prev
        or "street" in prev
        or "where is the dealership" in prev
        or "where's the store" in prev
        or "full address" in prev
    ):
        addr = _infer_address(s)
        if addr:
            out["address"] = addr

    if "first name" in prev and "last name" not in prev:
        words = s.split()
        if 1 <= len(words) <= 3 and len(s) <= 60 and "@" not in s:
            out.setdefault("first_name", s)
    elif "last name" in prev:
        words = s.split()
        if 1 <= len(words) <= 3 and len(s) <= 60 and "@" not in s:
            out.setdefault("last_name", s)
    elif (
        "your name" in prev
        or "full name" in prev
        or "what's your name" in prev
    ):
        words = s.split()
        if 1 <= len(words) <= 4 and len(s) <= 80 and "@" not in s:
            out.setdefault("name", s)


@dataclass
class SignupContext:
    email: str = ""
    dealership_name: str = ""
    selected_plan: str = ""
    lot_size: str = ""
    account_fields: dict[str, str] = field(default_factory=dict)
    capture_lead_sent: bool = False


def derive_signup_context_from_messages(messages: list[dict]) -> SignupContext:
    """Recover signup fields + whether capture_lead succeeded from ElevenLabs history."""
    ctx = SignupContext()
    last_assistant_text = ""
    # Track which tool_call_ids belong to capture_lead so we can recognize the
    # matching tool result and detect failures (warning/error responses).
    capture_lead_call_ids: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        text = _message_text(msg)
        if text:
            email_match = _EMAIL_RE.search(text)
            if email_match:
                candidate = _norm_email(email_match.group(0))
                if _email_local_part_looks_real(candidate):
                    ctx.email = candidate
            plan = _infer_selected_plan(text)
            if plan:
                ctx.selected_plan = plan
            lot = _infer_lot_size(text)
            if lot:
                ctx.lot_size = lot
        if role == "assistant":
            if text:
                last_assistant_text = text
            for tc in msg.get("tool_calls") or []:
                fname = (tc.get("function") or {}).get("name", "")
                tc_id = str(tc.get("id") or "")
                try:
                    fargs = json.loads((tc.get("function") or {}).get("arguments", "{}") or "{}")
                except Exception:
                    fargs = {}
                if fname == "capture_lead":
                    # Optimistically mark sent; we'll revert below if the matching
                    # tool result turns out to be a warning/error.
                    ctx.capture_lead_sent = True
                    if tc_id:
                        capture_lead_call_ids.add(tc_id)
                    candidate = _norm_email(str(fargs.get("email", "") or ""))
                    if candidate and "@" in candidate and _email_local_part_looks_real(candidate):
                        ctx.email = candidate
                    dealer = str(fargs.get("dealership_name", "") or "").strip()
                    if dealer:
                        ctx.dealership_name = dealer
                    plan = str(fargs.get("selected_plan", "") or "").strip()
                    if plan:
                        ctx.selected_plan = plan
                    lot = str(fargs.get("lot_size", "") or "").strip()
                    if lot:
                        ctx.lot_size = lot
                elif fname == "fill_hammer_account_field":
                    field_name = str(fargs.get("field", "") or "").strip()
                    value = str(fargs.get("value", "") or "").strip()
                    if field_name and value:
                        ctx.account_fields[field_name] = value
        elif role == "tool":
            content = str(msg.get("content") or "")
            low = content.strip().lower()
            tc_id = str(msg.get("tool_call_id") or "")
            if "agreement email queued" in low:
                ctx.capture_lead_sent = True
            elif tc_id and tc_id in capture_lead_call_ids and (
                low.startswith("warning") or low.startswith("error")
            ):
                # capture_lead actually failed/was rejected on this attempt — revert
                # the optimistic flag so the auto-fallback knows the email never sent.
                ctx.capture_lead_sent = False
            match = re.search(r"SESSION EMAIL KEY = (\S+@\S+)", content, re.I)
            if match:
                ctx.email = _norm_email(match.group(1))
        elif role == "user" and text:
            prev = last_assistant_text.lower()
            if (
                not ctx.dealership_name
                and _looks_like_dealership_answer(text)
                and (
                    "dealership" in prev
                    or "store name" in prev
                    or "business name" in prev
                    or "company name" in prev
                    or "name of the store" in prev
                )
            ):
                ctx.dealership_name = text.strip()
            _scan_user_message_for_fields(text, last_assistant_text, ctx.account_fields)
    return ctx


def derive_agreement_email_from_messages(messages: list[dict]) -> str:
    """Recover capture_lead email from ElevenLabs message history (stateless sessions)."""
    return derive_signup_context_from_messages(messages).email


def derive_i_approve_verified_from_messages(messages: list[dict]) -> bool:
    """True once check_agreement_approval returned approved for this conversation."""
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "").strip().lower()
        if content.startswith("approved for ") and "session email key" in content:
            return True
    return False


def derive_visitor_claimed_i_approve(messages: list[dict]) -> bool:
    """Visitor said they replied/sent I approve (even if Zap has not synced yet)."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = str(msg.get("content") or "").lower()
        if re.search(r"\bi\s+approve\b", text) or "replied i approve" in text or "sent i approve" in text:
            return True
        if "resent" in text and "approve" in text:
            return True
    return False


@dataclass
class CallSession:
    voice_scenario: str = "pen"
    pen_hammer_close_active: bool = False
    pen_challenge_skipped: bool = False
    pen_buyer_product: str = ""
    agreement_email: str = ""
    agreement_dealership: str = ""
    agreement_plan: str = ""
    agreement_lot_size: str = ""
    appointment_time: str = ""
    appointment_link: str = ""
    account_fields: dict[str, str] = field(default_factory=dict)
    capture_lead_sent: bool = False
    i_approve_verified: bool = False
    capture_stt_enabled: bool = False
    wiki_context: str = ""
    opening_response_finished: bool = False
    opening_response_id: str = ""
    call_id: str = ""
    lead: VoiceCallLeadAccumulator = field(default_factory=VoiceCallLeadAccumulator)

    def is_browser_call(self) -> bool:
        return (self.lead.channel or "elevenlabs_browser") != "phone"

    def hammer_knowledge_active(self) -> bool:
        return self.voice_scenario == "hammer" or self.pen_hammer_close_active

    def apply_signup_context(self, ctx: SignupContext) -> None:
        if ctx.email:
            self.agreement_email = ctx.email
        if ctx.dealership_name:
            self.agreement_dealership = ctx.dealership_name
        if ctx.selected_plan:
            self.agreement_plan = ctx.selected_plan
        if ctx.lot_size:
            self.agreement_lot_size = ctx.lot_size
        if ctx.account_fields:
            self.account_fields.update(ctx.account_fields)
        if ctx.capture_lead_sent:
            self.capture_lead_sent = True


def _capture_lead_block_message(email: str) -> str:
    return (
        f"already sent — agreement email was already queued for {email}. "
        "Do NOT call capture_lead again. "
        "Continue PHASE A.1: ask if they received the agreement at that email. "
        "Do NOT mention a live sales rep — you handle signup on this call. "
        "If they say they never received it, re-confirm the address, then call capture_lead "
        "with resend_agreement=true."
    )


def hydrate_session_from_call_store(session: CallSession, call_id: str) -> None:
    """Restore capture_lead state from durable stores (survives serverless cold requests)."""
    if not call_id:
        return
    session.call_id = call_id
    session.lead.call_id = call_id
    try:
        from voice_dashboard_store import get_call_record_only

        record = get_call_record_only(call_id)
    except Exception:
        record = None
    if record:
        if record.get("capture_lead_fired") or record.get("agreement_email_sent"):
            session.capture_lead_sent = True
        values = record.get("values") or {}
        email = _norm_email(str(values.get("email") or ""))
        if email:
            session.agreement_email = session.agreement_email or email
        dealer = str(values.get("dealership_name") or values.get("dealership") or "").strip()
        if dealer:
            session.agreement_dealership = session.agreement_dealership or dealer
        plan = str(values.get("selected_plan") or values.get("product_interest") or "").strip()
        if plan:
            session.agreement_plan = session.agreement_plan or plan
        lot = str(values.get("lot_size") or "").strip()
        if lot:
            session.agreement_lot_size = session.agreement_lot_size or lot
    email = session.agreement_email
    if email and agreement_email_already_queued(email):
        session.capture_lead_sent = True


def _push_pending_to_fly(email: str, *, dealership: str = "", plan: str = "") -> None:
    """After capture_lead, mirror pending agreement state on Fly (durable for Vercel)."""
    try:
        from agreement_approvals import _use_fly_approval_store

        if not _use_fly_approval_store():
            return
        sync_pending_agreement_to_fly(email, dealership=dealership, selected_plan=plan)
    except Exception as exc:
        _log.debug("_push_pending_to_fly skipped for %s: %s", email, exc)


def _should_block_capture_lead_resend(
    session: CallSession,
    email: str,
    *,
    resend: bool,
) -> bool:
    if resend:
        return False
    if session.capture_lead_sent and email == _norm_email(session.agreement_email or ""):
        return True
    if agreement_email_already_queued(email):
        return True
    call_id = session.call_id or session.lead.call_id
    if not call_id:
        return False
    try:
        from voice_dashboard_store import get_call_record_only

        record = get_call_record_only(call_id)
    except Exception:
        return False
    if not record:
        return False
    if not (record.get("capture_lead_fired") or record.get("agreement_email_sent")):
        return False
    values = record.get("values") or {}
    stored_email = _norm_email(str(values.get("email") or ""))
    return not stored_email or stored_email == email


def _fn(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def pen_challenge_tool_definitions() -> list[dict[str, Any]]:
    return [
        _fn(
            "begin_hammer_signup",
            "Call silently when they concede the pen or agree to hear about Hammer. Unlocks Hammer wiki and capture_lead. After this, never mention pen price or pen sold aloud.",
            {
                "buyer_product": {
                    "type": "string",
                    "description": "Hammer product they named when conceding (Drive, Facebook AIA, MarketPoster, Connect)",
                }
            },
        ),
        _fn(
            "skip_pen_challenge",
            "Call silently with visitor_confirmed_skip=true the moment the visitor asks a substantive Hammer / products question (price, feature, integration, 'tell me about Hammer Drive', etc.) or explicitly says 'skip the pen'. The Hammer question itself IS the confirmation — do NOT ask 'are you sure?' first. After OK, never mention pen or challenge aloud unless the visitor brings it up.",
            {
                "visitor_confirmed_skip": {
                    "type": "boolean",
                    "description": "Pass true. Asking a Hammer question or saying 'skip the pen' counts as confirmation.",
                }
            },
            required=["visitor_confirmed_skip"],
        ),
        _fn(
            "set_buyer_product",
            "After pen victory when they name a Hammer product; returns micro-pitch.",
            {"product": {"type": "string", "description": "Hammer product name"}},
            required=["product"],
        ),
        _fn(
            "search_wiki",
            "LAST RESORT for Hammer facts — answer from PRODUCT CONTEXT and PRICING blocks first; only call when the specific fact the visitor asked about is absent from those blocks. Pass 3-6 keywords. During the pen phase: still allowed for drive-by Hammer questions (price/feature/integration), but always bridge back to the pen afterward.",
            {"query": {"type": "string", "description": "3-6 keywords"}},
            required=["query"],
        ),
        _fn(
            "capture_lead",
            "PHASE A — after email + dealership_name confirmed. Silent.",
            {
                "email": {"type": "string"},
                "dealership_name": {"type": "string"},
                "selected_plan": {"type": "string"},
                "lot_size": {"type": "string"},
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "website": {"type": "string"},
                "role": {"type": "string"},
                "seat_count": {"type": "string"},
                "currency": {"type": "string", "enum": ["USD", "CAD"]},
                "resend_agreement": {
                    "type": "boolean",
                    "description": "Set true ONLY when the visitor confirmed they did not receive the agreement and you re-verified their email.",
                },
            },
            required=["email", "dealership_name", "selected_plan"],
        ),
        _fn(
            "check_availability",
            "After capture_lead — check if a date/time is open for a live rep walkthrough. Call before committing to a time aloud.",
            {
                "date": {"type": "string", "description": "YYYY-MM-DD or natural (e.g. today, Thursday)"},
                "time": {"type": "string", "description": "12h or 24h (e.g. 2pm, 14:00)"},
                "timezone": {
                    "type": "string",
                    "description": "Visitor timezone (e.g. Central, ET, America/Denver). Default Central if omitted.",
                },
            },
            required=["date", "time"],
        ),
        _fn(
            "book_appointment",
            "After check_availability confirms an open slot — book the rep walkthrough and send a Google Calendar invite. Speak a warm confirmation after ok.",
            {
                "email": {"type": "string"},
                "date": {"type": "string", "description": "Same date passed to check_availability"},
                "time": {"type": "string", "description": "Same time passed to check_availability"},
                "timezone": {"type": "string"},
                "name": {"type": "string"},
                "notes": {"type": "string", "description": "Optional context from the conversation"},
            },
            required=["email", "date", "time"],
        ),
        _fn(
            "check_agreement_approval",
            "Before account submit — I approve on agreement email. Speak confirming-wait line FIRST, then just_replied true; while polling ask PHASE B questions and fill_hammer_account_field. If approved: same turn confirm I approve, open_hammer_account_form if needed, next PHASE B question (skip collected fields) — no pause after logged-in line. Ask business_type as legal structure (LLC/corporation/partnership/sole proprietorship), not dealership category. Never ask role aloud.",
            {
                "email": {"type": "string"},
                "just_replied": {"type": "boolean"},
            },
            required=["email"],
        ),
        _fn(
            "open_hammer_account_form",
            "PHASE B — instant; ask next field same turn.",
            {
                "email": {"type": "string"},
                "dealership_name": {"type": "string"},
                "display_name": {"type": "string"},
                "name": {"type": "string"},
            },
            required=["email", "dealership_name"],
        ),
        _fn(
            "fill_hammer_account_field",
            "PHASE B — one field per call. Ask first name and last name explicitly every signup (never assume Hannah); fill name as First Last after both confirmed. For business_type, collect legal structure only: LLC, corporation, partnership, or sole proprietorship — not auto/motorcycle/powersports/franchise/dealership category.",
            {
                "email": {"type": "string"},
                "field": {"type": "string"},
                "value": {"type": "string"},
            },
            required=["email", "field", "value"],
        ),
        _fn(
            "create_hammer_account",
            "Only if incremental fill never ran. Speak one short creating-account line before calling. Prefer fill path.",
            {
                "email": {"type": "string"},
                "name": {"type": "string"},
                "legal_name": {"type": "string"},
                "display_name": {"type": "string"},
                "business_type": {
                    "type": "string",
                    "description": "Legal business structure only, e.g. LLC, Corporation, Partnership, Sole Proprietorship. Not dealership category such as auto, motorcycle, powersports, franchise, independent, or dealer.",
                },
                "phone": {"type": "string"},
                "cell_phone": {"type": "string"},
                "website": {"type": "string"},
                "address": {"type": "string"},
                "currency": {"type": "string", "enum": ["USD", "CAD"]},
                "dealership_name": {"type": "string"},
                "role": {"type": "string"},
                "selected_plan": {"type": "string"},
                "gst_hst": {"type": "string"},
                "qst": {"type": "string"},
            },
            required=[
                "email",
                "name",
                "business_type",
                "phone",
                "website",
                "address",
                "dealership_name",
            ],
        ),
    ]


def realtime_tools_to_chat_completions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Realtime API tool defs (flat name/desc/parameters) to Chat Completions format."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("function"):
            out.append(tool)
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {"type": "object", "properties": {}, "required": []},
                    ),
                },
            }
        )
    return out


def pen_challenge_chat_tool_definitions() -> list[dict[str, Any]]:
    """Tool schemas for OpenAI Chat Completions (ElevenLabs custom LLM path)."""
    return realtime_tools_to_chat_completions(pen_challenge_tool_definitions())


_PEN_ONLY_TOOLS = frozenset({"begin_hammer_signup", "skip_pen_challenge", "set_buyer_product"})


def hammer_browser_tool_definitions() -> list[dict[str, Any]]:
    """Browser Hammer demo — signup tools only (no pen-challenge phase tools)."""
    tools: list[dict[str, Any]] = []
    for tool in pen_challenge_tool_definitions():
        name = tool["name"]
        if name in _PEN_ONLY_TOOLS:
            continue
        if name == "search_wiki":
            tools.append(
                _fn(
                    "search_wiki",
                    "LAST RESORT for Hammer facts — answer from PRODUCT CONTEXT block first; only call when the specific fact is absent from PRODUCT CONTEXT. Pass 3-6 keywords.",
                    {"query": {"type": "string", "description": "3-6 keywords"}},
                    required=["query"],
                )
            )
            continue
        if name == "check_availability":
            tools.append(
                _fn(
                    "check_availability",
                    "ONLY when the visitor explicitly asks for a live rep to call them back — check if a date/time is open before committing aloud. Do NOT use during normal self-serve signup.",
                    {
                        "date": {"type": "string", "description": "YYYY-MM-DD or natural (e.g. today, Thursday)"},
                        "time": {"type": "string", "description": "12h or 24h (e.g. 2pm, 14:00)"},
                        "timezone": {
                            "type": "string",
                            "description": "Visitor timezone (e.g. Central, ET, America/Denver). Default Central if omitted.",
                        },
                    },
                    required=["date", "time"],
                )
            )
            continue
        if name == "book_appointment":
            tools.append(
                _fn(
                    "book_appointment",
                    "ONLY when the visitor explicitly asked for a rep callback — book after check_availability confirms an open slot. Do NOT use during normal self-serve signup.",
                    {
                        "email": {"type": "string"},
                        "date": {"type": "string", "description": "Same date passed to check_availability"},
                        "time": {"type": "string", "description": "Same time passed to check_availability"},
                        "timezone": {"type": "string"},
                        "name": {"type": "string"},
                        "notes": {"type": "string", "description": "Optional context from the conversation"},
                    },
                    required=["email", "date", "time"],
                )
            )
            continue
        if name == "capture_lead":
            tools.append(
                _fn(
                    "capture_lead",
                    "PHASE A — after email + dealership_name confirmed. Silent. You handle full signup on this call — do NOT mention a live rep.",
                    {
                        "email": {"type": "string"},
                        "dealership_name": {"type": "string"},
                        "selected_plan": {"type": "string"},
                        "lot_size": {"type": "string"},
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "website": {"type": "string"},
                        "role": {"type": "string"},
                        "seat_count": {"type": "string"},
                        "currency": {"type": "string", "enum": ["USD", "CAD"]},
                        "resend_agreement": {
                            "type": "boolean",
                            "description": "Set true ONLY when the visitor confirmed they did not receive the agreement and you re-verified their email.",
                        },
                    },
                    required=["email", "dealership_name", "selected_plan"],
                )
            )
            continue
        tools.append(tool)
    return tools


def hammer_browser_chat_tool_definitions() -> list[dict[str, Any]]:
    return realtime_tools_to_chat_completions(hammer_browser_tool_definitions())


def _env_top_k(default: int = 6) -> int:
    try:
        return max(1, min(12, int(os.environ.get("REALTIME_SALES_TOP_K", str(default)))))
    except ValueError:
        return default


def _env_max_wiki_chars(default: int = 4500) -> int:
    try:
        return max(800, min(12000, int(os.environ.get("REALTIME_SALES_TOOL_MAX_CHARS", str(default)))))
    except ValueError:
        return default


class VoiceToolExecutor:
    def __init__(
        self,
        get_retriever: Callable[[], Any],
        *,
        max_wiki_chars: int | None = None,
        top_k: int | None = None,
    ) -> None:
        self._get_retriever = get_retriever
        self._max_wiki_chars = max_wiki_chars if max_wiki_chars is not None else _env_max_wiki_chars()
        self._top_k = top_k if top_k is not None else _env_top_k()
        self._prefetched_wiki: str | None = None
        self._search_wiki_cache: dict[str, str] = {}

    def warm_wiki_context(self, queries: list[str] | None = None) -> str:
        """Prefetch wiki into memory so Hammer-mode tools do not block the first lookup."""
        if self._prefetched_wiki is not None:
            return self._prefetched_wiki
        started_at = time.perf_counter()
        self._prefetched_wiki = prefetch_wiki_context(
            self._get_retriever(),
            queries or WIKI_PREFETCH_QUERIES,
            max_chars=self._max_wiki_chars,
        )
        _log.info(
            "wiki_context warmed chars=%s elapsed_ms=%s",
            len(self._prefetched_wiki),
            int((time.perf_counter() - started_at) * 1000),
        )
        return self._prefetched_wiki

    def ensure_wiki_context(self, session: CallSession) -> str:
        if session.wiki_context:
            return session.wiki_context
        if self._prefetched_wiki:
            session.wiki_context = self._prefetched_wiki
            return session.wiki_context
        session.wiki_context = prefetch_wiki_context(
            self._get_retriever(), WIKI_PREFETCH_QUERIES, max_chars=self._max_wiki_chars
        )
        return session.wiki_context

    def prefetched_wiki_context(self) -> str | None:
        return self._prefetched_wiki

    def execute(self, session: CallSession, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            _log.warning("tool unknown: %s args=%s", name, arguments)
            return f"unknown tool: {name}"
        arguments = dict(arguments)
        # Important: do NOT pre-populate session.agreement_email from capture_lead args here.
        # The _tool_capture_lead handler validates the email via _suspicious_capture_warning
        # and Pydantic BEFORE accepting it. Setting session.agreement_email up here would
        # cause the auto-capture safety net to later re-fire with an email that capture_lead
        # itself already rejected (e.g. STT-corrupted "6025@gmail.com"), producing the
        # "agreement email was never queued" symptom while leaving session state dirty.
        if name in _EMAIL_TOOLS and session.agreement_email and name != "capture_lead":
            offered = _norm_email(str(arguments.get("email", "") or ""))
            if offered and offered != session.agreement_email:
                _log.warning(
                    "%s email %r corrected to session email %r",
                    name,
                    offered,
                    session.agreement_email,
                )
            arguments["email"] = session.agreement_email
        started_at = time.perf_counter()
        _log.info("tool start: %s args=%s", name, {k: v for k, v in arguments.items() if k != "value" or len(str(v)) < 80})
        try:
            result = handler(session, arguments)
            merge_tool_into_accumulator(session.lead, name, arguments, result)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            _log.info(
                "tool done: %s elapsed_ms=%s -> %s",
                name,
                elapsed_ms,
                str(result)[:120],
            )
            try:
                from voice_dashboard_store import append_call_event, session_log_to_transcript, update_active_session, upsert_call_record

                call_id = session.call_id or session.lead.call_id
                if call_id:
                    session.lead.call_id = call_id
                    upsert_call_record(session.lead)
                    update_active_session(
                        call_id,
                        {"transcript": session_log_to_transcript(session.lead.session_log or [])},
                    )
                append_call_event(
                    call_id=call_id,
                    event_type="tool",
                    detail={
                        "tool": name,
                        "elapsed_ms": elapsed_ms,
                        "result_preview": str(result)[:200],
                        **{
                            k: str(arguments[k]).strip()
                            for k in (
                                "email",
                                "field",
                                "dealership_name",
                                "product",
                                "buyer_product",
                                "query",
                            )
                            if str(arguments.get(k, "") or "").strip()
                        },
                    },
                )
            except Exception:
                pass
            return result
        except Exception as exc:
            _log.exception("tool error: %s args=%s", name, arguments)
            try:
                from voice_dashboard_store import append_call_event

                append_call_event(
                    call_id=session.call_id or session.lead.call_id,
                    event_type="tool_error",
                    detail={"tool": name, "error": str(exc)[:200]},
                )
            except Exception:
                pass
            return f"error — {type(exc).__name__}: {str(exc)[:200]}"

    def _tool_search_wiki(self, session: CallSession, args: dict[str, Any]) -> str:
        # Available in every phase — pen-phase callers regularly probe Hammer
        # ("how much is Drive?", "do you integrate with my CRM?") and dodging
        # those questions breaks the live demo. The pen prompt enforces the
        # answer-then-bridge-back-to-pen pattern; this tool just supplies facts.
        q = str(args.get("query", "")).strip()
        if not q:
            return "No query provided."
        cache_key = re.sub(r"\s+", " ", q.lower()).strip()
        cached = self._search_wiki_cache.get(cache_key)
        if cached is not None:
            _log.info("search_wiki cache hit query=%r chars=%s", q[:80], len(cached))
            return cached
        started_at = time.perf_counter()
        retriever = self._get_retriever()
        pairs = retriever.top_k(q, k=self._top_k)
        if not pairs:
            return "No strong match — use only general dealership language or offer a live rep; do not mention lookup."
        lines = [
            f"[{c.doc_id}#{c.chunk_id} score={round(float(s), 4)}] {re_sub_ws(c.text)}"
            for c, s in pairs
        ]
        result = "\n---\n".join(lines)
        if len(self._search_wiki_cache) < 256:
            self._search_wiki_cache[cache_key] = result
        _log.info(
            "search_wiki cache miss query=%r hits=%s elapsed_ms=%s",
            q[:80],
            len(pairs),
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    def _tool_begin_hammer_signup(self, session: CallSession, args: dict[str, Any]) -> str:
        session.pen_hammer_close_active = True
        session.pen_challenge_skipped = False
        session.pen_buyer_product = str(args.get("buyer_product", "") or "").strip()
        wiki = self.ensure_wiki_context(session)
        return build_hammer_signup_handoff(
            session.pen_buyer_product,
            wiki,
            awaiting_hammer_product=not session.pen_buyer_product,
        )

    def _tool_skip_pen_challenge(self, session: CallSession, args: dict[str, Any]) -> str:
        if not args.get("visitor_confirmed_skip"):
            # Do not bounce back asking the AI to confirm with the user — that
            # creates friction the visitor never asked for. Re-issue the call
            # with the flag set on your next tool turn.
            return (
                "Pass visitor_confirmed_skip=true on this call. A Hammer / products question or "
                "an explicit skip request counts as confirmation — do not ask the visitor 'are you sure?'."
            )
        session.pen_hammer_close_active = True
        session.pen_challenge_skipped = True
        session.pen_buyer_product = ""
        wiki = self.ensure_wiki_context(session)
        return build_hammer_knowledge_handoff(wiki)

    def _tool_set_buyer_product(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.pen_hammer_close_active:
            return "Call begin_hammer_signup or skip_pen_challenge first."
        product = str(args.get("product", "")).strip()
        session.pen_buyer_product = product
        if session.pen_challenge_skipped:
            return (
                f"Product noted: {session.pen_buyer_product}. Answer their Hammer question or move to signup discovery — "
                "micro-pitch optional unless they are committing to buy."
            )
        return build_micro_pitch_guidance(session.pen_buyer_product)

    def _tool_capture_lead(self, session: CallSession, args: dict[str, Any]) -> str:
        resend = bool(args.get("resend_agreement") or args.get("force_resend"))
        if session.capture_lead_sent and not resend:
            new_email = _norm_email(str(args.get("email", "") or ""))
            if not new_email or new_email == session.agreement_email:
                return _capture_lead_block_message(session.agreement_email or new_email)

        if not session.hammer_knowledge_active():
            # Model reached signup — unlock Hammer tools instead of blocking the agreement email.
            session.pen_hammer_close_active = True
        body = {k: v for k, v in args.items() if v is not None and str(v).strip() != ""}
        body["channel"] = "voice"
        raw_email = str(body.get("email", "") or "")
        sanitized_email, spell_warning = _sanitize_capture_email(raw_email)
        if spell_warning:
            return spell_warning
        if sanitized_email:
            body["email"] = sanitized_email
        suspicion = _suspicious_capture_warning(
            email=str(body.get("email", "") or ""),
            dealership=str(body.get("dealership_name", "") or ""),
        )
        if suspicion:
            return suspicion
        try:
            req = LeadCaptureRequest.model_validate(body)
        except Exception as exc:
            return f"error — {str(exc)[:240]}"
        if not lead_webhook_configured("voice"):
            return "error — ZAPIER_LEAD_WEBHOOK_URL not configured on server"
        email = req.email.strip().lower()
        if _should_block_capture_lead_resend(session, email, resend=resend):
            created, _ = account_already_created(email)
            browser_retest = session.voice_scenario == "hammer" and session.is_browser_call() and not created
            if browser_retest:
                dealership = (req.dealership_name or session.agreement_dealership or "").strip()
                reset_agreement_approval(email)
                reset_voice_signup_session(email, dealership_name=dealership)
                session.i_approve_verified = False
                _log.info(
                    "browser retest: reset approval + signup for %s — sending fresh agreement",
                    email,
                )
            else:
                session.capture_lead_sent = True
                session.agreement_email = email
                try:
                    reset_voice_signup_session(
                        email,
                        dealership_name=(session.agreement_dealership or req.dealership_name or "").strip(),
                    )
                except Exception as exc:
                    _log.warning("reset_voice_signup_session on capture_lead block failed for %s: %s", email, exc)
                return _capture_lead_block_message(email)
        prior_email = _norm_email(session.agreement_email or "")
        if resend or (prior_email and prior_email != email):
            if reset_agreement_approval(email):
                _log.info("capture_lead reset prior approval state for %s before re-sending", email)
        payload = build_zapier_payload(req)
        if str(payload.get("event", "")) == "agreement_email_request" and not (
            payload.get("productLine")
            and payload.get("agreementEmailSubject")
            and payload.get("agreementEmailHtml")
        ):
            plan = (req.selected_plan or "").strip()
            return (
                "error — agreement email template could not be resolved for selected_plan="
                f"{plan!r}. Do NOT say the agreement was sent. Ask one clarifying question: "
                "which product are they signing up for — Hammer Drive, Facebook A-I-A, "
                "MarketPoster, or Hammer Connect — then retry capture_lead."
            )
        dealership = (req.dealership_name or "").strip()
        session.agreement_email = email
        session.agreement_dealership = dealership
        session.agreement_plan = (req.selected_plan or "").strip()
        session.agreement_lot_size = (req.lot_size or "").strip()

        try:
            post_lead_to_zapier(payload)
        except Exception as exc:
            _log.exception("capture_lead Zapier delivery failed for %s", email)
            return f"error — {str(exc)[:240]}"
        session.capture_lead_sent = True
        # Register in the local approval store and push to Fly so that
        # agreement_email_already_queued() returns True on subsequent requests
        # (even from a different serverless instance that has no in-memory state).
        try:
            register_pending_agreement(
                email,
                dealership=dealership,
                product_line=(req.selected_plan or "").strip(),
                selected_plan=(req.selected_plan or "").strip(),
            )
        except Exception as exc:
            _log.warning("register_pending_agreement failed for %s: %s", email, exc)
        _push_pending_to_fly(email, dealership=dealership, plan=(req.selected_plan or "").strip())
        if email and dealership:
            try:
                prewarm_hammer_account_form(email, dealership_name=dealership)
            except Exception as exc:
                _log.warning("prewarm after capture_lead failed for %s: %s", email, exc)
        if str(payload.get("event", "")) == "agreement_email_request":
            if session.voice_scenario == "hammer" or session.is_browser_call():
                # Browser/site demo — Hannah handles the full signup herself.
                # Do NOT mention a live rep; proceed directly to account creation.
                return (
                    f"ok — agreement email queued for {email}; "
                    f"SESSION EMAIL KEY = {email} — use this exact email for every tool call on this call. "
                    "Next (PHASE A.1): in the same turn, ask 'Got the agreement at that same email?' — "
                    "nothing else on this turn. "
                    "When they confirm receipt, tell them to reply I approve — do NOT say a live rep will reach out. "
                    "The moment they say they replied, speak the confirming-wait line first, "
                    "then call check_agreement_approval with just_replied=true while asking PHASE B questions. "
                    "When approved, open_hammer_account_form (if not already open), collect remaining PHASE B "
                    "fields one per turn, fill_hammer_account_field after each, then create the account on this call. "
                    "Do NOT mention a live sales rep handling signup — you finish everything on this call. "
                    "Only mention a live rep if the caller explicitly asks for one."
                )
            # Pen challenge path — hand off to a live rep after I approve.
            return (
                f"ok — agreement email queued for {email}; "
                f"SESSION EMAIL KEY = {email} — use this exact email for every tool call on this call. "
                "Next: confirm the agreement email is on its way, tell them to review it and reply I approve if interested, "
                "and explain a live sales rep will finish signup and walk them through the dashboard. "
                "Then ask when works best for that rep walkthrough — check_availability before committing, "
                "then book_appointment to send a calendar invite. "
                "Do not call check_agreement_approval, open_hammer_account_form, fill_hammer_account_field, or create_hammer_account on this call."
            )
        return "ok — lead sent to Zapier"

    def _tool_check_availability(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.capture_lead_sent and not session.agreement_email:
            return "blocked — call capture_lead first before scheduling"
        result = check_availability(
            str(args.get("date", "") or ""),
            str(args.get("time", "") or ""),
            str(args.get("timezone", "") or "") or None,
        )
        return format_check_availability_result(result)

    def _tool_book_appointment(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.capture_lead_sent and not session.agreement_email:
            return "blocked — call capture_lead first before booking"
        email = _norm_email(str(args.get("email", "") or session.agreement_email or ""))
        if not email:
            return "error — email is required for book_appointment"
        result = book_appointment(
            email=email,
            date_str=str(args.get("date", "") or ""),
            time_str=str(args.get("time", "") or ""),
            timezone_str=str(args.get("timezone", "") or "") or None,
            name=str(args.get("name", "") or session.account_fields.get("name", "") or ""),
            dealership_name=session.agreement_dealership,
            selected_plan=session.agreement_plan or session.pen_buyer_product,
            notes=str(args.get("notes", "") or ""),
        )
        if result.get("booked"):
            session.appointment_time = str(result.get("display", "") or "")
            session.appointment_link = str(result.get("event_link", "") or "")
            if session.appointment_time:
                session.lead.set_value("appointment_time", session.appointment_time)
            if session.appointment_link:
                session.lead.set_value("appointment_link", session.appointment_link)
        return format_book_appointment_result(result)

    def _maybe_auto_capture_lead(self, session: CallSession) -> str | None:
        """Send agreement email if the model skipped capture_lead but we have signup context."""
        if session.capture_lead_sent:
            return None
        if not (session.agreement_email and session.agreement_dealership):
            return None
        if agreement_email_already_queued(session.agreement_email):
            session.capture_lead_sent = True
            return None
        selected_plan = session.agreement_plan or session.pen_buyer_product or "Hammer Drive"
        _log.warning(
            "auto capture_lead for %s — agreement email was never queued",
            session.agreement_email,
        )
        return self._tool_capture_lead(
            session,
            {
                "email": session.agreement_email,
                "dealership_name": session.agreement_dealership,
                "selected_plan": selected_plan,
                "lot_size": session.agreement_lot_size or "10",
            },
        )

    def ensure_agreement_email_queued(
        self,
        session: CallSession,
        trigger_text: str = "",
    ) -> str | None:
        """Deterministic safety net when the model talks past capture_lead."""
        if session.capture_lead_sent:
            return None
        if session.agreement_email and agreement_email_already_queued(session.agreement_email):
            session.capture_lead_sent = True
            return None
        low = trigger_text.lower()
        agreement_intent = (
            "agreement" in low
            or "i approve" in low
            or "reply approve" in low
            or "reply with approve" in low
            or "email" in low and ("received" in low or "sent" in low or "inbox" in low)
        )
        if not agreement_intent:
            return None
        auto = self._maybe_auto_capture_lead(session)
        if auto:
            return auto
        missing = []
        if not session.agreement_email:
            missing.append("email")
        if not session.agreement_dealership:
            missing.append("dealership_name")
        if missing:
            _log.warning(
                "agreement email not queued; missing signup context: %s",
                ", ".join(missing),
            )
        return None

    def ensure_account_fields_recorded(
        self,
        session: CallSession,
        trigger_text: str = "",
    ) -> str | None:
        """Safety net when the model collects PHASE B fields aloud but skips fill tools."""
        if not session.hammer_knowledge_active():
            return None
        email = _norm_email(session.agreement_email or "")
        if not email or not session.account_fields:
            return None
        from agreement_approvals import agreement_approval_status
        from hammer_office_session import (
            account_already_created,
            fill_hammer_account_field,
            get_phase_b_missing_fields,
            open_hammer_account_form,
        )

        if account_already_created(email)[0]:
            return None
        status = agreement_approval_status(email, wait_seconds=0)
        if not status.get("approved"):
            if voice_approve_on_call_enabled() and status.get("pending"):
                status = ensure_voice_call_approval(email)
        if not status.get("approved"):
            return None

        low = trigger_text.lower()
        creating = "creat" in low and "account" in low
        if not creating and get_phase_b_missing_fields(email):
            return None

        try:
            open_hammer_account_form(
                email,
                dealership_name=session.agreement_dealership or "",
            )
        except Exception as exc:
            _log.debug("auto open form skipped for %s: %s", email, exc)

        fill_order = (
            "last_name",
            "first_name",
            "name",
            "business_type",
            "phone",
            "website",
            "address",
        )
        last_result: dict[str, Any] | None = None
        for field in fill_order:
            value = str(session.account_fields.get(field, "") or "").strip()
            if not value:
                continue
            try:
                last_result = fill_hammer_account_field(email, field, value)
            except HammerOfficeError as exc:
                _log.warning("auto fill %s for %s failed: %s", field, email, exc)
                continue
            if last_result.get("account_created"):
                return (
                    "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived — "
                    "do not mention activate, password, or card yet"
                )

        if creating and not get_phase_b_missing_fields(email):
            try:
                stored = last_result or {}
                req_args: dict[str, Any] = {"email": email}
                from hammer_office_session import get_session_values

                req_args.update(get_session_values(email))
                if session.agreement_dealership:
                    req_args.setdefault("dealership_name", session.agreement_dealership)
                return self._tool_create_hammer_account(session, req_args)
            except Exception as exc:
                _log.warning("auto create_hammer_account for %s failed: %s", email, exc)
        return None

    def _tool_check_agreement_approval(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.hammer_knowledge_active():
            session.pen_hammer_close_active = True
        email = str(args.get("email", "")).strip()
        auto = self._maybe_auto_capture_lead(session)
        if auto is not None:
            if auto.startswith("ok — agreement email queued"):
                return (
                    f"{auto} — agreement email was missing and is now queued; "
                    "ask if they received it before checking I approve"
                )
            if auto.startswith("error"):
                return (
                    f"{auto} — agreement email never sent; fix fields and call capture_lead "
                    "before asking about receipt or I approve"
                )
        # Cap the blocking poll at 2 s on the voice path — Zapier writes to the approval store
        # asynchronously, so a long synchronous wait just creates dead air on the call.
        # The AI is instructed to ask PHASE B questions while re-checking; it will see the
        # approval on the next call_agreement_approval invocation.
        if args.get("just_replied"):
            from agreement_approvals import _use_fly_approval_store

            # Browser on Vercel: poll Fly once for full Gmail→Zap window (Zap writes there).
            # Phone on Fly: short local poll to avoid dead air; AI re-checks on next turn.
            max_wait = (
                just_replied_poll_wait_seconds()
                if _use_fly_approval_store()
                else min(just_replied_poll_wait_seconds(), 2)
            )
        else:
            max_wait = 0
        status = agreement_approval_status(email, wait_seconds=max_wait)
        if not status.get("approved") and args.get("just_replied") and status.get("pending"):
            status = ensure_voice_call_approval(email)
        if not status.get("approved") and session.i_approve_verified and status.get("pending"):
            status = ensure_voice_call_approval(email)
        if not status.get("approved") and not status.get("pending") and not session.capture_lead_sent:
            return (
                "blocked — agreement email was NEVER sent — call capture_lead immediately with "
                "email, dealership_name, and selected_plan before asking if they received it or checking I approve"
            )
        if status.get("approved"):
            session.i_approve_verified = True
            from hammer_office_session import (
                clear_signup_submission_state,
                get_phase_b_missing_fields,
                signup_ready_for_phase_c,
            )

            missing = get_phase_b_missing_fields(email)
            if missing:
                if not signup_ready_for_phase_c(email):
                    clear_signup_submission_state(email)
                next_field = missing[0]
                return (
                    f"approved for {email} — SESSION EMAIL KEY = {email} — "
                    "PHASE A.4 same turn: confirm I approve on agreement email, "
                    f"open_hammer_account_form(email={email}) silently if needed, "
                    f"then ask the next PHASE B question aloud: **{next_field}** — "
                    f"still need: {', '.join(missing)}. "
                    "Do NOT mention Welcome to Hammer, Activate, password, or card until "
                    "fill_hammer_account_field returns account created. "
                    "Do NOT mention a live sales rep — you create the account on this call."
                )
            if signup_ready_for_phase_c(email):
                return (
                    f"approved for {email} — account already created — "
                    "PHASE C.1 only: ask if Welcome to Hammer email arrived; "
                    "do not ask more account fields or mention activate/password/card yet"
                )
            return (
                f"approved for {email} — SESSION EMAIL KEY = {email} — "
                "PHASE A.4 same turn: confirm I approve on agreement email (even if account "
                f"questions already asked), open_hammer_account_form(email={email}) silently if needed, next PHASE B question "
                "(first name if missing, else last name, else legal business structure, etc.; never assume caller first name is Hannah). "
                "Do not end turn after logged-in transition only. "
                "Never ask role aloud. Do NOT mention a live sales rep — you finish signup on this call."
            )
        if args.get("just_replied"):
            if session.i_approve_verified:
                return (
                    f"approved for {email} — I approve already verified on this call — "
                    "do NOT say approval was not received; continue account creation or PHASE B. "
                    "Never re-ask fields already collected in this conversation."
                )
            return (
                "not approved yet after polling — keep asking next PHASE B question and "
                "fill_hammer_account_field while syncing; optional one short syncing line, then "
                "check_agreement_approval again with just_replied true once more. "
                "If still not approved: confirm they replied I approve to the agreement email thread "
                "(not only on this call); do not confirm email I approve or mention password yet; "
                "ask business_type as legal business structure only; never dead air"
            )
        if session.i_approve_verified:
            return (
                f"approved for {email} — I approve already verified on this call — "
                "continue PHASE B or create account; do NOT say approval was not received; "
                "never re-ask fields already answered on this call"
            )
        return (
            "not approved yet — one line only: reply I approve to the agreement email; "
            "when they say they did, speak confirming-wait line first then check — "
            "while polling ask PHASE B questions and fill fields; "
            "do not confirm email I approve or mention password yet; business_type means legal structure"
        )

    def _tool_open_hammer_account_form(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.hammer_knowledge_active():
            return "Not available yet."
        if not hammer_office_configured():
            return (
                "error — Hammer Office credentials not configured on this server; "
                "do not say rep will reach out — ask caller to hold one moment while the system connects"
            )
        try:
            open_hammer_account_form(
                str(args["email"]).strip(),
                dealership_name=str(args["dealership_name"]).strip(),
                display_name=str(args.get("display_name", "") or "").strip(),
                name=str(args.get("name", "") or "").strip(),
            )
        except HammerOfficeError as exc:
            return (
                f"form open failed — {str(exc)[:180]}; "
                "retry open_hammer_account_form once; if it fails again call create_hammer_account with all collected fields"
            )
        return (
            "ok — ask first name (always) if not collected yet, else last name, else next PHASE B field "
            "(full name, legal business structure, phone, website, full address — one per turn); "
            "never assume Hannah is their first name; never mention Welcome to Hammer until account created"
        )

    def _tool_fill_hammer_account_field(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.hammer_knowledge_active():
            return "Not available yet."
        if not hammer_office_configured():
            return (
                "error — Hammer Office credentials not configured on this server; "
                "do not say rep will reach out — ask caller to hold one moment"
            )
        email = str(args["email"]).strip()
        field_name = str(args["field"]).strip()
        value = str(args["value"]).strip()
        try:
            result = fill_hammer_account_field(email, field_name, value)
        except HammerOfficeError as exc:
            err = str(exc)
            if "business type needs legal structure" in err.lower():
                return (
                    "business type clarification needed — ask exactly one question: "
                    "Is the business an LLC, corporation, partnership, or sole proprietorship? "
                    "Do not ask dealership type."
                )
            if "no open hammer office form" in err.lower() or "open_hammer_account_form" in err.lower():
                # Form session not open — try to open it automatically before failing.
                try:
                    from hammer_office_session import open_hammer_account_form as _open
                    from agreement_approvals import agreement_approval_status as _approval
                    approval = _approval(email, wait_seconds=0)
                    dealership = str(approval.get("dealership", "") or "").strip()
                    _open(email, dealership_name=dealership)
                    result = fill_hammer_account_field(email, field_name, value)
                except HammerOfficeError as inner:
                    return (
                        f"fill failed — {str(inner)[:180]}; "
                        "call open_hammer_account_form first, then retry fill"
                    )
                except Exception:
                    return (
                        f"fill failed — {err[:180]}; "
                        "call open_hammer_account_form first, then retry fill"
                    )
            else:
                return f"fill failed — {err[:180]}"
        if result.get("account_created"):
            return (
                "account created — PHASE C.1 only: ask if Welcome to Hammer email arrived; "
                "do not mention activate, password, or card yet; do not call create_hammer_account"
            )
        email_reminder = f" [session email = {email}]"
        if result.get("message"):
            msg = str(result["message"])
            # Append email reminder to keep it in recent context (trim to fit 300 chars total)
            combined = f"{msg}{email_reminder}"
            return combined[:300]
        if result.get("billing_country") == "US":
            return (
                f"ok — US ({result.get('region_code') or 'address'}); USD set; skip tax numbers — "
                f"ask next required field only (never ask role aloud){email_reminder}"
            )
        if result.get("billing_country") == "CA":
            tax = (
                "ask QST next (field qst) if needed"
                if result.get("is_quebec") or result.get("tax_field") == "qst"
                else "ask GST/HST next (field gst_hst) if needed"
            )
            return (
                f"ok — Canada ({result.get('region_code') or 'address'}); CAD set; {tax} — "
                f"never ask role aloud{email_reminder}"
            )
        if result.get("timezone_set"):
            return f"ok — {args.get('field')} filled; timezone {result['timezone_set']}; confirm US vs Canada if unclear{email_reminder}"
        return f"ok — {args.get('field')} filled{email_reminder}"

    def _tool_create_hammer_account(self, session: CallSession, args: dict[str, Any]) -> str:
        if not session.hammer_knowledge_active():
            return "Not available yet."
        email = str(args["email"]).strip()
        status = agreement_approval_status(email, wait_seconds=0)
        if not status.get("approved") and status.get("pending"):
            status = ensure_voice_call_approval(email)
        if not status.get("approved"):
            return "blocked — agreement email I approve not verified yet; do not create account"
        if not hammer_office_configured():
            return (
                "error — Hammer Office credentials not configured on this server; "
                "do not say rep will reach out — ask caller to hold one moment while the system connects"
            )
        from hammer_office_session import get_session_values

        stored = get_session_values(email)
        if stored:
            dealership = (
                stored.get("dealership_name")
                or stored.get("legal_name")
                or str(args.get("dealership_name", "") or "").strip()
            )
            name = stored.get("name") or str(args.get("name", "") or "").strip()
            phone = stored.get("phone") or str(args.get("phone", "") or "").strip()
            website = stored.get("website") or str(args.get("website", "") or "").strip()
            address = stored.get("address") or str(args.get("address", "") or "").strip()
            business_type = stored.get("business_type") or str(args.get("business_type", "") or "").strip()
            currency = stored.get("currency") or str(args.get("currency", "") or "").strip() or "USD"
        else:
            dealership = str(args["dealership_name"]).strip()
            name = str(args["name"]).strip()
            phone = str(args["phone"]).strip() or str(args.get("cell_phone", "") or "").strip()
            website = str(args["website"]).strip()
            address = str(args["address"]).strip()
            business_type = str(args["business_type"]).strip()
            currency = str(args.get("currency", "") or "").strip() or "USD"
        if address_is_hammer_placeholder(address):
            return "blocked — need real business address from caller"
        done, _url = account_already_created(email)
        if done:
            if _url:
                session.lead.values["account_url"] = _url
                session.lead.account_created = True
                try:
                    from voice_dashboard_store import update_account_url_by_email
                    update_account_url_by_email(email, _url)
                except Exception:
                    pass
            return "account already created — continue PHASE C step-by-step; do not re-enter fields"
        role = str(args.get("role", "") or "").strip() or stored.get("role", "") or "Owner"
        req = HammerAccountRequest(
            email=email,
            name=name,
            legal_name=str(args.get("legal_name", "") or "").strip() or dealership,
            display_name=str(args.get("display_name", "") or "").strip() or dealership,
            phone=phone,
            cell_phone=str(args.get("cell_phone", "") or "").strip() or phone,
            website=website,
            address=address,
            business_type=business_type,
            timezone=str(args.get("timezone", "") or stored.get("timezone", "") or "").strip(),
            currency=currency,
            gst_hst=str(args.get("gst_hst", "") or stored.get("gst_hst", "") or "").strip(),
            qst=str(args.get("qst", "") or stored.get("qst", "") or "").strip(),
            dealership_name=dealership,
            role=role,
            selected_plan=str(args.get("selected_plan", "") or session.agreement_plan or "").strip(),
        )
        try:
            result = create_hammer_account(req)
        except HammerOfficeError as exc:
            msg = str(exc)
            if "not approved" in msg.lower():
                return f"blocked — {msg[:200]}"
            if "business type needs legal structure" in msg.lower():
                return (
                    "business type clarification needed — ask exactly one question: "
                    "Is the business an LLC, corporation, partnership, or sole proprietorship? "
                    "Then retry with that legal structure."
                )
            return (
                f"account creation failed — {msg[:200]}; "
                "retry: call create_hammer_account once more with the same fields; "
                "do not mention a rep or give up on the first failure"
            )
        if result.dry_run:
            return (
                "ok (dry run) — account created; PHASE C.1 only: did Welcome to Hammer email arrive? "
                "(one step — no activate/password/card yet)"
            )
        
        if result.account_url:
            session.lead.values["account_url"] = result.account_url
            session.lead.account_created = True
            try:
                from voice_dashboard_store import update_account_url_by_email
                update_account_url_by_email(email, result.account_url)
            except Exception:
                pass

        return (
            "ok — account created; PHASE C.1 only: ask if Welcome to Hammer email arrived — "
            "one question, wait — do not list activate, password, or card in one turn"
        )


def re_sub_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def parse_tool_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    s = raw.strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}
