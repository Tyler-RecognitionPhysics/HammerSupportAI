"""Support Control dashboard API."""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, Field

from hubspot_kb_sync import hubspot_kb_sync_status, run_hubspot_kb_sync_async
from hubspot_tickets_sync import hubspot_tickets_sync_status, run_hubspot_tickets_sync_async
from slack_sync import run_slack_sync, slack_sync_status
from support_admin_auth import admin_auth_configured
from hubspot_ticket_create import hubspot_ticket_create_configured
from support_dashboard_store import (
    SETTING_KEYS,
    backfill_session_categories,
    clear_all_sessions,
    clear_settings,
    create_appointment,
    delete_appointment,
    dismiss_billing_item,
    get_all_settings,
    get_session,
    list_active_sessions,
    list_appointments,
    list_dismissed_billing_keys,
    list_sessions,
    list_support_tickets,
    list_support_tickets_by_category,
    set_session_resolved,
    set_settings,
    set_ticket_resolved,
    support_stats,
    update_appointment,
)
from support_ticket_slack import slack_ticket_notify_configured
from support_instructions import get_default_prompts


class SupportSettingsPatch(BaseModel):
    support_voice_prompt: str | None = None
    support_chat_prompt: str | None = None
    chat_model: str | None = None


class AppointmentCreate(BaseModel):
    requested_at: str = ""
    requested_label: str = ""
    duration_min: int = 30
    dealership_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    reason: str = ""
    status: str = "requested"
    timezone: str = ""
    notes: str = ""


class AppointmentUpdate(BaseModel):
    requested_at: str | None = None
    requested_label: str | None = None
    duration_min: int | None = None
    dealership_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    reason: str | None = None
    status: str | None = None
    timezone: str | None = None
    notes: str | None = None


def dashboard_overview() -> dict[str, Any]:
    stats = support_stats()
    slack = slack_sync_status()
    hubspot = hubspot_kb_sync_status()
    hubspot_tickets = hubspot_tickets_sync_status()
    return {
        "configured": admin_auth_configured(),
        "stats": stats,
        "active_sessions": list_active_sessions(),
        "slack_sync": slack,
        "hubspot_kb_sync": hubspot,
        "hubspot_tickets_sync": hubspot_tickets,
        "hubspot_ticket_create_configured": hubspot_ticket_create_configured(),
        "slack_ticket_notify_configured": slack_ticket_notify_configured(),
    }


def dashboard_calls(*, limit: int = 100) -> dict[str, Any]:
    # Ensure every session shows a category (heuristic, no LLM cost) so the
    # dashboard never displays a column full of "Uncategorized".
    try:
        backfill_session_categories(limit=max(limit, 200))
    except Exception:
        pass
    return {"calls": list_sessions(limit=limit)}


def dashboard_clear_sessions() -> dict[str, Any]:
    """Delete all stored sessions (transcripts + summaries). Tickets are kept."""
    deleted = clear_all_sessions()
    return {"ok": True, "deleted": deleted}


def dashboard_call_detail(call_id: str) -> dict[str, Any]:
    row = get_session(call_id)
    if not row:
        return {"ok": False, "error": "Session not found"}
    return {"ok": True, "call": row}


def dashboard_tickets(*, limit: int = 50) -> dict[str, Any]:
    return {"tickets": list_support_tickets(limit=limit)}


# Requests the AI routes straight to a human (billing disputes, cancellations).
# These are surfaced together so staff can action them quickly.
BILLING_CANCEL_CATEGORIES = ("billing", "cancellation")

# Cancellations/billing often arrive as scheduled callbacks (the calendar path
# requires no email, unlike ticket creation), so they never reached this panel.
# Classify a callback's free-text reason so those requests show up here too.
_CANCEL_REASON_RE = re.compile(
    r"\b(cancel\w*|cancell\w*|pause|paus\w*|downgrade|terminat\w*|discontinu\w*|"
    r"unsubscrib\w*|close (?:my |the )?account|end (?:my |the )?(?:service|subscription|plan|account))\b",
    re.IGNORECASE,
)
_BILLING_REASON_RE = re.compile(
    r"\b(billing|bill|invoice\w*|charge\w*|overcharg\w*|payment\w*|refund\w*|"
    r"receipt|credit card|debit card|double[- ]?charged|dispute)\b",
    re.IGNORECASE,
)


