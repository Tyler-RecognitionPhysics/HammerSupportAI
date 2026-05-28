"""
ElevenLabs Conversational AI — Custom LLM endpoint + signed-URL helper.

ElevenLabs handles STT and TTS (ElevenLabs voice quality); this module
drives GPT-4o with Hannah's full persona, wiki context, and all business
tool execution (Zapier agreement emails, Hammer Office account creation).

To wire this up:
1. Create an agent in ElevenLabs Conversational AI dashboard.
   - LLM: Custom LLM
   - Custom LLM URL: https://<your-server>/api/elevenlabs/llm
   - Voice: pick any ElevenLabs voice
   - First message: leave blank or a placeholder — the browser SDK sends the
     real pen-challenge opener via overrides.agent.firstMessage per call.
2. Set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID in server/.env
3. The browser calls GET /api/elevenlabs/token to get a signed WebSocket URL
   then passes it to Conversation.startSession({ signedUrl }).

All tool schemas match the SIP/phone path for pen challenge calls; browser Hammer
demo uses hammer_browser_tool_definitions (no pen-phase tools).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from voice_instructions import (
    EL_BROWSER_WIKI_PREFETCH,
    WIKI_PREFETCH_QUERIES,
    build_hammer_browser_prompt,
    build_hammer_knowledge_handoff,
    build_hammer_signup_handoff,
    build_pen_challenge_prompt,
    pick_pen_opening,
    format_austin_session_clock,
    prefetch_wiki_context,
    voice_anti_narration_rules,
)
from voice_tools import (
    CallSession,
    VoiceToolExecutor,
    derive_i_approve_verified_from_messages,
    derive_signup_context_from_messages,
    derive_visitor_claimed_i_approve,
    hammer_browser_chat_tool_definitions,
    hydrate_session_from_call_store,
    pen_challenge_chat_tool_definitions,
)
from agreement_approvals import voice_approve_on_call_enabled

_log = logging.getLogger(__name__)

# ── Module-level executor (warmed once, shared across requests) ──────────────
_el_executor: VoiceToolExecutor | None = None
_el_executor_lock = asyncio.Lock()


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


async def _prewarm_executor(get_retriever_fn: Callable) -> None:
    """Warm wiki + tool executor so the first custom-LLM turn responds before EL times out."""
    try:
        await prewarm_elevenlabs_session(get_retriever_fn)
    except Exception:
        _log.exception("elevenlabs_agent: prewarm failed")


async def _get_executor(get_retriever_fn: Callable, *, browser: bool = False) -> VoiceToolExecutor:
    """Return the shared VoiceToolExecutor, creating it on first call.

    Wiki context warming is intentionally fire-and-forget: blocking on it added
    7–10 s of TTFT on every cold Vercel instance. The full system prompt already
    contains all critical facts (pricing, timelines, etc.). Wiki context is
    additive and will be ready by the second request on the same instance.
    """
    global _el_executor
    if _el_executor is not None:
        return _el_executor
    async with _el_executor_lock:
        if _el_executor is None:
            executor = VoiceToolExecutor(get_retriever_fn)
            _el_executor = executor
            queries = EL_BROWSER_WIKI_PREFETCH if browser else WIKI_PREFETCH_QUERIES

            async def _warm_bg() -> None:
                started_at = time.perf_counter()
                try:
                    await asyncio.to_thread(executor.warm_wiki_context, queries)
                    _log.info(
                        "elevenlabs_agent: wiki context warmed queries=%s elapsed_ms=%s",
                        len(queries),
                        _elapsed_ms(started_at),
                    )
                except Exception:
                    _log.exception("elevenlabs_agent: background wiki warm failed")

            asyncio.create_task(_warm_bg())
    return _el_executor


async def prewarm_elevenlabs_session(get_retriever_fn: Callable) -> None:
    """Warm wiki + tools before the visitor speaks (called from token/prewarm endpoints)."""
    started_at = time.perf_counter()
    await _get_executor(get_retriever_fn, browser=True)
    _log.info("elevenlabs_agent: prewarm_session returned elapsed_ms=%s", _elapsed_ms(started_at))


def invalidate_executor_wiki() -> None:
    """Call when the wiki cache is cleared so the next request re-fetches context."""
    global _el_executor
    if _el_executor is not None:
        _el_executor._prefetched_wiki = None


# ── Environment helpers ──────────────────────────────────────────────────────

def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _el_api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _el_agent_id() -> str:
    return os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def _chat_model() -> str:
    try:
        from voice_dashboard_store import get_setting

        override = get_setting("chat_model")
        if isinstance(override, str) and override.strip():
            return override.strip()
    except Exception:
        pass
    return os.environ.get("ELEVENLABS_CHAT_MODEL", "gpt-4o-mini").strip()


def elevenlabs_configured() -> bool:
    return bool(_el_api_key() and _el_agent_id())


# ── Session state derivation from conversation history ───────────────────────

def _is_serverless() -> bool:
    return os.environ.get("REALTIME_SALES_SERVERLESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _llm_extra_body(body: dict) -> dict:
    """Extract custom LLM extra body from an ElevenLabs chat-completions request.

    ElevenLabs may send this as elevenlabs_extra_body (LLM proxy), custom_llm_extra_body
    (WebSocket initiation), or customLlmExtraBody depending on SDK/version/settings.
    """
    for key in ("elevenlabs_extra_body", "custom_llm_extra_body", "customLlmExtraBody"):
        extra = body.get(key)
        if isinstance(extra, dict) and extra:
            return extra
    return {}


def _initiation_client_data(body: dict) -> dict[str, Any]:
    """Conversation initiation payload (browser SDK or ElevenLabs phone/SIP)."""
    for container in (body, _llm_extra_body(body)):
        if not isinstance(container, dict):
            continue
        for key in ("conversation_initiation_client_data", "conversationInitiationClientData"):
            val = container.get(key)
            if isinstance(val, dict):
                return val
    return {}


def _is_elevenlabs_phone_call(body: dict) -> bool:
    """True for Twilio/SIP inbound or outbound — pen challenge, not browser Hammer demo."""
    extra = _llm_extra_body(body)

    channel = str(extra.get("channel") or extra.get("voice_channel") or "").lower()
    if channel in ("phone", "sip", "twilio", "telephony"):
        return True
    if channel in ("browser", "webrtc", "web", "widget"):
        return False

    source = str(
        body.get("conversation_initiation_source")
        or extra.get("conversation_initiation_source")
        or ""
    ).lower()
    if any(token in source for token in ("phone", "sip", "twilio")):
        return True
    if any(token in source for token in ("widget", "webrtc", "web")):
        return False

    direction = str(body.get("direction") or extra.get("direction") or "").lower()
    if direction in ("inbound", "outbound"):
        return True

    init = _initiation_client_data(body)
    dyn = init.get("dynamic_variables") or {}
    if isinstance(dyn, dict):
        if str(dyn.get("system__caller_id") or "").strip():
            return True
        try:
            from outbound_telephony import customer_phone_from_dynamic_variables

            if customer_phone_from_dynamic_variables(dyn):
                return True
        except ImportError:
            pass

    return False


def _elevenlabs_channel(body: dict, voice_scenario: str) -> str:
    if _is_elevenlabs_phone_call(body) or voice_scenario == "pen":
        return "phone"
    return "elevenlabs_browser"


def _voice_scenario(body: dict) -> str:
    """Resolve pen vs hammer script for this ElevenLabs session.

    - **Phone / SIP** (inbound demo number, Call-me outbound): always **pen** first.
    - **Browser WebRTC demo**: website SDK sends ``voice_scenario: hammer`` — use Hammer sales.
    - Missing ``voice_scenario`` in extra body means phone/SIP (pen), not browser.
    """
    if _is_elevenlabs_phone_call(body):
        return "pen"

    extra = _llm_extra_body(body)
    explicit = str(extra.get("voice_scenario") or "").strip().lower()
    if explicit == "hammer":
        return "hammer"
    if explicit in ("pen", "challenge"):
        # Browser misconfig — never run pen challenge on website voice demo.
        return "hammer"

    # No browser SDK marker → telephony (pen challenge).
    return "pen"


def _has_user_speech(messages: list[dict]) -> bool:
    """True once the visitor has said something substantive."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return True
    return False


