"""Support voice tools — search_wiki, escalate_to_human, create_support_ticket."""



from __future__ import annotations



import json

import logging

import re

from dataclasses import dataclass, field

from typing import Any, Callable


# Hammer support never tells a customer to email — we log a ticket instead. The
# wiki/KB still contains the public support inbox, so we strip it from anything
# we feed the model (it cannot repeat an address it never sees).
# Hammer's own domains. We strip support-style inboxes on these (the AI must
# never hand out an email); HR addresses (hr@, recruiting@) are intentionally
# preserved for the careers/employment-verification flows.
_SUPPORT_INBOX_RE = re.compile(
    r"\b(?:support|cancellations?|help|billing|info|contact|care|service|sales)@(?:hammertime|hammer-corp)\.com\b",
    re.IGNORECASE,
)
_EMAIL_VERB_INBOX_RE = re.compile(
    r"(?:you\s+can\s+)?"
    r"(?:e-?mail|contact|reach(?:\s+out)?(?:\s+to)?|write\s+to|message)\s+"
    r"(?:us|them|hammer(?:\s+support)?|our\s+(?:support\s+)?team|the\s+(?:support\s+)?team|support)?\s*"
    r"(?:(?:via|by)\s+e-?mail\s+)?"
    r"(?:at\s+)?"
    r"(?:support|cancellations?|help|billing|info|contact|care|service|sales)@(?:hammertime|hammer-corp)\.com",
    re.IGNORECASE,
)

# A callback may only be scheduled once the CUSTOMER has actually stated a day/time.
# The model otherwise invents one ("tomorrow at 2:30pm") and books it silently. This
# detects a real time/day expression in the customer's own words so we can hard-block
# schedule_callback when they haven't given one. Careful to avoid false hits like the
# "am" in "I am Tyler".
_USER_TIME_HINT_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b"  # 2:30, 14:30
    r"|\b\d{1,2}\s*(?:am|pm|a\.m\.?|p\.m\.?|o'?clock)\b"  # 2pm, 11 am, 3 o'clock
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b(?:tomorrow|today|tonight|noon|midnight|morning|afternoon|evening)\b"
    r"|\bnext\s+(?:week|mon|tues|wednes|thurs|fri|satur|sun)(?:day)?\b"
    r"|\bthis\s+(?:week|afternoon|morning|evening|mon|tues|wednes|thurs|fri|satur|sun)(?:day)?\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?:thirty|fifteen|forty[-\s]?five|o'?clock|am|pm)\b"
    r"|\b(?:half\s+past|quarter\s+(?:past|to))\b",
    re.IGNORECASE,
)

_NO_TIME_GUARD_RESULT = (
    "BLOCKED — do not schedule. The customer has NOT given a specific day or time for a "
    "callback yet, so you may not call schedule_callback. Ask them in your own words, e.g. "
    "\"When works best for someone to reach out to you?\", and wait for their answer. Only "
    "call schedule_callback after they state an actual day/time. If they say they have no "
    "preference, do not schedule anything — the support ticket alone is enough."
)


def customer_stated_a_time(messages: list[dict[str, Any]] | None) -> bool:
    """True if any *customer* (user) turn contains a concrete day/time expression."""
    if not messages:
        return False
    for m in messages:
        if m.get("role") != "user":
            continue
        if _USER_TIME_HINT_RE.search(str(m.get("content") or "")):
            return True
    return False


# After a ticket is logged for human follow-up, Hannah must offer to schedule a callback
# by asking for the customer's preferred time. The model does this inconsistently, so we
# enforce it deterministically.
_ASKS_FOR_TIME_RE = re.compile(
    r"\b(?:preferred|best)\b[^.?!]{0,40}\b(?:day|time)\b"
    r"|\bday\s+and\s+time\b"
    r"|\bwhat\s+time\b"
    r"|\bwhen\s+(?:works|would|is\s+best|is\s+good)\b"
    r"|\btime\s+(?:that\s+)?works\b"
    r"|\bwork(?:s)?\s+best\s+for\s+(?:you|someone)\b",
    re.IGNORECASE,
)

CALLBACK_TIME_PROMPT = (
    "Is there a particular day and time that works best for someone to reach out to you?"
)

# Generic closing pleasantries ("Is there anything else I can help with?", "feel free to
# ask!"). When we have to append the callback-time question, these must NOT come first —
# "...feel free to ask! Is there a particular day and time..." reads wrong — so we strip
# them from the end of the reply and let the time question be the closer.
_PLEASANTRY_CORE = (
    r"(?:is there )?anything else"
    r"|feel free to (?:ask|reach out|let (?:me|us) know)"
    r"|let (?:me|us) know if (?:you|there)"
    r"|if you (?:need|have) any(?:thing)?\s*(?:else|more|other|further questions)?"
    r"|any (?:other|further|more) questions"
    r"|further assistance"
    r"|happy to help with anything"
)

_TRAILING_PLEASANTRY_RE = re.compile(
    r"(?:^|(?<=[.?!]))\s*[^.?!]*(?:" + _PLEASANTRY_CORE + r")[^.?!]*[.?!]?\s*$",
    re.IGNORECASE,
)

_PLEASANTRY_SENTENCE_RE = re.compile(_PLEASANTRY_CORE, re.IGNORECASE)