def _classify_callback_reason(reason: str) -> str:
    """Return 'cancellation' / 'billing' if the callback is about either, else ''."""
    text = reason or ""
    if _CANCEL_REASON_RE.search(text):
        return "cancellation"
    if _BILLING_REASON_RE.search(text):
        return "billing"
    return ""


def _callback_to_billing_row(appt: dict[str, Any], category: str) -> dict[str, Any]:
    """Normalize a callback appointment into the billing-card shape used by tickets."""
    status = str(appt.get("status") or "").lower()
    return {
        # Prefix the id so it never collides with an integer ticket id.
        "id": f"appt-{appt.get('id')}",
        "kind": "callback",
        "appointment_id": appt.get("id"),
        "dealership": appt.get("dealership_name") or "",
        "contact_name": appt.get("contact_name") or "",
        "first_name": appt.get("first_name") or "",
        "last_name": appt.get("last_name") or "",
        "email": appt.get("email") or "",
        "phone": appt.get("phone") or "",
        "message": appt.get("reason") or "",
        "created_at": appt.get("created_at") or appt.get("requested_at") or "",
        "session_id": appt.get("session_id") or "",
        "channel": appt.get("channel") or "",
        "resolved": status in ("completed", "cancelled"),
        "status": appt.get("status") or "",
        "issue_category": category,
        "requested_at": appt.get("requested_at") or "",
        "requested_label": appt.get("requested_label") or "",
        "ticket_url": "",
        "hubspot_ticket_id": "",
    }


# A session can be a billing/cancellation request even when Hannah never fired a
# tool (she sometimes just *says* she escalated). Extract contact details from the
# transcript so those sessions still surface here with as much info as we have.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\s.-]?){10,15}")


def _session_fingerprint(session: dict[str, Any]) -> str:
    """Content key that is identical across duplicate rows of one conversation.

    Voice conversations can land as more than one session row (e.g. the live LLM
    proxy plus the post-call webhook), each summarized separately, so the call_id
    differs even though it's the same chat. Fingerprint the actual turns so those
    collapse into a single billing/cancellation card.
    """
    parts: list[str] = []
    for turn in session.get("transcript") or []:
        role = str(turn.get("role") or "").strip().lower()
        text = re.sub(r"\s+", " ", str(turn.get("text") or "")).strip().lower()
        if text:
            parts.append(f"{role}:{text}")
    if not parts:
        summ = re.sub(r"\s+", " ", str(session.get("interaction_summary") or "")).strip().lower()
        if summ:
            parts.append(f"summary:{summ}")
    return "\n".join(parts)


def _classify_session(session: dict[str, Any]) -> str:
    """Return 'cancellation'/'billing' for a session, else '' (cancellation wins)."""
    cat = str(session.get("issue_category") or "").strip().lower()
    if cat in BILLING_CANCEL_CATEGORIES:
        return cat
    # Only consider the customer's own words + the AI summary to avoid matching
    # an assistant message that merely mentions the word "cancel".
    parts = [str(session.get("interaction_summary") or "")]
    for turn in session.get("transcript") or []:
        if str(turn.get("role")) == "user":
            parts.append(str(turn.get("text") or ""))
    text = "\n".join(parts)
    return _classify_callback_reason(text)


def _extract_contact_from_transcript(transcript: list[dict[str, Any]]) -> dict[str, str]:
    """Best-effort pull of email/phone from the customer's chat turns."""
    email = ""
    phone = ""
    for turn in transcript or []:
        if str(turn.get("role")) != "user":
            continue
        text = str(turn.get("text") or "")
        if not email:
            m = _EMAIL_RE.search(text)
            if m:
                email = m.group(0)
        if not phone:
            m = _PHONE_RE.search(text)
            if m:
                digits = "".join(c for c in m.group(0) if c.isdigit())
                if 10 <= len(digits) <= 15:
                    phone = m.group(0).strip()
    return {"email": email, "phone": phone}


