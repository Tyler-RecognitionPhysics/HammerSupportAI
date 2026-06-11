"""ElevenLabs custom LLM endpoint for Hammer Support AI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from support_instructions import (
    SUPPORT_GREETING,
    WIKI_PREFETCH_QUERIES,
    build_support_voice_prompt,
)
from support_tools import (
    SupportSession,
    SupportToolExecutor,
    _NO_TIME_GUARD_RESULT,
    append_callback_time_prompt,
    customer_stated_a_time,
    format_support_knowledge_result,
    is_closing_pleasantry,
    redact_support_contacts,
    should_offer_callback_time,
    support_tool_definitions,
)

_log = logging.getLogger(__name__)

# Facebook / advertising questions are never answered or troubleshot by the AI —
# they route to a human via a ticket (+ optional scheduled callback).
_FACEBOOK_ADS_INTENT_RE = re.compile(
    r"\b(facebook|fb|meta|instagram|insta|marketplace|"
    r"(?:\w+\s+)?ads?|advertis\w*|ad\s+account|ad\s+campaign\w*|campaign\w*|"
    r"aia|automated\s+inventory|boosted?\s+post\w*|ad\s+spend|ad\s+budget)\b",
    re.IGNORECASE,
)
_FACEBOOK_VOICE_NUDGE = (
    "Reminder: this is a Facebook/advertising question — our ads team handles these, so do NOT "
    "troubleshoot it, explain it, or ask clarifying questions about the ad issue. If you have the "
    "dealership name, the customer's name, and the email on their Hammer account, call "
    "create_support_ticket now with issue_category='facebook-aia', resolved=false, and a short "
    "issue_summary. If a field is missing, ask for just that one. Then ask if they have a preferred "
    "day/time for our team to reach out and, if they give one, call schedule_callback so it lands on "
    "the calendar. Never say it's handled until create_support_ticket has actually run."
)


def _is_facebook_ads_intent(messages: list[dict]) -> bool:
    for m in messages:
        if m.get("role") == "user" and _FACEBOOK_ADS_INTENT_RE.search(str(m.get("content") or "")):
            return True
    return False
_executor: SupportToolExecutor | None = None
_executor_lock = asyncio.Lock()

# Shared keep-alive client: a fresh AsyncClient per turn costs a full TCP+TLS
# handshake to api.openai.com (~100-300ms) before the model even starts.
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=120.0),
        )
    return _http_client


def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _openai_payload_extras() -> dict:
    """Optional knobs applied to every chat-completions call.

    SUPPORT_OPENAI_SERVICE_TIER=priority buys lower/more consistent TTFT on
    paid priority processing — same model, same answers, just faster scheduling.
    """
    tier = os.environ.get("SUPPORT_OPENAI_SERVICE_TIER", "").strip()
    return {"service_tier": tier} if tier else {}


def _el_api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _el_agent_id() -> str:
    return os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def _chat_model() -> str:
    return os.environ.get("SUPPORT_CHAT_MODEL", os.environ.get("ELEVENLABS_CHAT_MODEL", "gpt-4o-mini")).strip()


async def _get_executor(get_retriever_fn: Callable) -> SupportToolExecutor:
    global _executor
    if _executor is not None:
        return _executor
    async with _executor_lock:
        if _executor is None:
            _executor = SupportToolExecutor(get_retriever_fn)
            asyncio.create_task(asyncio.to_thread(_executor.warm_wiki_context, WIKI_PREFETCH_QUERIES))
    return _executor


def invalidate_executor_wiki() -> None:
    global _executor
    if _executor is not None:
        _executor._prefetched_wiki = None
    _WIKI_CACHE.clear()


async def _prewarm_openai_connection() -> None:
    """Open (or refresh) the pooled TLS connection to OpenAI so the first turn of
    the call doesn't pay the handshake before the model can start."""
    key = _openai_key()
    if not key:
        return
    try:
        client = _get_http_client()
        await client.get(
            "https://api.openai.com/v1/models/gpt-4o-mini",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10.0,
        )
    except Exception:
        pass


async def prewarm_elevenlabs_session(get_retriever_fn: Callable) -> None:
    await _get_executor(get_retriever_fn)