def _is_opening_turn(messages: list[dict]) -> bool:
    """True on the very first LLM call — before the agent has spoken anything.

    ElevenLabs may send a non-empty user message on the initial call when
    initial_wait_time fires (a silence/timeout event). Checking for zero
    assistant messages is more reliable than checking for user speech.
    """
    return not any(m.get("role") == "assistant" for m in messages)


def _opening_greeting(voice_scenario: str) -> str:
    """Instant spoken greeting for the first custom-LLM turn (before any user speech)."""
    if voice_scenario == "pen":
        opening = pick_pen_opening()
        return opening.greeting.rstrip(".!?")
    return "Hey it's Hannah with Hammer — what's on your mind?"


def _call_id_from_body(body: dict) -> str:
    cid = str(body.get("conversation_id") or "").strip()
    if cid:
        return cid
    extra = _llm_extra_body(body)
    return str(extra.get("conversation_id") or "").strip()


def _messages_to_live_transcript(messages: list[dict]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": role, "message": content.strip()[:2000]})
    return turns[-50:]


def _persist_live_transcript(
    session: CallSession,
    messages: list[dict],
    *,
    pending_agent: str = "",
) -> None:
    call_id = session.call_id or session.lead.call_id
    if not call_id:
        return
    turns = _messages_to_live_transcript(messages)
    pending = pending_agent.strip()
    if pending:
        if not turns or turns[-1].get("role") != "assistant" or turns[-1].get("message") != pending:
            turns.append({"role": "assistant", "message": pending[:2000]})
    log_lines: list[str] = []
    for turn in turns:
        label = "Visitor" if turn["role"] == "user" else "Agent"
        log_lines.append(f"{label}: {turn['message']}")
    session.lead.session_log = log_lines[-80:]

    def _db_work():
        try:
            from voice_dashboard_store import update_active_session, upsert_call_record

            upsert_call_record(session.lead)
            update_active_session(
                call_id,
                {
                    "transcript": turns,
                    "scenario": session.voice_scenario or "",
                    "channel": session.lead.channel or "browser",
                    "values": dict(session.lead.values or {}),
                },
            )
        except Exception:
            pass

    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_db_work))
    except RuntimeError:
        _db_work()