# Hannah explicitly asks for the dealership name during billing/cancellation
# requests, so the most reliable signal is the customer's reply right after she
# asks. A stated pattern ("our dealership is …") is used as a fallback.
_DEALER_ASK_RE = re.compile(
    r"name of your (?:dealership|business|store|company|dealer)"
    r"|your (?:dealership|business|store|company|dealer)(?:'s)? name"
    r"|which (?:dealership|business|store|company|dealer)"
    r"|what(?:'s| is) (?:the name of )?your (?:dealership|business|store|company|dealer)",
    re.IGNORECASE,
)
_DEALER_STATE_RE = re.compile(
    r"\b(?:dealership(?:'s)?(?: name)?|business(?: name)?|store|company)\s*(?:is|name is|:)\s+"
    r"([A-Za-z0-9][\w&'.\- ]{1,60})",
    re.IGNORECASE,
)


def _clean_dealership(text: str) -> str:
    """Trim a raw dealership answer down to a likely business name."""
    t = str(text or "").strip()
    if not t:
        return ""
    # Drop any email/phone the customer bundled into the same message.
    t = _EMAIL_RE.sub("", t)
    t = _PHONE_RE.sub("", t)
    t = t.splitlines()[0]
    t = re.split(r"[,;\n]| - | and | my (?:email|phone|number)", t, maxsplit=1)[0]
    t = re.sub(
        r"^(?:it'?s|it is|the name is|we'?re|we are|this is|i'?m (?:with|from|at)|"
        r"sure,?|yes,?|yeah,?|okay,?|ok,?)\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = t.strip(" .,-:\"'")
    return t[:60].rstrip() if len(t) > 60 else t


def _extract_dealership_from_transcript(transcript: list[dict[str, Any]]) -> str:
    """Best-effort pull of the dealership/business name from the customer's turns."""
    turns = transcript or []
    # Primary: the user's reply right after Hannah asks for the dealership name.
    for i, turn in enumerate(turns):
        if str(turn.get("role")) != "assistant":
            continue
        if not _DEALER_ASK_RE.search(str(turn.get("text") or "")):
            continue
        for nxt in turns[i + 1:]:
            if str(nxt.get("role")) == "user":
                name = _clean_dealership(str(nxt.get("text") or ""))
                if name:
                    return name
                break
    # Fallback: the customer states it themselves ("our dealership is …").
    for turn in turns:
        if str(turn.get("role")) != "user":
            continue
        m = _DEALER_STATE_RE.search(str(turn.get("text") or ""))
        if m:
            name = _clean_dealership(m.group(1))
            if name:
                return name
    return ""


def _session_to_billing_row(session: dict[str, Any], category: str) -> dict[str, Any]:
    """Normalize a billing/cancellation session into the billing-card shape."""
    call_id = str(session.get("call_id") or "").strip()
    contact = _extract_contact_from_transcript(session.get("transcript") or [])
    summary = str(session.get("interaction_summary") or "").strip()
    if not summary:
        for turn in session.get("transcript") or []:
            if str(turn.get("role")) == "user":
                summary = str(turn.get("text") or "").strip()
                break
    return {
        "id": f"sess-{call_id}",
        "kind": "session",
        "dealership": _extract_dealership_from_transcript(session.get("transcript") or []),
        "contact_name": "",
        "first_name": "",
        "last_name": "",
        "email": contact["email"],
        "phone": contact["phone"],
        "message": summary,
        "created_at": session.get("started_at") or session.get("updated_at") or "",
        "session_id": call_id,
        "channel": session.get("channel") or "",
        "resolved": bool(session.get("resolved")),
        "issue_category": category,
        "needs_ticket": True,
        "ticket_url": "",
        "hubspot_ticket_id": "",
    }


