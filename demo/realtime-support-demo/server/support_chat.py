"""Text support chat — wiki-grounded OpenAI completions."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Sequence

import httpx

from support_instructions import build_support_chat_prompt
from support_ticket_service import ticket_creation_enabled
from support_tools import (
    SupportSession,
    SupportToolExecutor,
    _NO_TIME_GUARD_RESULT,
    append_callback_time_prompt,
    customer_stated_a_time,
    format_support_knowledge_result,
    redact_support_contacts,
    should_offer_callback_time,
    support_tool_definitions,
)
from wiki_retrieval import Chunk

# Detect billing/cancellation intent so we can guarantee a ticket gets created
# (the model otherwise sometimes just *says* it escalated without calling the tool).
_BILLING_CANCEL_INTENT_RE = re.compile(
    r"\b(cancel\w*|cancell\w*|pause|downgrade|terminat\w*|discontinu\w*|unsubscrib\w*|"
    r"close (?:my |the )?account|stop (?:my |the )?(?:service|subscription|account)|"
    r"billing|invoice\w*|charge\w*|overcharg\w*|refund\w*|payment|double[- ]?charged)\b",
    re.IGNORECASE,
)

_TICKET_ENFORCE_NUDGE = (
    "Reminder: this conversation is a billing or cancellation request. You have NOT yet "
    "called create_support_ticket. If you already have the dealership name, the customer's "
    "name, and the email on their Hammer account, call create_support_ticket now with "
    "issue_category set to 'billing' or 'cancellation' and resolved=false (a phone number is "
    "optional — do not wait for it). If any required field is still missing, ask the customer "
    "for just that missing field — and never tell them to email anyone or that the request has "
    "been logged or that someone will reach out until the ticket has actually been created."
)

# Facebook / advertising questions are never answered by the AI — they route to a
# human via a ticket (and an optional scheduled callback).
_FACEBOOK_ADS_INTENT_RE = re.compile(
    r"\b(facebook|fb|meta|instagram|insta|marketplace|"
    r"(?:\w+\s+)?ads?|advertis\w*|ad\s+account|ad\s+campaign\w*|campaign\w*|"
    r"aia|automated\s+inventory|boosted?\s+post\w*|ad\s+spend|ad\s+budget)\b",
    re.IGNORECASE,
)

_FACEBOOK_ENFORCE_NUDGE = (
    "Reminder: this is a Facebook/advertising question. Do NOT troubleshoot it or list things "
    "to check — our ads team handles these. You have NOT yet called create_support_ticket. If "
    "you have the dealership name, the customer's name, and the email on their Hammer account, "
    "call create_support_ticket now with issue_category='facebook-aia', resolved=false, and an "
    "issue_summary of what they reported. If a required field is missing, ask for just that one "
    "field. Then ask if they have a preferred day/time for our team to reach out, and if they "
    "give one, call schedule_callback so it lands on the calendar. Never claim it has been "
    "logged or that someone will reach out until create_support_ticket has actually run."
)

# Deterministic, never-troubleshoot reply for Facebook/ad questions. gpt-4o-mini
# will happily generate generic ad "things to check" from its own training data
# even when the prompt forbids it, so for ad intent we never surface the model's
# free-form text until a ticket actually exists — we route to a human instead.
_FACEBOOK_ROUTING_REPLY = (
    "Anything with your Facebook or Meta advertising is handled by our ads specialists, "
    "so I won't try to troubleshoot it here — I'll get it straight to the right person. "
    "To open that for you, could you share your dealership name, your first and last name, "
    "and the email on your Hammer account? It also helps to know a day and time that work "
    "best for a callback, and I'll get you on the calendar."
)


def _is_billing_cancel_intent(messages: list[dict[str, str]]) -> bool:
    for m in messages:
        if m.get("role") == "user" and _BILLING_CANCEL_INTENT_RE.search(str(m.get("content") or "")):
            return True
    return False


def _is_facebook_ads_intent(messages: list[dict[str, str]]) -> bool:
    for m in messages:
        if m.get("role") == "user" and _FACEBOOK_ADS_INTENT_RE.search(str(m.get("content") or "")):
            return True
    return False


def _chat_model() -> str:
    return os.environ.get("SUPPORT_CHAT_MODEL", os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")).strip()


def _format_excerpts(chunks: Sequence[tuple[Chunk, float]]) -> str:
    lines = []
    for ch, sc in chunks:
        lines.append(f"[{ch.doc_id}] (score={sc:.2f})\n{ch.text.strip()}\n")
    return "\n".join(lines)


def _support_context(retriever: Any, query: str, *, max_chars: int = 4500) -> str:
    if hasattr(retriever, "search_support_knowledge"):
        result = retriever.search_support_knowledge(
            query,
            official_k=4,
            ticket_case_limit=4,
            ticket_chunks_per_case=2,
        )
        return format_support_knowledge_result(result, max_chars=max_chars)
    pairs = retriever.top_k(query, k=8) if query.strip() else []
    return _format_excerpts(pairs)


def _search_support_result(retriever: Any, query: str) -> dict[str, Any]:
    if hasattr(retriever, "search_support_knowledge"):
        return retriever.search_support_knowledge(
            query,
            official_k=4,
            ticket_case_limit=4,
            ticket_chunks_per_case=2,
        )
    pairs = retriever.top_k(query, k=8) if query.strip() else []
    return {"official": pairs, "ticket_cases": [], "all": pairs}


class _ChatSessionStub:
    call_id: str = "chat"
    channel: str = "chat"
    issue_category: str = ""
    escalated: bool = False
    resolved: bool = False
    session_log: list = []


async def complete_support_chat(
    executor: SupportToolExecutor,
    retriever: Any,
    messages: list[dict[str, str]],
    session: SupportSession | None = None,
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    billing_cancel_intent = _is_billing_cancel_intent(messages)
    facebook_ads_intent = _is_facebook_ads_intent(messages)
    route_facebook = facebook_ads_intent and ticket_creation_enabled()

    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    # For Facebook/ad questions we route to a human instead of answering, so we do
    # NOT feed troubleshooting KB into the prompt — otherwise the model grounds in
    # it and lists "things to check" even though it was told not to.
    wiki_context = "" if route_facebook else (_support_context(retriever, last_user) if last_user.strip() else "")
    system = build_support_chat_prompt(wiki_context=wiki_context)

    openai_messages: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            openai_messages.append({"role": role, "content": content})

    # Inject the routing directive UP FRONT (before the first model turn) so the
    # model never produces a troubleshooting answer it then has to walk back.
    if route_facebook:
        openai_messages.append({"role": "system", "content": _FACEBOOK_ENFORCE_NUDGE})

    tools = support_tool_definitions()
    model = _chat_model()
    if session is None:
        session = _ChatSessionStub()  # type: ignore

    ticket_nudged = False

    async with httpx.AsyncClient(timeout=90.0) as client:
        for _ in range(5):
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": openai_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                # Facebook/ad intent: the model must never surface free-form text (it
                # tends to invent generic "things to check" regardless of the prompt).
                # Until a ticket actually exists, replace any answer with a fixed
                # routing reply that collects contact info + a callback time.
                if route_facebook and not getattr(session, "ticket_created", False):
                    return _FACEBOOK_ROUTING_REPLY
                # For billing/cancellation requests, never let Hannah finalize a reply
                # (troubleshooting or claiming it's handled) unless a ticket was actually
                # created. Nudge once with the right instruction.
                if (
                    billing_cancel_intent
                    and not ticket_nudged
                    and ticket_creation_enabled()
                    and not getattr(session, "ticket_created", False)
                ):
                    ticket_nudged = True
                    openai_messages.append(choice)
                    openai_messages.append({"role": "system", "content": _TICKET_ENFORCE_NUDGE})
                    continue
                reply = redact_support_contacts(str(choice.get("content") or "").strip())
                # Always offer to schedule a callback after a follow-up ticket is logged.
                # The time question must be the closing sentence (any "anything else?"
                # pleasantry is stripped so the reply doesn't end with two questions).
                if should_offer_callback_time(messages, session, reply):
                    reply = append_callback_time_prompt(reply)
                return reply

            openai_messages.append(choice)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                # Hard guard: never let the model auto-book a callback at a time the
                # customer never gave. It must ask first.
                if name == "schedule_callback" and not customer_stated_a_time(messages):
                    result = _NO_TIME_GUARD_RESULT
                else:
                    result = await executor.execute_tool(name, args, session)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result,
                    }
                )

    return "I'm having trouble right now — let me log a ticket so a Hammer representative can reach out as soon as possible. Could you share your dealership name, your name, and the email on your Hammer account?"


def _record_sources(
    sources: list[dict[str, Any]],
    pairs: Sequence[tuple[Chunk, float]],
    *,
    via: str,
) -> None:
    seen = {(s["doc_id"], s["chunk_id"]) for s in sources}
    for ch, sc in pairs:
        key = (ch.doc_id, ch.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "doc_id": ch.doc_id,
                "chunk_id": ch.chunk_id,
                "score": round(float(sc), 4),
                "text": ch.text.strip(),
                "via": via,
            }
        )


def _record_support_sources(
    sources: list[dict[str, Any]],
    result: dict[str, Any],
    *,
    via: str,
) -> None:
    """Record structured retrieval so Test AI can show exact Help Desk cases."""
    seen = {(s["doc_id"], s["chunk_id"], s.get("source_group", "")) for s in sources}

    for ch, sc in result.get("playbook") or []:
        key = (ch.doc_id, ch.chunk_id, "official")
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "doc_id": ch.doc_id,
                "chunk_id": ch.chunk_id,
                "score": round(float(sc), 4),
                "text": ch.text.strip(),
                "via": via,
                "source_group": "official",
                "approved": True,
            }
        )

    for ch, sc in result.get("official") or []:
        key = (ch.doc_id, ch.chunk_id, "official")
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "doc_id": ch.doc_id,
                "chunk_id": ch.chunk_id,
                "score": round(float(sc), 4),
                "text": ch.text.strip(),
                "via": via,
                "source_group": "official",
            }
        )

    for case_rank, case in enumerate(result.get("ticket_cases") or [], start=1):
        case_doc_id = str(case.get("doc_id") or "")
        case_score = round(float(case.get("score") or 0.0), 4)
        solution_score = round(float(case.get("solution_score") or 0.0), 2)
        email_worked = bool(case.get("email_worked"))
        pinned = bool(case.get("pinned"))
        for ch, sc in case.get("chunks") or []:
            key = (ch.doc_id, ch.chunk_id, "helpdesk_ticket")
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "doc_id": ch.doc_id,
                    "chunk_id": ch.chunk_id,
                    "score": round(float(sc), 4),
                    "text": ch.text.strip(),
                    "via": via,
                    "source_group": "helpdesk_ticket",
                    "case_rank": case_rank,
                    "case_doc_id": case_doc_id,
                    "case_score": case_score,
                    "solution_score": solution_score,
                    "email_worked": email_worked,
                    "pinned": pinned,
                }
            )


async def preview_support_response(
    executor: SupportToolExecutor,
    retriever: Any,
    question: str,
) -> dict[str, Any]:
    """Run the same chat pipeline Hannah uses and return the reply plus cited sources."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    q = question.strip()
    if not q:
        return {"ok": False, "error": "Question required."}

    sources: list[dict[str, Any]] = []
    prefetch_result = _search_support_result(retriever, q)
    _record_support_sources(sources, prefetch_result, via="prefetch")
    wiki_context = format_support_knowledge_result(prefetch_result)
    system = build_support_chat_prompt(wiki_context=wiki_context)

    openai_messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": q},
    ]
    tools = support_tool_definitions()
    model = _chat_model()
    session = _ChatSessionStub()  # type: ignore[assignment]
    reply = ""
    tool_queries: list[str] = []

    async with httpx.AsyncClient(timeout=90.0) as client:
        for _ in range(4):
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": openai_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                reply = redact_support_contacts(str(choice.get("content") or "").strip())
                break

            openai_messages.append(choice)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "search_wiki":
                    query = str(args.get("query") or "").strip()
                    if query:
                        tool_queries.append(query)
                        tool_result = _search_support_result(retriever, query)
                        _record_support_sources(sources, tool_result, via="search_wiki")
                result = await executor.execute_tool(name, args, session)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result,
                    }
                )
        else:
            reply = (
                "I'm having trouble right now — let me log a ticket so a Hammer representative can reach out "
                "as soon as possible. Could you share your dealership name, your name, and the email on your "
                "Hammer account?"
            )

    return {
        "ok": True,
        "query": q,
        "response": reply,
        "model": model,
        "escalated": bool(session.escalated),
        "issue_category": session.issue_category or "",
        "tool_queries": tool_queries,
        "sources": sources,
    }