def _track_elevenlabs_session(session: CallSession, body: dict, voice_scenario: str) -> None:
    call_id = _call_id_from_body(body) or session.call_id
    if not call_id:
        return
    session.call_id = call_id
    session.lead.call_id = call_id
    session.lead.channel = session.lead.channel or _elevenlabs_channel(body, voice_scenario)
    session.lead.touch_started()

    def _db_work():
        try:
            from voice_dashboard_store import (
                append_call_event,
                get_call_record_only,
                register_active_session,
                upsert_call_record,
            )

            is_new = get_call_record_only(call_id) is None
            register_active_session(
                call_id,
                {"scenario": voice_scenario, "channel": session.lead.channel},
            )
            upsert_call_record(session.lead)
            if is_new:
                append_call_event(
                    call_id=call_id,
                    event_type="call_started",
                    detail={
                        "scenario": voice_scenario,
                        "channel": session.lead.channel,
                    },
                )
        except Exception:
            pass

    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_db_work))
    except RuntimeError:
        _db_work()


def _derive_session(messages: list[dict]) -> CallSession:
    """
    Re-construct per-call state from the message history (stateless approach).
    ElevenLabs sends the full history on every turn so we can derive state
    without an external session store.
    """
    # Pre-index tool results by tool_call_id so we can check whether each
    # tool call succeeded — critical for idempotency of capture_lead.
    tool_results: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = str(msg.get("tool_call_id") or "")
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "") if isinstance(c, dict) else c) for c in content
                )
            if tc_id:
                tool_results[tc_id] = str(content)

    session = CallSession()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fname = (tc.get("function") or {}).get("name", "")
            tc_id = str(tc.get("id") or "")
            try:
                fargs = json.loads((tc.get("function") or {}).get("arguments", "{}") or "{}")
            except Exception:
                fargs = {}
            if fname == "begin_hammer_signup":
                session.pen_hammer_close_active = True
                session.pen_buyer_product = str(fargs.get("buyer_product", "") or "").strip()
            elif fname == "skip_pen_challenge" and fargs.get("visitor_confirmed_skip"):
                session.pen_hammer_close_active = True
                session.pen_challenge_skipped = True
            elif fname == "set_buyer_product":
                if session.pen_hammer_close_active:
                    session.pen_buyer_product = str(fargs.get("product", "") or "").strip()
            elif fname == "capture_lead":
                candidate = str(fargs.get("email", "") or "").strip().lower()
                if candidate and "@" in candidate:
                    session.agreement_email = candidate
                dealer = str(fargs.get("dealership_name", "") or "").strip()
                if dealer:
                    session.agreement_dealership = dealer
                plan = str(fargs.get("selected_plan", "") or "").strip()
                if plan:
                    session.agreement_plan = plan
                lot = str(fargs.get("lot_size", "") or "").strip()
                if lot:
                    session.agreement_lot_size = lot
                # Mark sent if the tool result confirms the email was queued.
                # This is the primary idempotency guard — it works across all
                # serverless instances because ElevenLabs resends the full
                # conversation history on every request.
                result = tool_results.get(tc_id, "")
                if (
                    result.startswith("ok —")
                    or result == "ok — lead sent to Zapier"
                    or "agreement email queued" in result
                    or "already sent" in result
                ):
                    session.capture_lead_sent = True
            elif fname == "book_appointment":
                result = tool_results.get(tc_id, "")
                if "booked" in result and result.startswith("ok —"):
                    appt_time = str(fargs.get("date", "") or "") + " " + str(fargs.get("time", "") or "")
                    session.appointment_time = appt_time.strip() or "booked"
    session.apply_signup_context(derive_signup_context_from_messages(messages))
    session.i_approve_verified = derive_i_approve_verified_from_messages(messages)
    if (
        not session.i_approve_verified
        and session.agreement_email
        and derive_visitor_claimed_i_approve(messages)
        and voice_approve_on_call_enabled()
    ):
        from agreement_approvals import agreement_approval_status, ensure_voice_call_approval

        pending = agreement_approval_status(session.agreement_email, wait_seconds=0)
        if pending.get("pending"):
            approved = ensure_voice_call_approval(session.agreement_email)
            session.i_approve_verified = bool(approved.get("approved"))
    return session


# ── System prompt construction ───────────────────────────────────────────────