def dashboard_billing_tickets(*, limit: int = 500) -> dict[str, Any]:
    tickets = list_support_tickets_by_category(list(BILLING_CANCEL_CATEGORIES), limit=limit)

    # Fold in billing/cancellation callbacks (scheduled via the calendar) so every
    # cancel/billing request the AI handled is visible here, not just ones that
    # produced a full support ticket.
    seen_sessions = {
        str(t.get("session_id") or "").strip()
        for t in tickets
        if str(t.get("session_id") or "").strip()
    }
    try:
        appointments = list_appointments(limit=limit)
    except Exception:
        appointments = []
    for appt in appointments:
        category = _classify_callback_reason(str(appt.get("reason") or ""))
        if not category:
            continue
        sid = str(appt.get("session_id") or "").strip()
        if sid and sid in seen_sessions:
            continue  # already represented by a ticket from the same session
        if sid:
            seen_sessions.add(sid)
        tickets.append(_callback_to_billing_row(appt, category))

    # Safety net: fold in billing/cancellation chat/voice sessions that never
    # produced a ticket or callback, so a request is never silently dropped just
    # because Hannah narrated the escalation instead of calling the tool.
    try:
        sessions = list_sessions(limit=limit)
    except Exception:
        sessions = []
    seen_session_fps: dict[str, dict[str, Any]] = {}
    for sess in sessions:
        call_id = str(sess.get("call_id") or "").strip()
        if not call_id or call_id in seen_sessions:
            continue
        category = _classify_session(sess)
        if not category:
            continue
        fp = _session_fingerprint(sess)
        if fp and fp in seen_session_fps:
            # Same conversation already has a card — don't add a duplicate.
            # Cancellations outrank billing for the surviving card so the team
            # never loses sight of a cancel that was also billing-flavored.
            existing = seen_session_fps[fp]
            if category == "cancellation" and existing.get("issue_category") == "billing":
                existing["issue_category"] = "cancellation"
            seen_sessions.add(call_id)
            continue
        seen_sessions.add(call_id)
        row = _session_to_billing_row(sess, category)
        if fp:
            seen_session_fps[fp] = row
        tickets.append(row)

    # Drop any cards the operator has dismissed (keyed by the card's stable id).
    try:
        dismissed = list_dismissed_billing_keys()
    except Exception:
        dismissed = set()
    if dismissed:
        tickets = [t for t in tickets if str(t.get("id")) not in dismissed]

    # Newest first across all sources.
    tickets.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)

    open_count = sum(1 for t in tickets if not t.get("resolved"))
    billing = sum(1 for t in tickets if str(t.get("issue_category", "")).lower() == "billing")
    cancellation = sum(1 for t in tickets if str(t.get("issue_category", "")).lower() == "cancellation")
    return {
        "ok": True,
        "tickets": tickets,
        "total": len(tickets),
        "open": open_count,
        "resolved": len(tickets) - open_count,
        "billing": billing,
        "cancellation": cancellation,
    }


def dashboard_ticket_set_resolved(ticket_id: int, resolved: bool) -> dict[str, Any]:
    row = set_ticket_resolved(ticket_id, resolved)
    if not row:
        return {"ok": False, "error": "Ticket not found."}
    return {"ok": True, "ticket": row}


_VARIATIONS_SYSTEM = (
    "You are Hannah, the friendly customer-support AI for Hammer (software for car "
    "dealerships). A support manager is coaching how you should reply at one point in a "
    "real conversation. Write exactly 3 alternative replies for that moment.\n"
    "Rules:\n"
    "- Keep each reply short: 1-4 sentences, warm and professional, plain text (no markdown).\n"
    "- Stay faithful to the facts in the reference replies. Do NOT invent new policies, "
    "prices, timelines, steps, or commitments that are not already stated.\n"
    "- If the manager provided an edited draft, treat it as the preferred direction: keep "
    "its meaning and facts, and offer polished takes on it.\n"
    "- Make the 3 options meaningfully different from each other (e.g. more concise, "
    "more empathetic, more step-by-step).\n"
    'Return ONLY JSON: {"variations": ["...", "...", "..."]}'
)