def _last_user_message(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


# Per-call cache of the last question-specific wiki context. Mid-call turns that
# are just contact info / acknowledgements ("my email is...", "yes", a name) reuse
# the previous turn's grounding instead of paying retrieval + losing context.
_WIKI_CACHE: dict[str, str] = {}

_NON_QUESTION_TURN_RE = re.compile(
    r"^(?:yes|yeah|yep|no|nope|ok(?:ay)?|sure|correct|right|sounds good|that works|perfect|"
    r"great|thanks?|thank you|uh huh|mm-?hm+)[.! ]*$"
    r"|^(?:my |the )?(?:name|email|e-mail|phone|number|dealership)(?: name)?\s+is\b"
    r"|^(?:i'?m|it'?s|this is)\s+[A-Za-z][\w'.-]*(?:\s+[A-Za-z][\w'.-]*){0,3}[.! ]*$"
    r"|^[\w'.-]+@[\w.-]+\.\w+[.! ]*$"
    r"|^[+\d][\d\s().-]{6,}$",
    re.IGNORECASE,
)


def _needs_retrieval(text: str) -> bool:
    """False only for turns that obviously contain no new question: short acks,
    names, emails, phone numbers, or a bare callback time. Anything with a '?' or
    real content always retrieves — accuracy beats the saved milliseconds."""
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or len(t) > 90:
        return True
    if _NON_QUESTION_TURN_RE.match(t):
        return False
    # A pure time/availability answer ("tomorrow at 2pm works") needs no KB.
    words = t.split()
    if len(words) <= 8 and customer_stated_a_time([{"role": "user", "content": t}]):
        return False
    return True


def _query_specific_wiki(retriever: Any, query: str, *, max_chars: int = 3500) -> str:
    """Question-specific support knowledge — mirrors the Test AI / chat path.

    Uses ``search_support_knowledge`` (which surfaces admin-APPROVED ANSWERS as the
    highest-authority block, then official KB, then related resolved tickets) so the
    voice prompt is grounded on the SAME corrected facts the dashboard's Test AI sees.
    Falls back to plain top_k for retrievers that don't expose the structured search.

    Voice trims the context slightly vs chat (3500 chars / 3 ticket cases): a smaller
    prompt means a faster first spoken token, and the authority hierarchy (approved
    answers > official KB > tickets) is preserved, so accuracy is unchanged.
    """
    q = (query or "").strip()
    if not q:
        return ""
    if hasattr(retriever, "search_support_knowledge"):
        result = retriever.search_support_knowledge(
            q,
            official_k=4,
            ticket_case_limit=3,
            ticket_chunks_per_case=2,
        )
        return format_support_knowledge_result(result, max_chars=max_chars)
    pairs = retriever.top_k(q, k=8)
    from support_tools import format_wiki_excerpts

    return format_wiki_excerpts(pairs, max_chars=max_chars)


def _is_opening_turn(messages: list[dict]) -> bool:
    """True before Hannah has spoken — matches ElevenLabs silence/timeout first calls."""
    if not any(m.get("role") == "assistant" for m in messages):
        return True
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if len(user_msgs) == 1:
        text = str(user_msgs[0].get("content") or "").strip().lower()
        if text in ("", "...", "hello", "hi", "hey"):
            return True
    return False


def _llm_extra_body(body: dict) -> dict:
    """ElevenLabs sends per-conversation context under one of several keys depending
    on SDK/version (LLM proxy vs WebSocket initiation)."""
    for key in ("elevenlabs_extra_body", "custom_llm_extra_body", "customLlmExtraBody"):
        extra = body.get(key)
        if isinstance(extra, dict) and extra:
            return extra
    return {}


def _call_id_from_body(body: dict) -> str:
    # 1) Top-level + metadata (older shape).
    meta = body.get("metadata") or {}
    for key in ("conversation_id", "call_id", "session_id"):
        val = str(meta.get(key) or body.get(key) or "").strip()
        if val:
            return val
    # 2) ElevenLabs custom-LLM extra body — this is where the stable conversation_id
    #    actually arrives on the LLM proxy turns. Without it every turn would fall
    #    back to a new random id and the transcript would never accumulate.
    extra = _llm_extra_body(body)
    for key in ("conversation_id", "call_id", "session_id"):
        val = str(extra.get(key) or "").strip()
        if val:
            return val
    # 3) Conversation initiation client data (WebSocket initiation shape).
    for container in (body, extra):
        init = container.get("conversation_initiation_client_data") or container.get("conversationInitiationClientData")
        if isinstance(init, dict):
            val = str(init.get("conversation_id") or "").strip()
            if val:
                return val
    return ""


def _sse_chunks(
    text: str,
    model: str,
    *,
    chunk_id: str | None = None,
    created: int | None = None,
    content_only: bool = False,
) -> AsyncIterator[bytes]:
    async def _gen() -> AsyncIterator[bytes]:
        cid = chunk_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
        ts = created if created is not None else int(time.time())
        if not content_only:
            role_payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_payload)}\n\n".encode()
        for i in range(0, max(len(text), 1), 8):
            piece = text[i : i + 8]
            payload = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload)}\n\n".encode()
        done = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return _gen()