def is_closing_pleasantry(sentence: str) -> bool:
    """True if this (single) sentence is a generic 'anything else?' style closer."""
    return bool(_PLEASANTRY_SENTENCE_RE.search(sentence or ""))


def append_callback_time_prompt(reply: str) -> str:
    """End the reply with the callback-time question, dropping any trailing
    'anything else?' closers so the question lands naturally as the final sentence."""
    text = (reply or "").strip()
    while True:
        trimmed = _TRAILING_PLEASANTRY_RE.sub("", text).strip()
        if trimmed == text:
            break
        text = trimmed
    return f"{text} {CALLBACK_TIME_PROMPT}".strip()


def _reply_or_history_asks_for_time(messages: list[dict[str, Any]] | None, reply: str) -> bool:
    if _ASKS_FOR_TIME_RE.search(reply or ""):
        return True
    for m in messages or []:
        if m.get("role") == "assistant" and _ASKS_FOR_TIME_RE.search(str(m.get("content") or "")):
            return True
    return False


def should_offer_callback_time(
    messages: list[dict[str, Any]] | None, session: Any, reply: str
) -> bool:
    """True when we must append the callback-time question to the reply.

    Fires only once per conversation: after a follow-up ticket exists, when the customer
    hasn't already given a time and nobody (model or a prior enforced turn) has asked yet.
    """
    if session is None or not getattr(session, "ticket_created", False):
        return False
    if getattr(session, "resolved", False):
        return False  # self-served, no human follow-up needed
    if customer_stated_a_time(messages):
        return False  # they already gave a time
    if _reply_or_history_asks_for_time(messages, reply):
        return False  # already asked
    return True


def _redact_support_contacts(text: str) -> str:
    """Strip the public Hammer support inbox from anything the model sees or says.

    Hammer support never hands out an email address — the AI logs a ticket
    instead. The wiki/KB still contains the public inbox and the model will even
    invent ``support@hammertime.com`` from the domain, so we sanitize both the
    retrieved excerpts AND the final response as a deterministic backstop.
    """
    if not text:
        return text
    text = _EMAIL_VERB_INBOX_RE.sub(
        "let me log a support ticket so a Hammer rep can reach out to you — no email needed", text
    )
    text = _SUPPORT_INBOX_RE.sub("the Hammer support team", text)
    return text


def redact_support_contacts(text: str) -> str:
    """Public wrapper so other modules can sanitize outgoing responses."""
    return _redact_support_contacts(text)



_log = logging.getLogger(__name__)



HUMAN_SUPPORT_MESSAGE = (

    "I've flagged this for our team and I'll log a ticket so a Hammer representative can reach out "

    "as soon as possible. Just so they have what they need, what's your dealership name, your name, "

    "and the email on your Hammer account?"

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

                            "description": "Mobile number with country code, e.g. +15551234567 (OPTIONAL — only if the customer offers it; never block the ticket waiting on a phone number).",

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

                            "description": "Optional: login, ai-responses, crm-leads, facebook-aia, inventory, sales-demo, billing, cancellation, other. Use 'billing' for charges/invoices/payments/refunds, 'cancellation' for any cancel/pause/downgrade/close-account request, 'crm-leads' for lead delivery/CRM/integration issues, and 'ai-responses' for questions about what the AI said to customers.",

                        },

                    },

                    "required": [

                        "dealership_name",

                        "first_name",

                        "last_name",

                        "email",

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

                            "description": "Category: login, ai-responses, crm-leads, facebook-aia, inventory, sales-demo, billing, cancellation, other",

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
                    "Schedule a callback for a CURRENT Hammer customer who needs a live rep to reach out. "
                    "ONLY call this AFTER the customer has told you their preferred day and time — never "
                    "invent, assume, or pick a time yourself. Always ask 'When works best for you?' first "
                    "and use what they say. Collect the required fields first. Pass requested_time as a full "
                    "ISO 8601 datetime (include the date, time, and timezone offset if known, e.g. "
                    "2026-06-10T14:30:00-05:00) and requested_time_label as the customer's own words. If their "
                    "requested time is already taken or outside business hours, this tool automatically books "
                    "the CLOSEST available time and returns it with adjusted=true — read the returned message "
                    "and confirm that exact time back to the customer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dealership_name": {"type": "string", "description": "Dealership name (required)."},
                        "first_name": {"type": "string", "description": "Customer first name (required)."},
                        "last_name": {"type": "string", "description": "Customer last name (required)."},
                        "phone": {
                            "type": "string",
                            "description": "Callback number with country code, e.g. +15551234567 (OPTIONAL — capture if offered; never block scheduling on it).",
                        },
                        "email": {"type": "string", "description": "Email — Hammer account email preferred (optional but preferred)."},
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

    return _redact_support_contacts("\n".join(lines))


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

                return _redact_support_contacts("\n".join(lines).strip())

            for ch, score in case.get("chunks") or []:

                block = f"[ticket:{ch.doc_id} #{ch.chunk_id} score={float(score):.2f}]\n{ch.text.strip()}\n\n"

                if not add(block):

                    return _redact_support_contacts("\n".join(lines).strip())

    return _redact_support_contacts("\n".join(lines).strip())





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