def dashboard_response_variations(
    user_message: str, original_response: str, draft: str = ""
) -> dict[str, Any]:
    """Generate 3 alternative phrasings of an AI reply for the session coaching editor."""
    import json

    import httpx

    from cs_questions import _model, _openai_key, _OPENAI_URL

    key = _openai_key()
    if not key:
        return {"ok": False, "error": "OPENAI_API_KEY not configured."}
    original = (original_response or "").strip()
    if not original and not (draft or "").strip():
        return {"ok": False, "error": "Nothing to rephrase."}

    parts = []
    if (user_message or "").strip():
        parts.append(f'Customer message: "{user_message.strip()}"')
    if original:
        parts.append(f'Hannah\'s original reply: "{original}"')
    if (draft or "").strip() and draft.strip() != original:
        parts.append(f'Manager\'s edited draft (preferred direction): "{draft.strip()}"')

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                _OPENAI_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": _model(),
                    "messages": [
                        {"role": "system", "content": _VARIATIONS_SYSTEM},
                        {"role": "user", "content": "\n".join(parts)},
                    ],
                    # Higher temperature than analysis calls so the 3 options differ.
                    "temperature": 0.8,
                    "max_tokens": 700,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception as exc:
        return {"ok": False, "error": f"Could not generate variations: {exc}"}

    variations = [str(v).strip() for v in (data.get("variations") or []) if str(v).strip()]
    if not variations:
        return {"ok": False, "error": "The model returned no variations."}
    return {"ok": True, "variations": variations[:3]}


_COACH_CONTEXT_SYSTEM = (
    "You are helping file a coaching note for Hannah, the customer-support AI for Hammer "
    "(software for car dealerships). A support manager corrected one of Hannah's replies in "
    "a real conversation. You get the conversation excerpt, the customer message that "
    "triggered the reply, and the corrected reply.\n"
    "Return JSON with:\n"
    '- "trigger": the customer message rewritten as ONE self-contained question or request '
    "that makes sense with zero surrounding context. Resolve pronouns and vague references "
    '("this process", "it", "that") using the conversation. Keep the customer\'s intent and '
    "wording where possible. Max 140 characters.\n"
    '- "context": one short line (max 120 characters) describing the situation this guidance '
    'applies to, e.g. "Customer was asking how long new lead provider setup takes".\n'
    'Return ONLY JSON: {"trigger": "...", "context": "..."}'
)


