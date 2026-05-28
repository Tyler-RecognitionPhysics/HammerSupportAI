"""Support voice tools — search_wiki and escalate_to_human."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

_log = logging.getLogger(__name__)

HUMAN_SUPPORT_MESSAGE = (
    "I've flagged this for our team — a Hammer representative will reach out as soon as possible. "
    "If you need to follow up by email, use support@hammertime.com."
)


def support_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_wiki",
                "description": "Search Hammer customer support knowledge base for troubleshooting steps and answers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Support question or topic to search",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "escalate_to_human",
                "description": "Escalate ONLY when search_wiki has no relevant answer, the customer explicitly asks for a person, or KB troubleshooting is complete and support must verify account-specific details. Do NOT use when KB already has step-by-step steps you have not given yet.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_summary": {
                            "type": "string",
                            "description": "Brief summary of the customer's issue",
                        },
                        "issue_category": {
                            "type": "string",
                            "description": "Category: login, billing, integrations, dashboard, facebook-aia, marketposter, connect, other",
                        },
                    },
                    "required": ["issue_summary"],
                },
            },
        },
    ]


def format_wiki_excerpts(pairs: list[tuple[Any, float]], *, max_chars: int = 4500) -> str:
    lines: list[str] = []
    used = 0
    for ch, score in pairs:
        block = f"[{ch.doc_id} #{ch.chunk_id} score={score:.2f}]\n{ch.text.strip()}\n"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


@dataclass
class SupportSession:
    call_id: str = ""
    channel: str = "browser_voice"
    issue_category: str = ""
    escalated: bool = False
    resolved: bool = False
    session_log: list[dict[str, Any]] = field(default_factory=list)


class SupportToolExecutor:
    def __init__(self, get_retriever: Callable[[], Any], *, max_wiki_chars: int = 4500) -> None:
        self._get_retriever = get_retriever
        self._max_wiki_chars = max_wiki_chars
        self._prefetched_wiki: str | None = None

    def warm_wiki_context(self, queries: tuple[str, ...]) -> str:
        retriever = self._get_retriever()
        chunks: list[tuple[Any, float]] = []
        seen: set[tuple[str, int]] = set()
        for q in queries:
            for ch, sc in retriever.top_k(q, k=4):
                key = (ch.doc_id, ch.chunk_id)
                if key in seen:
                    continue
                seen.add(key)
                chunks.append((ch, sc))
        text = format_wiki_excerpts(chunks, max_chars=self._max_wiki_chars)
        self._prefetched_wiki = text
        return text

    def prefetched_wiki_context(self) -> str | None:
        return self._prefetched_wiki

    def execute(self, name: str, arguments: dict[str, Any], session: SupportSession) -> str:
        if name == "search_wiki":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return json.dumps({"ok": False, "error": "query required"})
            pairs = self._get_retriever().top_k(query, k=6)
            excerpts = format_wiki_excerpts(pairs, max_chars=self._max_wiki_chars)
            session.session_log.append({"tool": "search_wiki", "query": query})
            return json.dumps(
                {
                    "ok": True,
                    "excerpts": excerpts
                    or "(no matches — say you do not have that in the help articles and ask one clarifying question; escalate only if still stuck)",
                }
            )

        if name == "escalate_to_human":
            summary = str(arguments.get("issue_summary") or "").strip()
            category = str(arguments.get("issue_category") or "other").strip()
            session.escalated = True
            session.issue_category = category or session.issue_category
            session.session_log.append(
                {"tool": "escalate_to_human", "summary": summary, "category": category}
            )
            return json.dumps(
                {
                    "ok": True,
                    "message": HUMAN_SUPPORT_MESSAGE,
                    "issue_summary": summary,
                    "issue_category": category,
                }
            )

        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