def _session_state_block(session: CallSession) -> str:
    """Return a reminder block injected into the system prompt so GPT remembers
    key call state even when the relevant tool calls scroll out of the trimmed
    context window."""
    lines: list[str] = []
    email = (session.agreement_email or "").strip().lower()
    try:
        from agreement_approvals import agreement_email_already_queued
    except Exception:
        agreement_email_already_queued = None  # type: ignore[assignment,misc]
    has_agreement_context = bool(
        session.capture_lead_sent
        or (email and agreement_email_already_queued and agreement_email_already_queued(email))
    )
    if has_agreement_context and email:
        session.agreement_email = email
        lines.append(
            f"Agreement email ALREADY SENT to: {email}"
            + (f" ({session.agreement_dealership})" if session.agreement_dealership else "")
            + ". Do NOT call capture_lead again unless the visitor gives a corrected email address."
            + " YOU handle full signup on this call — NEVER say a live sales rep will reach out or finish signup."
            + " I approve confirmed ≠ account created — collect ALL Phase B fields (name, business structure, phone, website, address)"
            + " and wait for fill_hammer_account_field account created before Welcome to Hammer."
        )
        lines.append(
            "REMINDER: this is the website voice AI demo. NO live rep will reach out. NEVER say 'a live sales rep will reach out,'"
            " 'a Hammer rep will follow up,' 'someone will call you,' or any human-handoff phrasing. "
            "If they ask whether someone will reach out, answer: 'No need — I can finish your signup right now on this call.'"
        )
        try:
            from agreement_approvals import agreement_approval_status
            from hammer_office_session import (
                account_already_created,
                get_phase_b_missing_fields,
                signup_ready_for_phase_c,
            )

            approval = agreement_approval_status(session.agreement_email, wait_seconds=0)
            created, _account_url = account_already_created(session.agreement_email)
            missing = get_phase_b_missing_fields(session.agreement_email)
            approved_now = bool(approval.get("approved") or session.i_approve_verified)
            if approved_now:
                lines.append(
                    "I APPROVE ALREADY VERIFIED for this agreement email. "
                    "Do NOT ask the visitor to reply I approve again. Do NOT say approval was not received. "
                    "Do NOT re-confirm the email spelling. Never re-ask Phase B fields already collected on this call."
                )
            if created or signup_ready_for_phase_c(session.agreement_email):
                lines.append(
                    "HAMMER ACCOUNT ALREADY CREATED for this email. If the visitor asks whether it was created, answer yes. "
                    "Next step is PHASE C.1 only: ask whether the Welcome to Hammer email arrived. "
                    "Never return to agreement approval, email spelling, or Phase B collection unless they correct the email."
                )
            elif approval.get("approved") and missing:
                lines.append(
                    "Agreement is approved but account is NOT created yet. Continue PHASE B only; still need: "
                    + ", ".join(missing)
                    + ". Ask one missing field at a time, then call fill_hammer_account_field."
                )
            elif approved_now and not missing:
                lines.append(
                    "Agreement is approved and Phase B fields appear complete, but account is not marked created yet. "
                    "Submit/create the account now with fill_hammer_account_field or create_hammer_account. "
                    "Do NOT ask for I approve again and do NOT ask whether Welcome email arrived yet."
                )
        except Exception as exc:
            _log.debug("session state signup hydration skipped for %s: %s", session.agreement_email, exc)
    if session.appointment_time:
        lines.append(f"Appointment already booked: {session.appointment_time}. Do NOT call book_appointment again.")
    if not lines:
        return ""
    return "── SESSION STATE (authoritative) ──\n" + "\n".join(lines)


def _build_prompt(session: CallSession, wiki_context: str, voice_scenario: str) -> str:
    """Build the full Hannah system prompt for this turn based on conversation state."""
    state = _session_state_block(session)

    if voice_scenario == "hammer":
        base = build_hammer_browser_prompt(wiki_context)
        return f"{base}\n\n{state}" if state else base

    clock = format_austin_session_clock()
    anti_narration = voice_anti_narration_rules()

    if not session.pen_hammer_close_active:
        base = build_pen_challenge_prompt(wiki_context)
        return f"{base}\n\n{state}" if state else base

    if session.pen_challenge_skipped:
        base = (
            f"{anti_narration}\n\n"
            f"{build_hammer_knowledge_handoff(wiki_context)}\n\n"
            f"{clock}"
        )
        return f"{base}\n\n{state}" if state else base

    # Pen won → Hammer signup mode
    base = (
        f"{anti_narration}\n\n"
        f"{build_hammer_signup_handoff(session.pen_buyer_product, wiki_context, is_browser=session.is_browser_call())}\n\n"
        f"{clock}"
    )
    return f"{base}\n\n{state}" if state else base


def _inject_system(messages: list[dict], prompt: str) -> list[dict]:
    """Replace the ElevenLabs-provided system message with Hannah's full prompt."""
    filtered = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": prompt}] + filtered


def _trim_history(messages: list[dict], max_turns: int = 12) -> list[dict]:
    """Keep the most recent `max_turns` non-system messages.

    On long calls ElevenLabs sends the full conversation history, which balloons
    the token count and adds latency on every turn. Keeping the last N turns is
    sufficient for any realistic sales conversation while keeping costs and TTFT low.
    """
    non_system = [m for m in messages if m.get("role") != "system"]
    return non_system[-max_turns:]


# ── GPT tool-execution loop ──────────────────────────────────────────────────

async def _exec_tool(
    executor: VoiceToolExecutor,
    session: CallSession,
    name: str,
    arguments_str: str,
) -> str:
    try:
        args = json.loads(arguments_str or "{}")
    except Exception:
        args = {}
    started_at = time.perf_counter()
    result = await asyncio.to_thread(executor.execute, session, name, args)
    elapsed_tool = _elapsed_ms(started_at)
    _log.info("elevenlabs_tool name=%s elapsed_ms=%s", name, elapsed_tool)
    call_id = session.call_id or session.lead.call_id
    if call_id:
        try:
            from voice_dashboard_store import append_call_event
            append_call_event(
                call_id=call_id,
                event_type="latency",
                detail={"phase": f"tool_{name}", "elapsed_ms": elapsed_tool},
            )
        except Exception:
            pass
    return result