def build_coach_playbook_entry(
    *,
    trigger: str,
    trigger_edited: bool,
    original_response: str,
    corrected_response: str,
    context_turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a context-aware playbook entry for a corrected session response.

    Uses the LLM (best effort) to rewrite the trigger into a self-contained question
    and to produce a one-line situation summary, so the correction is retrieved in
    the right context later. Falls back to the raw trigger when the LLM is
    unavailable — the save must never fail because contextualization did.
    """
    import json

    import httpx

    from cs_questions import _model, _openai_key, _OPENAI_URL

    corrected = (corrected_response or "").strip()
    if not corrected:
        return {"ok": False, "error": "Corrected response required."}

    raw_trigger = " ".join((trigger or "").split())
    resolved_trigger = raw_trigger
    context_line = ""

    convo = "\n".join(
        f"{'Customer' if str(t.get('role')) == 'user' else 'Hannah'}: {str(t.get('text') or '').strip()}"
        for t in (context_turns or [])
        if str(t.get("text") or "").strip()
    )
    if convo and _openai_key():
        user_prompt = (
            f"Conversation excerpt:\n{convo}\n\n"
            f'Trigger customer message: "{raw_trigger}"\n'
            f'Corrected reply: "{corrected}"'
        )
        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post(
                    _OPENAI_URL,
                    headers={
                        "Authorization": f"Bearer {_openai_key()}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": _model(),
                        "messages": [
                            {"role": "system", "content": _COACH_CONTEXT_SYSTEM},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": 300,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                data = json.loads(resp.json()["choices"][0]["message"]["content"])
            # A manually edited trigger is the manager's call — keep it verbatim.
            if not trigger_edited:
                resolved_trigger = " ".join(str(data.get("trigger") or "").split()) or raw_trigger
            context_line = " ".join(str(data.get("context") or "").split())[:160]
        except Exception:
            pass

    if not resolved_trigger:
        resolved_trigger = " ".join((original_response or "").split())[:70]
    if not resolved_trigger:
        return {"ok": False, "error": "Could not determine when this answer applies."}

    title = f"Corrected response: {resolved_trigger[:70]}"
    lines: list[str] = []
    if context_line:
        lines += [f"Context: {context_line}", ""]
    lines += [
        f'When a customer says something like: "{resolved_trigger}"',
        "",
        "Respond this way:",
        corrected,
    ]
    return {
        "ok": True,
        "title": title,
        "content": "\n".join(lines),
        "trigger": resolved_trigger,
        "context": context_line,
    }


def dashboard_session_set_resolved(call_id: str, resolved: bool) -> dict[str, Any]:
    """Resolve/reopen a billing/cancellation session card (no ticket was created)."""
    row = set_session_resolved(call_id, resolved)
    if not row:
        return {"ok": False, "error": "Session not found."}
    return {"ok": True, "session": row}


def dashboard_billing_dismiss(item_key: str) -> dict[str, Any]:
    """Hide a billing/cancellation card from the list (non-destructive)."""
    if not dismiss_billing_item(item_key):
        return {"ok": False, "error": "Missing item id."}
    return {"ok": True}


def dashboard_appointments(*, start: str = "", end: str = "", status: str = "", limit: int = 500) -> dict[str, Any]:
    appts = list_appointments(start=start, end=end, status=status, limit=limit)
    # Billing/cancellation callbacks live in the Billing & Cancellations panel AND
    # on the calendar (when they carry a concrete time), so the team can see at a
    # glance when the customer wants the call. Tag them with their category so the
    # UI can badge them; drop only the ones with no time on the books — those are
    # escalations that belong solely in the billing panel.
    visible = []
    for appt in appts:
        category = _classify_callback_reason(str(appt.get("reason") or ""))
        if category:
            if not str(appt.get("requested_at") or "").strip():
                continue
            appt = {**appt, "category": category}
        visible.append(appt)
    return {"appointments": visible}


def dashboard_appointment_create(body: AppointmentCreate) -> dict[str, Any]:
    label = body.requested_label.strip()
    if not label and body.requested_at.strip():
        label = body.requested_at.strip()
    appt = create_appointment(
        requested_at=body.requested_at,
        duration_min=body.duration_min,
        dealership_name=body.dealership_name,
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        phone=body.phone,
        reason=body.reason,
        status=body.status,
        source="manual",
        timezone=body.timezone,
        requested_label=label,
        notes=body.notes,
    )
    try:
        from support_ticket_slack import post_callback_scheduled_alert

        post_callback_scheduled_alert(
            dealership_name=appt["dealership_name"],
            contact_name=appt["contact_name"],
            phone=appt["phone"],
            email=appt["email"],
            when_label=appt["requested_label"] or appt["requested_at"],
            reason=appt["reason"],
            channel=appt["channel"],
            source="manual",
        )
    except Exception:
        pass
    return {"ok": True, "appointment": appt}


def dashboard_appointment_update(appointment_id: int, body: AppointmentUpdate) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    appt = update_appointment(appointment_id, fields)
    if not appt:
        return {"ok": False, "error": "Appointment not found"}
    return {"ok": True, "appointment": appt}


def dashboard_appointment_delete(appointment_id: int) -> dict[str, Any]:
    ok = delete_appointment(appointment_id)
    return {"ok": ok}


def dashboard_cs_questions() -> dict[str, Any]:
    from cs_questions import get_cs_questions

    return get_cs_questions()


def dashboard_cs_questions_status() -> dict[str, Any]:
    from cs_questions import cs_questions_status

    return cs_questions_status()


def dashboard_cs_questions_rebuild() -> dict[str, Any]:
    from cs_questions import start_cs_questions_rebuild

    return start_cs_questions_rebuild()


class QaAnswerSave(BaseModel):
    key: str = ""
    question: str
    category: str = "other"
    answer: str = ""
    updated_by: str = ""


def dashboard_qa() -> dict[str, Any]:
    from support_qa import get_qa_board

    return get_qa_board()


def dashboard_qa_save(body: QaAnswerSave) -> dict[str, Any]:
    from support_qa import save_qa_answer

    return save_qa_answer(
        key=body.key,
        question=body.question,
        category=body.category,
        answer=body.answer,
        updated_by=body.updated_by,
    )


class QaGenerateRequest(BaseModel):
    scope: str = "unanswered"
    keys: list[str] = []


def dashboard_qa_generate(
    body: QaGenerateRequest,
    get_retriever: Any,
    get_tool_executor: Any,
) -> dict[str, Any]:
    from support_qa import start_qa_generation

    return start_qa_generation(
        get_retriever,
        get_tool_executor,
        scope=body.scope,
        keys=body.keys or None,
    )


def dashboard_qa_generate_status() -> dict[str, Any]:
    from support_qa import qa_generation_status

    return {"ok": True, **qa_generation_status()}


def dashboard_qa_generate_cancel() -> dict[str, Any]:
    from support_qa import cancel_qa_generation

    return cancel_qa_generation()


def dashboard_qa_approve_all(updated_by: str = "") -> dict[str, Any]:
    from support_qa import approve_all_qa_drafts

    return approve_all_qa_drafts(updated_by=updated_by)


class QaDiscardRequest(BaseModel):
    key: str


def dashboard_qa_discard(body: QaDiscardRequest) -> dict[str, Any]:
    from support_qa import discard_qa_draft

    return discard_qa_draft(key=body.key)


async def dashboard_qa_regenerate(
    body: QaAnswerSave,
    retriever: Any,
    executor: Any,
) -> dict[str, Any]:
    from support_qa import regenerate_qa_answer

    return await regenerate_qa_answer(
        retriever,
        executor,
        key=body.key,
        question=body.question,
        category=body.category,
    )


def dashboard_settings_get() -> dict[str, Any]:
    defaults = get_default_prompts()
    overrides = get_all_settings()
    env_model = os.environ.get("SUPPORT_CHAT_MODEL", "gpt-4o-mini").strip()
    return {
        "defaults": defaults,
        "overrides": {k: overrides[k] for k in SETTING_KEYS if k in overrides},
        # The model the server falls back to when no override is set — lets the
        # dashboard show "default: X" even while a custom model is active.
        "env_default_model": env_model,
        "effective": {
            "support_voice_prompt": overrides.get("support_voice_prompt") or defaults["support_voice_prompt"],
            "support_chat_prompt": overrides.get("support_chat_prompt") or defaults["support_chat_prompt"],
            "chat_model": overrides.get("chat_model") or env_model,
        },
    }


def dashboard_settings_patch(body: SupportSettingsPatch) -> dict[str, Any]:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if patch:
        set_settings(patch)
    return {"ok": True, "settings": dashboard_settings_get()}


def dashboard_settings_reset() -> dict[str, Any]:
    clear_settings()
    return {"ok": True, "settings": dashboard_settings_get()}


async def dashboard_slack_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    return run_slack_sync(full_backfill=full_backfill)


async def dashboard_hubspot_kb_sync() -> dict[str, Any]:
    return await run_hubspot_kb_sync_async()


async def dashboard_hubspot_tickets_sync(*, full_backfill: bool = False) -> dict[str, Any]:
    return await run_hubspot_tickets_sync_async(full_backfill=full_backfill)