_TECH_ISSUE_FALLBACK = (
    "I'm having a technical issue right now — let me log a ticket so a Hammer representative "
    "can reach out as soon as possible. What's your dealership name, your name, and the email "
    "on your Hammer account?"
)

# Flush points for streaming: a completed sentence (or clause long enough to speak).
# ElevenLabs starts TTS as soon as the first flushed piece arrives.
_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]?\s")
_CLAUSE_END_RE = re.compile(r"[,;:][\"')\]]?\s")


def _pop_flushable(buf: str, *, first: bool) -> tuple[str, str]:
    """Split (piece_to_flush, remainder). The FIRST flush is aggressive — a clause
    or even a word boundary is enough to get TTS speaking — later flushes wait for
    full sentences so the voice doesn't get choppy."""
    m = _SENTENCE_END_RE.search(buf)
    if m:
        return buf[: m.end()], buf[m.end():]
    if first:
        if len(buf) >= 20:
            m = _CLAUSE_END_RE.search(buf)
            if m:
                return buf[: m.end()], buf[m.end():]
        if len(buf) >= 60:
            # Never split mid-word (keeps email addresses whole for redaction).
            ws = buf.rfind(" ")
            if ws > 20:
                return buf[: ws + 1], buf[ws + 1:]
    return "", buf


async def _prepare_turn(
    messages: list[dict],
    executor: SupportToolExecutor,
    session: SupportSession,
) -> list[dict]:
    """Build the full prompt (question-specific KB grounding + persona) for this turn."""
    # Ground on knowledge SPECIFIC to what the customer just asked — same as the
    # dashboard's Test AI (which is correct). The generic pre-warmed context does
    # NOT include admin-approved answers, so relying on it (or hoping the model
    # calls search_wiki) is exactly why voice drifts from the Test AI answer.
    last_user = _last_user_message(messages)
    call_id = getattr(session, "call_id", "") or ""
    wiki = ""
    if last_user and _needs_retrieval(last_user):
        try:
            t0 = time.perf_counter()
            retriever = executor._get_retriever()
            wiki = await asyncio.to_thread(_query_specific_wiki, retriever, last_user)
            _log.info(
                "voice_timing call_id=%s retrieval_ms=%d wiki_chars=%d",
                call_id, int((time.perf_counter() - t0) * 1000), len(wiki),
            )
        except Exception:
            _log.exception("support_agent: question-specific wiki retrieval failed")
    elif last_user and call_id:
        # Contact-info / ack turn: keep grounding from the question that started
        # this thread instead of re-retrieving for "my email is ...".
        wiki = _WIKI_CACHE.get(call_id, "")
    if not wiki:
        wiki = executor.prefetched_wiki_context() or ""
    # Vendor questions are answered from the structured vendor list (exact
    # lookup beats BM25) — same data the chat AI and dashboard Vendors tab use.
    if last_user:
        try:
            from support_vendors import vendor_context_block

            vendor_block = vendor_context_block(last_user)
            if vendor_block:
                wiki = f"{wiki}\n\n{vendor_block}".strip() if wiki else vendor_block
        except Exception:
            _log.exception("support_agent: vendor context lookup failed")

    if call_id and wiki:
        if len(_WIKI_CACHE) > 256:
            _WIKI_CACHE.clear()
        _WIKI_CACHE[call_id] = wiki

    system = build_support_voice_prompt(wiki_context=wiki)
    return [{"role": "system", "content": system}] + messages[-24:]


async def _execute_tool_guarded(
    name: str,
    args: dict,
    messages: list[dict],
    executor: SupportToolExecutor,
    session: SupportSession,
) -> str:
    """Run a tool with the hard callback-time guard applied."""
    # Hard guard: never let the model auto-book a callback at a time the
    # customer never said out loud. It must ask for a time first.
    if name == "schedule_callback" and not customer_stated_a_time(messages):
        return _NO_TIME_GUARD_RESULT
    return await executor.execute_tool(name, args, session)