def _sse_role_chunk(chunk_id: str, created: int, model: str) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_content_chunk(chunk_id: str, created: int, model: str, content: str) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _stream_gpt_sse(
    messages: list[dict],
    tools: list[dict],
    api_key: str,
    model: str,
    executor: VoiceToolExecutor,
    session: CallSession,
    *,
    chunk_id: str | None = None,
    emit_initial_role: bool = True,
) -> AsyncIterator[bytes]:
    """
    Stream GPT output to ElevenLabs as OpenAI SSE chunks.

    ElevenLabs closes the call if the custom LLM endpoint does not emit SSE data
    quickly — we must not buffer the full reply (or tool loop) before streaming.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    working = list(messages)
    chunk_id = chunk_id or f"chatcmpl-el-{int(time.time())}"
    created = int(time.time())

    if emit_initial_role:
        yield _sse_role_chunk(chunk_id, created, model)

    for _iteration in range(8):
        text_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}

        gpt_started_at = time.perf_counter()
        first_content_logged = False
        stream = await client.chat.completions.create(
            model=model,
            messages=working,
            tools=tools,
            tool_choice="auto",
            max_tokens=250,
            stream=True,
        )

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta
            if delta.content:
                if not first_content_logged:
                    first_content_logged = True
                    elapsed_gpt = _elapsed_ms(gpt_started_at)
                    _log.info("elevenlabs_gpt first_content elapsed_ms=%s", elapsed_gpt)
                    call_id = session.call_id or session.lead.call_id
                    if call_id:
                        try:
                            from voice_dashboard_store import append_call_event
                            append_call_event(
                                call_id=call_id,
                                event_type="latency",
                                detail={"phase": "first_gpt_token", "elapsed_ms": elapsed_gpt},
                            )
                        except Exception:
                            pass
                text_parts.append(delta.content)
                payload = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": delta.content}, "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    i = tc.index
                    if i not in tool_acc:
                        tool_acc[i] = {"id": "", "name": "", "arguments": ""}
                    entry = tool_acc[i]
                    if tc.id:
                        entry["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] += tc.function.name
                        if tc.function.arguments:
                            entry["arguments"] += tc.function.arguments

        if not tool_acc:
            final_text = "".join(text_parts)
            async def _bg_post_reply_hooks():
                try:
                    auto = await asyncio.to_thread(
                        executor.ensure_agreement_email_queued,
                        session,
                        final_text,
                    )
                    if auto:
                        _log.warning("agreement email auto-queued after no-tool reply: %s", auto[:160])
                except Exception:
                    _log.exception("ensure_agreement_email_queued background task failed")

                try:
                    account_auto = await asyncio.to_thread(
                        executor.ensure_account_fields_recorded,
                        session,
                        final_text,
                    )
                    if account_auto:
                        _log.warning("account fields auto-recorded after no-tool reply: %s", account_auto[:180])
                except Exception:
                    _log.exception("ensure_account_fields_recorded background task failed")

            asyncio.create_task(_bg_post_reply_hooks())

            _persist_live_transcript(session, messages, pending_agent=final_text)
            done = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            return

        # GPT chose tools with no spoken text — continue tool loop silently.
        # (Role chunk already sent; avoid spoken filler that adds TTS latency.)

        final_text = "".join(text_parts)
        tc_list = [
            {
                "id": tool_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_acc[i]["name"],
                    "arguments": tool_acc[i]["arguments"],
                },
            }
            for i in sorted(tool_acc)
        ]
        working.append({"role": "assistant", "content": final_text or None, "tool_calls": tc_list})

        tc_values = [tool_acc[i] for i in sorted(tool_acc)]
        results = await asyncio.gather(
            *[_exec_tool(executor, session, tc["name"], tc["arguments"]) for tc in tc_values]
        )
        for tc_info, result in zip(tc_values, results):
            working.append({
                "role": "tool",
                "tool_call_id": tc_info["id"],
                "content": result,
            })

    _log.warning("elevenlabs_agent: tool loop hit iteration limit")
    async for chunk in _sse_chunks(
        "Something went wrong on my end — could you repeat that?",
        model,
        chunk_id=chunk_id,
    ):
        yield chunk


async def _gpt_loop(
    messages: list[dict],
    tools: list[dict],
    api_key: str,
    model: str,
    executor: VoiceToolExecutor,
    session: CallSession,
) -> str:
    """
    Drive GPT through as many tool-call rounds as needed and return the final
    spoken text.  All tool executions happen server-side before we stream.
    """
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key)
    working = list(messages)

    for _iteration in range(8):  # safety limit
        text_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}

        stream = await client.chat.completions.create(
            model=model,
            messages=working,
            tools=tools,
            tool_choice="auto",
            max_tokens=250,
            stream=True,
        )

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta
            if delta.content:
                text_parts.append(delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    i = tc.index
                    if i not in tool_acc:
                        tool_acc[i] = {"id": "", "name": "", "arguments": ""}
                    entry = tool_acc[i]
                    if tc.id:
                        entry["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] += tc.function.name
                        if tc.function.arguments:
                            entry["arguments"] += tc.function.arguments

        final_text = "".join(text_parts)

        if not tool_acc:
            async def _bg_post_reply_hooks():
                try:
                    auto = await asyncio.to_thread(
                        executor.ensure_agreement_email_queued,
                        session,
                        final_text,
                    )
                    if auto:
                        _log.warning("agreement email auto-queued after no-tool reply: %s", auto[:160])
                except Exception:
                    pass

                try:
                    account_auto = await asyncio.to_thread(
                        executor.ensure_account_fields_recorded,
                        session,
                        final_text,
                    )
                    if account_auto:
                        _log.warning("account fields auto-recorded after no-tool reply: %s", account_auto[:180])
                except Exception:
                    pass

            asyncio.create_task(_bg_post_reply_hooks())
            return final_text  # no tool calls — this is the spoken response

        # Append assistant message with tool calls
        tc_list = [
            {
                "id": tool_acc[i]["id"],
                "type": "function",
                "function": {
                    "name": tool_acc[i]["name"],
                    "arguments": tool_acc[i]["arguments"],
                },
            }
            for i in sorted(tool_acc)
        ]
        working.append({"role": "assistant", "content": final_text or None, "tool_calls": tc_list})

        # Execute tools concurrently then append results
        tc_values = [tool_acc[i] for i in sorted(tool_acc)]
        results = await asyncio.gather(
            *[_exec_tool(executor, session, tc["name"], tc["arguments"]) for tc in tc_values]
        )
        for tc_info, result in zip(tc_values, results):
            working.append({
                "role": "tool",
                "tool_call_id": tc_info["id"],
                "content": result,
            })

    _log.warning("elevenlabs_agent: tool loop hit iteration limit")
    return "Something went wrong on my end — could you repeat that?"


async def _sse_chunks(
    text: str,
    model: str,
    *,
    chunk_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Stream text as OpenAI-format SSE (used for fallbacks)."""
    chunk_id = chunk_id or f"chatcmpl-el-{int(time.time())}"
    created = int(time.time())

    role_payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_payload)}\n\n".encode()

    chunk_size = 8  # small chunks — ElevenLabs starts TTS sooner

    for start in range(0, max(len(text), 1), chunk_size):
        piece = text[start : start + chunk_size]
        if not piece:
            break
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(payload)}\n\n".encode()

    done = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done)}\n\n".encode()
    yield b"data: [DONE]\n\n"


