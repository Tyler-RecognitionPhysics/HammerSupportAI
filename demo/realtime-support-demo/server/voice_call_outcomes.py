"""Infer voice call funnel outcomes for the admin dashboard."""

from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_OUTCOME_ORDER: tuple[tuple[str, str, str], ...] = (
    ("account_created", "Deal closed", "closed"),
    ("i_approve_approved", "Approved", "approved"),
    ("agreement_email_sent", "Email sent", "email"),
    ("capture_lead_fired", "Lead captured", "email"),
    ("pen_hammer_close_active", "Hammer handoff", "handoff"),
)

_SUMMARY_EMAIL_HINTS = (
    "agreement email",
    "sent the agreement",
    "sending the agreement",
    "email with the agreement",
    "sent you an email",
    "sent an email",
    "capture_lead",
    "premise agreement",
)

_SUMMARY_APPROVE_HINTS = (
    "i approve",
    "approved the agreement",
    "agreement was approved",
    "approval received",
    "clicked approve",
)

_SUMMARY_ACCOUNT_HINTS = (
    "account created",
    "account is set up",
    "account was created",
    "hammer account",
    "hammer office",
    "welcome to hammer",
    "logged in",
    "create_hammer_account",
    "signed up for hammer",
)

_SUMMARY_HANDOFF_HINTS = (
    "hammer drive",
    "hammer connect",
    "facebook aia",
    "pen challenge",
    "skip the pen",
    "move on to hammer",
)


def _has_any_flag(call: dict[str, Any]) -> bool:
    return any(
        call.get(k)
        for k in (
            "capture_lead_fired",
            "agreement_email_sent",
            "i_approve_approved",
            "account_created",
            "pen_hammer_close_active",
        )
    )


def call_needs_detail_enrichment(call: dict[str, Any]) -> bool:
    """True when the calls list should fetch full ElevenLabs conversation data."""
    if call.get("account_created"):
        return False
    if not str(call.get("call_id") or "").strip():
        return False
    if not _has_any_flag(call):
        return True
    if call.get("i_approve_approved") and not call.get("account_created"):
        return True
    if call.get("pen_hammer_close_active") and not call.get("account_created"):
        return True
    return False


def _needs_transcript_enrichment(call: dict[str, Any]) -> bool:
    """Re-parse transcript/tools when deal-closed or other key outcomes may be missing."""
    if not call.get("account_created"):
        return True
    return not _has_any_flag(call)


def infer_outcomes_from_session_log(call: dict[str, Any], session_log: list[str] | None) -> dict[str, Any]:
    if not session_log:
        return call
    for line in session_log:
        lower = str(line or "").lower()
        if not lower:
            continue
        if "create_hammer_account" in lower or "tool: create_hammer_account" in lower:
            from voice_call_summary import is_hammer_account_created_result

            if is_hammer_account_created_result(lower) or "account created" in lower:
                call["account_created"] = True
                call["i_approve_approved"] = call.get("i_approve_approved") or True
        if "capture_lead" in lower and ("ok" in lower or "agreement email" in lower):
            call["capture_lead_fired"] = True
            call["agreement_email_sent"] = True
        if "check_agreement_approval" in lower and "approved" in lower and "not approved" not in lower[:48]:
            call["i_approve_approved"] = True
    return call


def infer_outcomes_from_summary(call: dict[str, Any], summary: str) -> dict[str, Any]:
    text = (summary or "").lower()
    if not text:
        return call

    if any(h in text for h in _SUMMARY_EMAIL_HINTS):
        call["capture_lead_fired"] = True
        call["agreement_email_sent"] = True
    if any(h in text for h in _SUMMARY_APPROVE_HINTS):
        call["i_approve_approved"] = True
        call["agreement_email_sent"] = call.get("agreement_email_sent") or True
    if any(h in text for h in _SUMMARY_ACCOUNT_HINTS):
        call["account_created"] = True
        call["i_approve_approved"] = call.get("i_approve_approved") or True
    if any(h in text for h in _SUMMARY_HANDOFF_HINTS):
        call["pen_hammer_close_active"] = True

    values = dict(call.get("values") or {})
    if not values.get("email"):
        match = _EMAIL_RE.search(summary)
        if match:
            values["email"] = match.group(0)
    call["values"] = values
    return call