async def regenerate_support_response(
    retriever: Any,
    question: str,
    correct_info: str,
) -> dict[str, Any]:
    """Rewrite Hannah's customer-facing answer from admin-provided correct info.

    The admin supplies the verified facts needed to solve the issue; this composes
    a polished reply in Hannah's normal voice/format grounded on that info, so the
    reviewer only has to enter the correct answer and see the generated response.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    q = question.strip()
    info = correct_info.strip()
    if not q:
        return {"ok": False, "error": "Question required."}
    if not info:
        return {"ok": False, "error": "Enter the correct information first."}

    sources: list[dict[str, Any]] = []
    prefetch_result = _search_support_result(retriever, q)
    _record_support_sources(sources, prefetch_result, via="prefetch")
    wiki_context = format_support_knowledge_result(prefetch_result)
    system = build_support_chat_prompt(wiki_context=wiki_context)

    guidance = (
        "An admin has supplied the VERIFIED CORRECT INFORMATION needed to answer the "
        "customer's question below. Treat it as authoritative ground truth that overrides "
        "the knowledge excerpts and anything else if they conflict. Write Hannah's "
        "customer-facing reply to the question using this information, in Hannah's normal "
        "chat voice and formatting (concise; numbered steps when there is a procedure). "
        "Do not mention the admin, this instruction, that the answer was edited, or that you "
        "were given information. Do not ask for contact details and do not call any tools — "
        "just write the answer.\n\n"
        f"VERIFIED CORRECT INFORMATION:\n{info}"
    )

    model = _chat_model()
    messages = [
        {"role": "system", "content": system},
        {"role": "system", "content": guidance},
        {"role": "user", "content": q},
    ]

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        reply = redact_support_contacts(str(data["choices"][0]["message"].get("content") or "").strip())

    if not reply:
        return {"ok": False, "error": "Could not generate a response from that information."}

    return {
        "ok": True,
        "query": q,
        "response": reply,
        "model": model,
        "correct_info": info,
        "sources": sources,
    }