# ── Public handlers (called from app.py) ────────────────────────────────────

async def handle_elevenlabs_llm(body: dict, get_retriever_fn: Callable) -> StreamingResponse:
    """
    POST /api/elevenlabs/llm — ElevenLabs custom LLM endpoint.

    Receives OpenAI-format messages, injects Hannah's system prompt + wiki
    context, runs GPT-4o with tool execution, streams text back to ElevenLabs
    which converts it to speech.
    """
    request_started_at = time.perf_counter()
    api_key = _openai_key()
    if not api_key:
        raise HTTPException(503, "OPENAI_API_KEY not configured on server")

    messages: list[dict] = body.get("messages", [])
    model = _chat_model()
    voice_scenario = _voice_scenario(body)
    extra = _llm_extra_body(body)
    _log.info(
        "elevenlabs_llm start scenario=%s extra_voice_scenario=%s messages=%s opening=%s",
        voice_scenario,
        extra.get("voice_scenario") or "(missing)",
        len(messages),
        _is_opening_turn(messages),
    )

    # Opening turn: respond in milliseconds so ElevenLabs does not drop the call.
    if _is_opening_turn(messages):
        greeting = _opening_greeting(voice_scenario)
        call_id = _call_id_from_body(body)
        if call_id:
            opener = CallSession()
            opener.call_id = call_id
            opener.lead.call_id = call_id
            opener.voice_scenario = voice_scenario
            opener.lead.channel = opener.lead.channel or _elevenlabs_channel(body, voice_scenario)
            # _track_elevenlabs_session now emits call_started on first registration.
            _track_elevenlabs_session(opener, body, voice_scenario)
            _persist_live_transcript(opener, [], pending_agent=greeting)

        async def _instant_open() -> AsyncIterator[bytes]:
            first = True
            async for chunk in _sse_chunks(greeting, model):
                if first:
                    first = False
                    elapsed_sse = _elapsed_ms(request_started_at)
                    _log.info(
                        "elevenlabs_llm first_sse opening=true elapsed_ms=%s",
                        elapsed_sse,
                    )
                    if call_id:
                        try:
                            from voice_dashboard_store import append_call_event
                            append_call_event(
                                call_id=call_id,
                                event_type="latency",
                                detail={"phase": "first_sse_opening", "elapsed_ms": elapsed_sse},
                            )
                        except Exception:
                            pass
                yield chunk

        return StreamingResponse(
            _instant_open(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Derive per-call session state from history (stateless)
    session = _derive_session(messages)
    session.voice_scenario = voice_scenario
    session.lead.channel = session.lead.channel or _elevenlabs_channel(body, voice_scenario)
    call_id = _call_id_from_body(body) or session.call_id
    if call_id:
        hydrate_session_from_call_store(session, call_id)
    _track_elevenlabs_session(session, body, voice_scenario)
    _persist_live_transcript(session, messages)
    chunk_id = f"chatcmpl-el-{int(time.time())}"

    async def _generate() -> AsyncIterator[bytes]:
        # CRITICAL: yield immediately — Vercel cold starts can spend several seconds
        # warming wiki context before GPT runs. ElevenLabs cascade_timeout fires if
        # no SSE bytes arrive, which looks like the AI "can't hear" after the intro.
        yield _sse_role_chunk(chunk_id, int(time.time()), model)
        elapsed_sse = _elapsed_ms(request_started_at)
        _log.info(
            "elevenlabs_llm first_sse opening=false elapsed_ms=%s",
            elapsed_sse,
        )
        if call_id:
            try:
                from voice_dashboard_store import append_call_event
                append_call_event(
                    call_id=call_id,
                    event_type="latency",
                    detail={"phase": "first_sse", "elapsed_ms": elapsed_sse},
                )
            except Exception:
                pass
        try:
            executor_started_at = time.perf_counter()
            executor = await _get_executor(get_retriever_fn, browser=(voice_scenario == "hammer"))
            _log.info("elevenlabs_llm executor_ready elapsed_ms=%s", _elapsed_ms(executor_started_at))
            wiki_context = executor.prefetched_wiki_context() or ""
            prompt_started_at = time.perf_counter()
            prompt = _build_prompt(session, wiki_context, voice_scenario)
            _log.info(
                "elevenlabs_llm prompt_built chars=%s wiki_chars=%s elapsed_ms=%s",
                len(prompt),
                len(wiki_context),
                _elapsed_ms(prompt_started_at),
            )
            # Keep last 12 conversational turns so the context window doesn't bloat
            # on long calls — system message is always prepended after truncation.
            trimmed = _trim_history(messages, max_turns=12)
            full_messages = _inject_system(trimmed, prompt)
            tools = (
                pen_challenge_chat_tool_definitions()
                if voice_scenario == "pen"
                else hammer_browser_chat_tool_definitions()
            )
            async for chunk in _stream_gpt_sse(
                full_messages,
                tools,
                api_key,
                model,
                executor,
                session,
                chunk_id=chunk_id,
                emit_initial_role=False,
            ):
                yield chunk
        except Exception as exc:
            _log.exception("elevenlabs_agent: request failed")
            error_text = (
                "I'm having a technical issue right now — could you try again in a moment?"
            )
            _log.debug("Falling back to error text due to: %s", exc)
            yield _sse_content_chunk(chunk_id, int(time.time()), model, error_text)
            done = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done)}\n\n".encode()
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_elevenlabs_token(get_retriever_fn: Callable | None = None) -> dict:
    """
    GET /api/elevenlabs/token — return a short-lived WebRTC conversation token.

    The browser uses WebRTC (LiveKit) which uses the native browser audio stack.
    This reliably captures the microphone in all browsers, unlike the WebSocket
    path which depends on AudioWorklet and can fail silently in some environments.

    Token is only generated when the user clicks the mic button (no pre-warming),
    which prevents hitting the ElevenLabs concurrent-session rate limit from
    speculative background token fetches.
    """
    api_key = _el_api_key()
    agent_id = _el_agent_id()
    if not api_key or not agent_id:
        raise HTTPException(
            503,
            "ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set on the server. "
            "Add them to server/.env or Vercel environment variables.",
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://api.elevenlabs.io/v1/convai/conversation/token",
            params={"agent_id": agent_id},
            headers={"xi-api-key": api_key},
        )

    if r.status_code == 429:
        _log.warning("ElevenLabs token rate limited (429): %s", r.text[:200])
        raise HTTPException(
            429,
            "ElevenLabs rate limit — too many active voice sessions. "
            "Wait 30–60 seconds and try again.",
        )

    if not r.is_success:
        _log.error("ElevenLabs token error: HTTP %s — %s", r.status_code, r.text[:300])
        raise HTTPException(502, f"ElevenLabs API returned HTTP {r.status_code}")

    data = r.json()
    token = (data.get("token") or "").strip()
    if not token:
        raise HTTPException(502, "ElevenLabs response missing token field")

    if get_retriever_fn is not None:
        asyncio.create_task(_prewarm_executor(get_retriever_fn))

    return {"conversation_token": token}