def apply_accumulator_outcomes(call: dict[str, Any], acc: Any) -> dict[str, Any]:
    """Merge VoiceCallLeadAccumulator funnel flags into a dashboard call dict."""
    for key in (
        "capture_lead_fired",
        "agreement_email_sent",
        "i_approve_approved",
        "account_created",
        "pen_challenge_skipped",
        "pen_hammer_close_active",
        "summary_sent",
    ):
        call[key] = bool(call.get(key) or getattr(acc, key, False))

    values = dict(call.get("values") or {})
    acc_values = getattr(acc, "values", None) or {}
    if isinstance(acc_values, dict):
        values.update({k: v for k, v in acc_values.items() if v})
    call["values"] = values

    if getattr(acc, "interaction_summary", "") and not call.get("interaction_summary"):
        call["interaction_summary"] = acc.interaction_summary
    if getattr(acc, "channel", "") and call.get("channel") in ("", "elevenlabs", "voice"):
        call["channel"] = acc.channel
    if getattr(acc, "call_direction", "") and not call.get("call_direction"):
        call["call_direction"] = acc.call_direction
    if getattr(acc, "started_at", "") and not call.get("started_at"):
        call["started_at"] = acc.started_at
    if getattr(acc, "ended_at", "") and not call.get("ended_at"):
        call["ended_at"] = acc.ended_at
    return call


def enrich_call_outcomes(call: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill funnel flags from ElevenLabs payload, transcript tools, and summaries."""
    payload = data if isinstance(data, dict) else {}
    if not payload.get("transcript") and call.get("transcript"):
        payload = {**payload, "transcript": call.get("transcript")}

    summary = str(call.get("interaction_summary") or "")
    analysis = payload.get("analysis") or {}
    if isinstance(analysis, dict):
        summary = summary or str(analysis.get("transcript_summary") or analysis.get("call_summary_title") or "")

    if payload.get("transcript") and _needs_transcript_enrichment(call):
        from elevenlabs_agent import _build_accumulator_from_el_transcript

        merged = {**payload, "conversation_id": call.get("call_id") or payload.get("conversation_id")}
        acc = _build_accumulator_from_el_transcript(merged)
        apply_accumulator_outcomes(call, acc)

    infer_outcomes_from_session_log(call, call.get("session_log") or [])

    if not call.get("account_created") and summary:
        infer_outcomes_from_summary(call, summary)
    elif summary and not _has_any_flag(call):
        infer_outcomes_from_summary(call, summary)

    email = str((call.get("values") or {}).get("email") or "").strip()
    if email:
        try:
            from hammer_office_session import account_already_created

            done, _url = account_already_created(email)
            if done:
                call["account_created"] = True
                call["i_approve_approved"] = call.get("i_approve_approved") or True
                if _url:
                    call["account_url"] = _url
                    if "values" not in call or not isinstance(call["values"], dict):
                        call["values"] = {}
                    call["values"]["account_url"] = _url
                    try:
                        from voice_dashboard_store import update_account_url_by_email
                        update_account_url_by_email(email, _url)
                    except Exception:
                        pass
        except Exception:
            pass
    return call


def primary_outcome(call: dict[str, Any]) -> dict[str, str]:
    for field, label, slug in _OUTCOME_ORDER:
        if call.get(field):
            return {"field": field, "label": label, "slug": slug}
    return {"field": "", "label": "No conversion", "slug": "none"}


def call_matches_outcome_filter(call: dict[str, Any], filt: str) -> bool:
    if not filt or filt == "all":
        return True
    if filt == "email":
        return bool(call.get("agreement_email_sent") or call.get("capture_lead_fired"))
    if filt == "approved":
        return bool(call.get("i_approve_approved"))
    if filt == "closed":
        return bool(call.get("account_created"))
    if filt == "handoff":
        return bool(call.get("pen_hammer_close_active"))
    if filt == "converted":
        return bool(
            call.get("account_created")
            or call.get("i_approve_approved")
            or call.get("agreement_email_sent")
            or call.get("capture_lead_fired")
        )
    if filt == "none":
        return not call_matches_outcome_filter(call, "converted") and not call.get("pen_hammer_close_active")
    return True


def outcome_counts(calls: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "all": len(calls),
        "email": sum(1 for c in calls if call_matches_outcome_filter(c, "email")),
        "approved": sum(1 for c in calls if call_matches_outcome_filter(c, "approved")),
        "closed": sum(1 for c in calls if call_matches_outcome_filter(c, "closed")),
        "handoff": sum(1 for c in calls if call_matches_outcome_filter(c, "handoff")),
        "converted": sum(1 for c in calls if call_matches_outcome_filter(c, "converted")),
        "none": sum(1 for c in calls if call_matches_outcome_filter(c, "none")),
    }


def normalize_call_duration(call: dict[str, Any]) -> dict[str, Any]:
    """Ensure duration_secs is set from explicit field or start/end timestamps."""
    if call.get("duration_secs"):
        try:
            call["duration_secs"] = max(0, int(call["duration_secs"]))
            return call
        except (TypeError, ValueError):
            pass

    start_raw = call.get("started_at") or ""
    end_raw = call.get("ended_at") or ""
    if start_raw and end_raw:
        try:
            from datetime import datetime

            start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            secs = int((end - start).total_seconds())
            if secs > 0:
                call["duration_secs"] = secs
        except ValueError:
            pass
    return call