async def _run_tool_loop(
    messages: list[dict],
    executor: SupportToolExecutor,
    session: SupportSession,
    model: str,
) -> str:
    """Buffered (non-streaming) turn — used for Facebook/ad routing turns where the
    reply may need to be retracted and replaced via the nudge before it is spoken."""
    api_key = _openai_key()
    full = await _prepare_turn(messages, executor, session)
    tools = support_tool_definitions()

    facebook_ads_intent = _is_facebook_ads_intent(messages)
    fb_nudged = False

    client = _get_http_client()
    for _ in range(5):
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": full,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.1,
                **_openai_payload_extras(),
            },
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            # Backstop: never let Hannah troubleshoot a Facebook/ad question on voice —
            # nudge her once to route it to a ticket (+ callback) instead.
            if (
                facebook_ads_intent
                and not fb_nudged
                and not getattr(session, "ticket_created", False)
            ):
                fb_nudged = True
                full.append(choice)
                full.append({"role": "system", "content": _FACEBOOK_VOICE_NUDGE})
                continue
            reply = redact_support_contacts(str(choice.get("content") or "").strip())
            # Always offer to schedule a callback after a follow-up ticket is logged.
            # The time question must close the reply, replacing any "anything else?".
            if should_offer_callback_time(messages, session, reply):
                reply = append_callback_time_prompt(reply)
            return reply
        full.append(choice)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await _execute_tool_guarded(name, args, messages, executor, session)
            full.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
    return _TECH_ISSUE_FALLBACK