# ── Post-call webhook (ElevenLabs → Zapier call summary) ────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+?1?\s*[-.]?\s*\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_NAME_RE  = re.compile(
    r"(?:my name is|I(?:'m| am)|this is|it's|its)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
    re.IGNORECASE,
)
_DEALER_RE = re.compile(
    r"(?:at|from|with|here at|work(?:ing)? at|calling from)\s+([A-Z][A-Za-z0-9\s'&]{2,40}(?:dealer(?:ship)?|auto|motors?|ford|chevy|chevrolet|honda|toyota|kia|hyundai|nissan|bmw|mazda|dodge|ram|jeep|chrysler))",
    re.IGNORECASE,
)


def _verify_el_webhook_signature(raw_body: bytes, sig_header: str, secret: str) -> bool:
    """Verify ElevenLabs HMAC-SHA256 webhook signature (t=<ts>,v1=<hex>)."""
    try:
        parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p)}
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")
        if not timestamp or not signature:
            return False
        signed = f"{timestamp}.{raw_body.decode('utf-8', errors='replace')}"
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


def _build_accumulator_from_el_transcript(data: dict) -> "VoiceCallLeadAccumulator":
    """Build a VoiceCallLeadAccumulator from an ElevenLabs post_call_transcription payload."""
    from voice_call_summary import VoiceCallLeadAccumulator

    acc = VoiceCallLeadAccumulator()
    acc.call_id = data.get("conversation_id", "")

    # Determine channel from custom_llm_extra_body.voice_scenario
    init_data = data.get("conversation_initiation_client_data") or {}
    extra_body = init_data.get("custom_llm_extra_body") or {}
    scenario = str(extra_body.get("voice_scenario", "")).strip().lower()
    acc.channel = "elevenlabs-phone" if not scenario else f"elevenlabs-{scenario}"

    # Phone + direction: Call-me outbound vs true inbound (same rules as OpenAI SIP)
    dyn = init_data.get("dynamic_variables") or {}
    caller_id = str(dyn.get("system__caller_id") or "").strip()
    resolved_phone = ""
    try:
        from outbound_telephony import (
            customer_phone_from_dynamic_variables,
            resolve_sip_caller_for_summary,
        )

        customer_phone = customer_phone_from_dynamic_variables(dyn)
        if customer_phone:
            resolved_phone = customer_phone
            acc.call_direction = "outbound"
        elif caller_id:
            resolved_phone, acc.call_direction = resolve_sip_caller_for_summary(caller_id)
    except ImportError:
        if caller_id:
            resolved_phone = caller_id
            acc.call_direction = "inbound"

    if resolved_phone:
        acc.set_value("phone", resolved_phone)

    # Timestamps from metadata
    meta = data.get("metadata") or {}
    start_ts = meta.get("start_time_unix_secs")
    if start_ts:
        acc.started_at = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(timespec="seconds")
        dur = meta.get("call_duration_secs") or 0
        acc.ended_at = datetime.fromtimestamp(start_ts + dur, tz=timezone.utc).isoformat(timespec="seconds")

    # Interaction summary from ElevenLabs analysis
    analysis = data.get("analysis") or {}
    summary = (analysis.get("transcript_summary") or "").strip()
    if summary:
        acc.interaction_summary = summary

    # Walk transcript — tool calls, contact info, and session flags
    from voice_call_summary import merge_tool_into_accumulator
    from voice_tools import parse_tool_arguments

    transcript = data.get("transcript") or []
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        role = (turn.get("role") or "").lower()
        msg = (turn.get("message") or turn.get("text") or "").strip()

        tool_calls = turn.get("tool_calls") or turn.get("toolCalls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(tc.get("tool_name") or tc.get("name") or fn.get("name") or "").strip()
                if not name:
                    continue
                args_raw = tc.get("parameters") or tc.get("arguments") or tc.get("params")
                if args_raw is None and fn:
                    args_raw = fn.get("arguments")
                args = parse_tool_arguments(args_raw)
                result = str(
                    tc.get("result") or tc.get("output") or tc.get("response") or tc.get("content") or ""
                ).strip()
                merge_tool_into_accumulator(acc, name, args, result)

        if not msg:
            continue
        acc.append_log(f"{role[:1].upper()}: {msg[:120]}")

        # Extract fields from both user and agent turns (agent reads back email/phone)
        for email_match in _EMAIL_RE.finditer(msg):
            acc.set_value("email", email_match.group())
        for phone_match in _PHONE_RE.finditer(msg):
            ph = re.sub(r"\D", "", phone_match.group())
            skip_digits = re.sub(r"\D", "", resolved_phone or caller_id or "")
            if len(ph) >= 10 and ph != skip_digits:
                acc.set_value("phone", phone_match.group())

        if role == "user":
            for name_match in _NAME_RE.finditer(msg):
                acc.set_value("name", name_match.group(1))
            for dealer_match in _DEALER_RE.finditer(msg):
                acc.set_value("dealership_name", dealer_match.group(1).strip())

        # Detect pen/hammer session state from agent language
        if role == "agent":
            lower = msg.lower()
            if any(kw in lower for kw in ("hammer drive", "hammer connect", "facebook aia", "lot size", "agreement")):
                acc.pen_hammer_close_active = True
            if "skip" in lower and "pen" in lower:
                acc.pen_challenge_skipped = True
                acc.pen_hammer_close_active = True
            if "sending" in lower and acc.values.get("email"):
                acc.capture_lead_fired = True
                acc.agreement_email_sent = True
            if any(
                kw in lower
                for kw in (
                    "account created",
                    "account is set up",
                    "welcome to hammer",
                    "hammer account is ready",
                )
            ):
                acc.account_created = True

    return acc


def handle_elevenlabs_call_end(raw_body: bytes, sig_header: str | None, event: dict) -> dict:
    """
    POST /api/elevenlabs/call-end — fires Zapier call summary for ElevenLabs phone/browser calls.

    Called by ElevenLabs post-call webhook (post_call_transcription events).
    Verifies HMAC signature if ELEVENLABS_WEBHOOK_SECRET is set.
    Parses the transcript to extract contact info, then fires maybe_post_voice_call_summary.
    """
    from voice_call_summary import maybe_post_voice_call_summary

    # Signature verification (optional). Mismatch must not block Slack — wrong secret is common.
    secret = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "").strip()
    strict = os.environ.get("ELEVENLABS_WEBHOOK_STRICT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if secret:
        if not sig_header:
            msg = "elevenlabs call-end: missing signature header"
            if strict:
                _log.warning("%s — rejecting", msg)
                raise HTTPException(401, "Missing ElevenLabs-Signature header")
            _log.warning("%s — processing anyway (set ELEVENLABS_WEBHOOK_STRICT=1 to reject)", msg)
        elif not _verify_el_webhook_signature(raw_body, sig_header, secret):
            msg = (
                "elevenlabs call-end: invalid HMAC — copy Signing secret from "
                "ElevenLabs → Agent → Post-call webhook into ELEVENLABS_WEBHOOK_SECRET on Fly/Vercel"
            )
            if strict:
                _log.warning(msg)
                raise HTTPException(401, "Invalid webhook signature")
            _log.warning("%s — processing anyway for Slack summary", msg)

    event_type = event.get("type", "")
    if event_type != "post_call_transcription":
        # Acknowledge other event types without processing
        _log.debug("elevenlabs call-end: ignored event type %s", event_type)
        return {"status": "ignored", "type": event_type}

    data = event.get("data") or {}
    conv_id = data.get("conversation_id", "unknown")
    _log.info("elevenlabs call-end: processing conversation_id=%s", conv_id)

    try:
        acc = _build_accumulator_from_el_transcript(data)
        posted = maybe_post_voice_call_summary(acc)
        try:
            from voice_dashboard_store import upsert_call_record

            upsert_call_record(acc)
        except Exception:
            _log.exception("elevenlabs call-end: dashboard persist failed conversation_id=%s", conv_id)
        _log.info(
            "elevenlabs call-end: zapier posted=%s conversation_id=%s channel=%s",
            posted, conv_id, acc.channel,
        )
        return {"status": "ok", "zapier_posted": posted, "conversation_id": conv_id}
    except Exception:
        _log.exception("elevenlabs call-end: failed for conversation_id=%s", conv_id)
        # Return 200 so ElevenLabs does not retry and disable the webhook
        return {"status": "error", "conversation_id": conv_id}
