"""Text support chat — wiki-grounded OpenAI completions."""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

import httpx

from support_instructions import build_support_chat_prompt
from support_tools import (
    SupportSession,
    SupportToolExecutor,
    format_support_knowledge_result,
    support_tool_definitions,
)
from wiki_retrieval import Chunk


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

    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    wiki_context = _support_context(retriever, last_user) if last_user.strip() else ""
    system = build_support_chat_prompt(wiki_context=wiki_context)

    openai_messages: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            openai_messages.append({"role": role, "content": content})

    tools = support_tool_definitions()
    model = _chat_model()
    if session is None:
        session = _ChatSessionStub()  # type: ignore

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
                return str(choice.get("content") or "").strip()

            openai_messages.append(choice)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await executor.execute_tool(name, args, session)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result,
                    }
                )

    return "I'm having trouble right now — a representative will reach out as soon as possible. You can also email support@hammertime.com."


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
                reply = str(choice.get("content") or "").strip()
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
                "I'm having trouble right now — a representative will reach out as soon as possible. "
                "You can also email support@hammertime.com."
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
        reply = str(data["choices"][0]["message"].get("content") or "").strip()

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
