"""Text support chat — wiki-grounded OpenAI completions."""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

import httpx

from support_instructions import build_support_chat_prompt
from support_tools import SupportToolExecutor, support_tool_definitions
from wiki_retrieval import Chunk


def _chat_model() -> str:
    return os.environ.get("SUPPORT_CHAT_MODEL", os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")).strip()


def _format_excerpts(chunks: Sequence[tuple[Chunk, float]]) -> str:
    lines = []
    for ch, sc in chunks:
        lines.append(f"[{ch.doc_id}] (score={sc:.2f})\n{ch.text.strip()}\n")
    return "\n".join(lines)


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
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    pairs = retriever.top_k(last_user, k=8) if last_user.strip() else []
    wiki_context = _format_excerpts(pairs)
    system = build_support_chat_prompt(wiki_context=wiki_context)

    openai_messages: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            openai_messages.append({"role": role, "content": content})

    tools = support_tool_definitions()
    model = _chat_model()
    session = _ChatSessionStub()

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
                    "temperature": 0.4,
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
                result = executor.execute(name, args, session)
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result,
                    }
                )

    return "I'm having trouble right now — a representative will reach out as soon as possible. You can also email support@hammertime.com."
