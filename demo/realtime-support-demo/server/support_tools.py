"""Support voice tools — search_wiki, escalate_to_human, create_support_ticket."""



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



TICKET_CREATED_MESSAGE = (

    "Thanks — I've logged your request. A Hammer representative may follow up if needed."

)





def support_tool_definitions() -> list[dict[str, Any]]:

    from support_ticket_service import ticket_creation_enabled

    tools = [

        {

            "type": "function",

            "function": {

                "name": "search_wiki",

                "description": (

                    "Search Hammer support knowledge: official KB/wiki first, then related resolved Help Desk ticket cases. "

                    "Call for any product, policy, or how-to question before stating facts. "

                    "You may only tell the customer what appears in the returned excerpts."

                ),

                "parameters": {

                    "type": "object",

                    "properties": {

                        "query": {

                            "type": "string",

                            "description": "The customer's question or topic as they stated it.",

                        }

                    },

                    "required": ["query"],

                },

            },

        },

        {

            "type": "function",

            "function": {

                "name": "create_support_ticket",

                "description": (

                    "Create a HubSpot support ticket after you have collected ALL required contact fields. "

                    "Call exactly once per conversation before it ends — required even if you fully resolved "

                    "their issue in the knowledge base. Include a brief issue_summary of what they needed."

                ),

                "parameters": {

                    "type": "object",

                    "properties": {

                        "dealership_name": {

                            "type": "string",

                            "description": "Dealership name (required).",

                        },

                        "first_name": {"type": "string", "description": "Customer first name (required)."},

                        "last_name": {"type": "string", "description": "Customer last name (required)."},

                        "email": {

                            "type": "string",

                            "description": "Email — Hammer login email preferred (required).",

                        },

                        "phone": {

                            "type": "string",

                            "description": "Mobile number with country code, e.g. +15551234567 (required).",

                        },

                        "issue_summary": {

                            "type": "string",

                            "description": "Brief summary of the customer's question or issue.",

                        },

                        "resolved": {

                            "type": "boolean",

                            "description": "True if the KB fully resolved their issue in this session.",

                        },

                        "issue_category": {

                            "type": "string",

                            "description": "Optional: login, billing, integrations, dashboard, facebook-aia, marketposter, connect, other",

                        },

                    },

                    "required": [

                        "dealership_name",

                        "first_name",

                        "last_name",

                        "email",

                        "phone",

                        "issue_summary",

                    ],

                },

            },

        },

        {

            "type": "function",

            "function": {

                "name": "escalate_to_human",

                "description": (

                    "Escalate when search_wiki has no relevant excerpt for the question, the customer asks for a person, "

                    "or KB steps are done and account-specific verification is needed. Use this instead of guessing. "

                    "You must still call create_support_ticket in every session."

                ),

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
        {
            "type": "function",
            "function": {
                "name": "check_callback_calendar",
                "description": (
                    "Read the customer callback calendar to see times already booked for a day or range, "
                    "so you can avoid double-booking and offer/confirm a good time before scheduling. "
                    "Call this BEFORE schedule_callback when the customer proposes a time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "A single day to check, as YYYY-MM-DD.",
                        },
                        "start": {
                            "type": "string",
                            "description": "Optional ISO 8601 start of a range to list.",
                        },
                        "end": {
                            "type": "string",
                            "description": "Optional ISO 8601 end of a range to list.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "schedule_callback",
                "description": (
                    "Schedule a callback when a CURRENT Hammer customer asks for someone to reach out and "
                    "help them with their account at a specific time. Collect all required fields first. "
                    "Pass requested_time as a full ISO 8601 datetime (include the date, time, and timezone "
                    "offset if known, e.g. 2026-06-10T14:30:00-05:00). Confirm the time back to the customer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dealership_name": {"type": "string", "description": "Dealership name (required)."},
                        "first_name": {"type": "string", "description": "Customer first name (required)."},
                        "last_name": {"type": "string", "description": "Customer last name (required)."},
                        "phone": {
                            "type": "string",
                            "description": "Callback number with country code, e.g. +15551234567 (required).",
                        },
                        "email": {"type": "string", "description": "Email (optional but preferred)."},
                        "requested_time": {
                            "type": "string",
                            "description": "Desired callback time as ISO 8601, e.g. 2026-06-10T14:30:00-05:00 (required).",
                        },
                        "requested_time_label": {
                            "type": "string",
                            "description": "Plain-language time as the customer said it, e.g. 'Tuesday at 2:30pm CT'.",
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Customer timezone, e.g. America/Chicago or 'CT'.",
                        },
                        "duration_min": {
                            "type": "integer",
                            "description": "Expected length in minutes (default 30).",
                        },
                        "reason": {
                            "type": "string",
                            "description": "What the customer needs help with on the call (required).",
                        },
                    },
                    "required": [
                        "dealership_name",
                        "first_name",
                        "last_name",
                        "phone",
                        "requested_time",
                        "reason",
                    ],
                },
            },
        },

    ]

    if not ticket_creation_enabled():
        tools = [
            t for t in tools
            if t.get("function", {}).get("name") != "create_support_ticket"
        ]

    return tools





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


def _is_ticket_doc_id(doc_id: str) -> bool:

    return "hubspot-tickets/" in (doc_id or "").lower()


def _ticket_label(doc_id: str) -> str:

    name = str(doc_id or "").rsplit("/", 1)[-1].removesuffix(".md")

    ticket_id = name.split("-", 1)[0] if name else ""

    title = name.split("-", 1)[1].replace("-", " ") if "-" in name else name

    if ticket_id and ticket_id.isdigit():

        return f"Ticket #{ticket_id}: {title}".strip()

    return name or doc_id


def format_support_knowledge_result(result: dict[str, Any], *, max_chars: int = 4500) -> str:

    """Format structured retrieval so the model treats tickets as related cases."""

    lines: list[str] = []

    used = 0

    def add(block: str, *, limit: int | None = None) -> bool:

        nonlocal used

        cap = max_chars if limit is None else min(limit, max_chars)

        if used + len(block) > cap:

            remaining = cap - used

            if remaining >= 280:

                lines.append(block[: remaining - 18].rstrip() + "\n...[truncated]\n\n")

                used = cap

                return True

            return False

        lines.append(block)

        used += len(block)

        return True

    playbook = list(result.get("playbook") or [])

    official = list(result.get("official") or [])

    ticket_cases = list(result.get("ticket_cases") or [])

    if playbook:

        playbook_limit = int(max_chars * 0.5)

        add(
            "APPROVED ANSWERS (admin-verified — HIGHEST AUTHORITY: use these as the answer and prefer them over everything below if they conflict):\n"
        )

        for ch, score in playbook:

            block = f"[approved:{ch.doc_id} #{ch.chunk_id}]\n{ch.text.strip()}\n\n"

            if not add(block, limit=playbook_limit):

                break

    official_limit = int(max_chars * 0.58) if ticket_cases else max_chars

    if official:

        add(

            "OFFICIAL KNOWLEDGE (source of truth for product facts, steps, URLs, and policy):\n"

        )

        for ch, score in official:

            block = f"[official:{ch.doc_id} #{ch.chunk_id} score={score:.2f}]\n{ch.text.strip()}\n\n"

            if not add(block, limit=official_limit):

                break

    if ticket_cases:

        add(

            "RELATED RESOLVED HELP DESK TICKETS (similar prior cases; use as support evidence, not official policy):\n"

        )

        for idx, case in enumerate(ticket_cases, start=1):

            doc_id = str(case.get("doc_id") or "")

            header = f"Case {idx}: {_ticket_label(doc_id)} (case_score={float(case.get('score') or 0.0):.3f})\n"

            if not add(header):

                return "\n".join(lines).strip()

            for ch, score in case.get("chunks") or []:

                block = f"[ticket:{ch.doc_id} #{ch.chunk_id} score={float(score):.2f}]\n{ch.text.strip()}\n\n"

                if not add(block):

                    return "\n".join(lines).strip()

    return "\n".join(lines).strip()





@dataclass

class SupportSession:

    call_id: str = ""

    channel: str = "browser_voice"

    issue_category: str = ""

    escalated: bool = False

    resolved: bool = False

    ticket_created: bool = False

    hubspot_ticket_id: str = ""

    dealership_name: str = ""

    first_name: str = ""

    last_name: str = ""

    email: str = ""

    phone: str = ""

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



    async def execute_tool(self, name: str, arguments: dict[str, Any], session: SupportSession) -> str:

        if name == "create_support_ticket":

            from support_ticket_service import create_and_notify_ticket



            result = await create_and_notify_ticket(arguments, session=session)

            session.session_log.append(

                {

                    "tool": "create_support_ticket",

                    "ok": result.get("ok"),

                    "hubspot_ticket_id": result.get("hubspot_ticket_id"),

                }

            )

            return json.dumps(result)

        if name == "schedule_callback":
            from support_calendar import schedule_callback

            result = schedule_callback(arguments, session=session)
            session.session_log.append(
                {
                    "tool": "schedule_callback",
                    "ok": result.get("ok"),
                    "appointment_id": result.get("appointment_id"),
                }
            )
            return json.dumps(result)

        return self.execute(name, arguments, session)



    def execute(self, name: str, arguments: dict[str, Any], session: SupportSession) -> str:

        if name == "search_wiki":

            query = str(arguments.get("query") or "").strip()

            if not query:

                return json.dumps({"ok": False, "error": "query required"})

            retriever = self._get_retriever()

            if hasattr(retriever, "search_support_knowledge"):

                result = retriever.search_support_knowledge(

                    query,

                    official_k=4,

                    ticket_case_limit=4,

                    ticket_chunks_per_case=2,

                )

                excerpts = format_support_knowledge_result(result, max_chars=self._max_wiki_chars)

            else:

                pairs = retriever.top_k(query, k=8)

                excerpts = format_wiki_excerpts(pairs, max_chars=self._max_wiki_chars)

            session.session_log.append({"tool": "search_wiki", "query": query})

            return json.dumps(

                {

                    "ok": True,

                    "excerpts": excerpts

                    or (

                        "(no matches found for this query — try a different phrasing. "

                        "Do NOT invent an answer or escalate yet. Search again with synonyms "

                        "before concluding the KB has no answer.)"

                    ),

                }

            )



        if name == "create_support_ticket":

            return json.dumps(

                {

                    "ok": False,

                    "error": "create_support_ticket must be invoked via async execute_tool",

                }

            )



        if name == "check_callback_calendar":
            from support_calendar import callbacks_for_day, list_callbacks

            day = str(arguments.get("date") or "").strip()
            start = str(arguments.get("start") or "").strip()
            end = str(arguments.get("end") or "").strip()
            if day:
                result = callbacks_for_day(day)
            else:
                result = list_callbacks(start=start, end=end)
            session.session_log.append({"tool": "check_callback_calendar", "date": day or f"{start}..{end}"})
            return json.dumps(result)

        if name == "schedule_callback":
            return json.dumps(
                {
                    "ok": False,
                    "error": "schedule_callback must be invoked via async execute_tool",
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

            from support_ticket_service import ticket_creation_enabled

            escalation_payload = {

                "ok": True,

                "message": HUMAN_SUPPORT_MESSAGE,

                "issue_summary": summary,

                "issue_category": category,

            }

            if ticket_creation_enabled():

                escalation_payload["reminder"] = (
                    "You must still call create_support_ticket before the session ends."
                )

            return json.dumps(escalation_payload)



        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})