async def _stream_tool_loop(
    messages: list[dict],
    executor: SupportToolExecutor,
    session: SupportSession,
    model: str,
) -> AsyncIterator[str]:
    """Streaming turn: forward model tokens to ElevenLabs as sentences complete.

    This is the main latency win — TTS starts speaking after the FIRST sentence
    instead of waiting for the whole reply to generate. Tool-call turns are
    detected from the first delta (OpenAI decides content-vs-tools up front), so
    tools still execute exactly as in the buffered loop, and redaction runs on
    each flushed sentence before it is ever emitted.
    """
    api_key = _openai_key()
    full = await _prepare_turn(messages, executor, session)
    tools = support_tool_definitions()
    client = _get_http_client()

    emitted = ""  # everything yielded so far (for the callback-time check)
    held = ""  # pleasantry sentences withheld while a callback-time ask is pending

    for _round in range(5):
        tool_frags: dict[int, dict] = {}
        buf = ""
        saw_tool_calls = False
        finish_reason = None

        async with client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": full,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.1,
                "stream": True,
                **_openai_payload_extras(),
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                finish_reason = choices[0].get("finish_reason") or finish_reason
                delta = choices[0].get("delta") or {}

                for frag in delta.get("tool_calls") or []:
                    saw_tool_calls = True
                    idx = int(frag.get("index") or 0)
                    slot = tool_frags.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if frag.get("id"):
                        slot["id"] = frag["id"]
                    fn = frag.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

                piece = delta.get("content")
                if piece and not saw_tool_calls:
                    buf += piece
                    # Flush as soon as something speakable exists (redacted).
                    while True:
                        out, buf = _pop_flushable(buf, first=not emitted)
                        if not out:
                            break
                        out = redact_support_contacts(out)
                        if not out:
                            continue
                        # When we'll have to ask for a callback time at the end of
                        # this reply, withhold "anything else?" closers so the time
                        # question isn't spoken AFTER them (it replaces them).
                        if is_closing_pleasantry(out) and should_offer_callback_time(
                            messages, session, emitted + out
                        ):
                            held += out
                            continue
                        if held:
                            out = held + out
                            held = ""
                        emitted += out
                        yield out

        if saw_tool_calls and tool_frags:
            tool_calls = [
                {
                    "id": slot["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for i, slot in sorted(tool_frags.items())
            ]
            full.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await _execute_tool_guarded(
                    fn.get("name") or "", args, messages, executor, session
                )
                full.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue  # next round — model speaks after seeing tool results

        # Plain-text turn finished: flush the tail + enforced callback-time ask.
        tail = held + redact_support_contacts(buf).rstrip()
        held = ""
        reply_total = (emitted + tail).strip()
        if should_offer_callback_time(messages, session, reply_total):
            # Drop any withheld/trailing "anything else?" closer; end on the question.
            tail = append_callback_time_prompt(tail)
        if tail:
            emitted += tail
            yield tail
        if not emitted.strip():
            yield _TECH_ISSUE_FALLBACK
        return

    if not emitted.strip():
        yield _TECH_ISSUE_FALLBACK


async def handle_elevenlabs_llm(body: dict, get_retriever_fn: Callable) -> StreamingResponse:
    if not _openai_key():
        raise HTTPException(503, "OPENAI_API_KEY not configured")

    messages: list[dict] = body.get("messages", [])
    model = _chat_model()
    call_id = _call_id_from_body(body) or f"support-{uuid.uuid4().hex[:12]}"

    if _is_opening_turn(messages):
        greeting = os.environ.get("SUPPORT_GREETING", SUPPORT_GREETING).strip() or SUPPORT_GREETING
        _log.info("elevenlabs_llm opening turn call_id=%s", call_id)
        try:
            from support_dashboard_store import register_session_start

            register_session_start(call_id, channel="browser_voice")
        except Exception:
            pass

        async def _instant_open() -> AsyncIterator[bytes]:
            async for chunk in _sse_chunks(greeting, model):
                yield chunk

        return StreamingResponse(
            _instant_open(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    session = SupportSession(call_id=call_id, channel="browser_voice")
    try:
        from support_dashboard_store import hydrate_support_session

        hydrate_support_session(session, call_id)
    except Exception:
        pass
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _sse_delta(content: str) -> bytes:
        payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def _generate() -> AsyncIterator[bytes]:
        # ElevenLabs drops the call if no SSE bytes arrive quickly (cascade timeout).
        role_payload = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(role_payload)}\n\n".encode()

        full_text = ""
        turn_t0 = time.perf_counter()
        ttft_ms = -1
        try:
            executor = await _get_executor(get_retriever_fn)
            # Facebook/ad routing turns stay buffered: their reply may be retracted
            # and rewritten by the nudge, which streaming cannot take back.
            if _is_facebook_ads_intent(messages) and not getattr(session, "ticket_created", False):
                text = await _run_tool_loop(messages, executor, session, model)
                full_text = text
                ttft_ms = int((time.perf_counter() - turn_t0) * 1000)
                yield _sse_delta(text)
            else:
                async for piece in _stream_tool_loop(messages, executor, session, model):
                    if ttft_ms < 0:
                        ttft_ms = int((time.perf_counter() - turn_t0) * 1000)
                    full_text += piece
                    yield _sse_delta(piece)
        except Exception:
            _log.exception("support_agent llm failed")
            if not full_text:
                apology = (
                    "I'm having a technical issue — please try again in a moment, and I'll "
                    "log a ticket so a Hammer representative can follow up."
                )
                full_text = apology
                yield _sse_delta(apology)

        done = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done)}\n\n".encode()
        yield b"data: [DONE]\n\n"

        _log.info(
            "voice_timing call_id=%s ttft_ms=%d total_ms=%d reply_chars=%d",
            call_id, ttft_ms, int((time.perf_counter() - turn_t0) * 1000), len(full_text),
        )

        try:
            from support_dashboard_store import persist_session

            persist_session(session, messages, agent_reply=full_text)
        except Exception:
            pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _voice_greeting() -> str:
    try:
        from support_instructions import get_default_prompts

        return (
            os.environ.get("SUPPORT_GREETING", "").strip()
            or get_default_prompts().get("support_greeting", "").strip()
            or SUPPORT_GREETING
        )
    except Exception:
        return os.environ.get("SUPPORT_GREETING", SUPPORT_GREETING).strip() or SUPPORT_GREETING


async def handle_elevenlabs_token(get_retriever_fn: Callable | None = None) -> dict:
    api_key = _el_api_key()
    agent_id = _el_agent_id()
    if not api_key or not agent_id:
        raise HTTPException(503, "ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID required")

    from support_elevenlabs_setup import ensure_support_agent_speaks_first

    asyncio.create_task(ensure_support_agent_speaks_first())
    asyncio.create_task(_prewarm_openai_connection())

    if get_retriever_fn is not None:
        asyncio.create_task(prewarm_elevenlabs_session(get_retriever_fn))

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/convai/conversation/token?agent_id={agent_id}",
            headers={"xi-api-key": api_key},
        )
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, resp.text)
        data = resp.json()
        token = data.get("token") or data.get("conversation_token")
        if not token:
            raise HTTPException(502, "ElevenLabs token missing from response")
        return {"token": token, "agent_id": agent_id, "voice_greeting": _voice_greeting()}
