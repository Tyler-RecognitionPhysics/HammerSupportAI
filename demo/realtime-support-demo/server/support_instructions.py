"""Support agent voice + chat instructions — CS persona."""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from zoneinfo import ZoneInfo

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_CS_PERSONA_PATH = _PROMPTS_DIR / "cs-persona.md"

VOICE_CHANNEL_RULES = """# Voice channel rules

- Your name is **Hannah**. You work at Hammer — use we, us, our for Hammer support, products, and policies.
- You are on a **live voice call** on the Hammer support site.
- **Session opening:** The moment the voice session connects, **you speak first** — one warm greeting, then pause for their question. Do not wait in silence for them to talk first.
- Keep replies **short and spoken** — one KB troubleshooting step at a time.
- Use **search_wiki** before answering product-specific questions; follow KB steps before offering escalation.
- Use **escalate_to_human** only when they ask for a person, KB has no answer, or KB says support must verify account-specific details after self-serve steps.
- Never collect payment card numbers or passwords."""

CHAT_CHANNEL_RULES = """# Chat channel rules

- Your name is **Hannah**. You work at Hammer — use we, us, our for Hammer support, products, and policies.
- You are on **live website text chat** — the customer is already messaging us.
- Default: **1–3 short sentences** (under 55 words) for simple questions; use **numbered steps** when the knowledge base provides troubleshooting.
- Plain text only — no markdown bullets unless they asked for a list.
- **Answer from the knowledge base first.** WIKI EXCERPTS and **search_wiki** take precedence for factual troubleshooting. Follow KB steps before suggesting escalation.
- Call **search_wiki** when you need more detail than the excerpts already in context.
- Use **escalate_to_human** only if: the customer explicitly asks for a person; search_wiki has no relevant answer; or the KB says support must verify something account-specific **after** you have given the self-serve steps and collected any details the KB lists (name, time, lead source).
- **Never** open with only "a representative will reach out" when the KB already explains their issue.
- **Never** tell them to call us — they are already chatting for support."""

SUPPORT_GREETING = (
    "Hi it's Hannah with Hammer — how can I help you today?"
)


@lru_cache(maxsize=1)
def _load_cs_persona_core() -> str:
    if _CS_PERSONA_PATH.is_file():
        return _CS_PERSONA_PATH.read_text(encoding="utf-8").strip()
    return "You are Hannah, Hammer's Customer Success Representative."


def format_working_hours_block() -> str:
    tz = ZoneInfo("America/Chicago")
    now = datetime.now(tz)
    weekday = now.weekday()
    is_open = weekday < 5 and 9 <= now.hour < 17
    status = "open" if is_open else "closed"
    hour = now.strftime("%I").lstrip("0") or "12"
    today_label = now.strftime("%A, %B %d, %Y")
    time_label = now.strftime(f"{hour}:%M %p").lower()
    today_name = now.strftime("%A")
    return (
        f"Current DateTime with Open/Closed status\n"
        f"Today is {today_label}. Right now is {time_label} Central. We are **{status}** now.\n"
        f"({today_name} is {'a business day' if weekday < 5 else 'the weekend'}; "
        f"regular hours Mon–Fri 9am–5pm Central.)"
    )


def _inject_working_hours(text: str) -> str:
    block = format_working_hours_block()
    if "[WORKING_HOURS]" in text:
        return text.replace("[WORKING_HOURS]", block)
    return f"{text.rstrip()}\n\n{block}"


def _prompt_override(key: str) -> str | None:
    try:
        from support_dashboard_store import get_all_settings

        val = get_all_settings().get(key)
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return None


def _default_voice_prompt() -> str:
    return f"{_load_cs_persona_core()}\n\n{VOICE_CHANNEL_RULES}"


def _default_chat_prompt() -> str:
    return f"{_load_cs_persona_core()}\n\n{CHAT_CHANNEL_RULES}"


def build_support_voice_prompt(*, wiki_context: str = "") -> str:
    base = _prompt_override("support_voice_prompt") or _default_voice_prompt()
    parts = [_inject_working_hours(base)]
    if wiki_context.strip():
        parts.append(
            "\n── WIKI EXCERPTS (authoritative for troubleshooting steps and product facts; follow these before escalating) ──\n"
        )
        parts.append(wiki_context.strip())
    return "\n".join(parts)


def build_support_chat_prompt(*, wiki_context: str = "") -> str:
    base = _prompt_override("support_chat_prompt") or _default_chat_prompt()
    parts = [_inject_working_hours(base)]
    if wiki_context.strip():
        parts.append(
            "\n── WIKI EXCERPTS (authoritative for troubleshooting steps and product facts; follow these before escalating) ──\n"
        )
        parts.append(wiki_context.strip())
    return "\n".join(parts)


def get_default_prompts() -> dict[str, str]:
    return {
        "support_voice_prompt": _default_voice_prompt(),
        "support_chat_prompt": _default_chat_prompt(),
        "support_greeting": SUPPORT_GREETING,
    }


WIKI_PREFETCH_QUERIES = (
    "password reset login",
    "dashboard inbox",
    "CRM integration setup",
    "Facebook AIA",
    "MarketPoster extension",
    "Hammer Connect messaging",
    "billing renewal",
    "demo scheduling",
    "cancellation refund",
    "not receiving leads lead delivery",
)
