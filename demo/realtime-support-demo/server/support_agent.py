"""ElevenLabs custom LLM endpoint for Hammer Support AI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from support_tools import SupportSession, SupportToolExecutor, support_tool_definitions

_log = logging.getLogger(__name__)
_executor: SupportToolExecutor | None = None
_executor_lock = asyncio.Lock()


def _openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


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


async def prewarm_elevenlabs_session(get_retriever_fn: Callable) -> None:
    await _get_executor(get_retriever_fn)


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


def _call_id_from_body(body: dict) -> str:
    meta = body.get("metadata") or {}
    for key in ("conversation_id", "call_id", "session_id"):
        val = str(meta.get(key) or body.get(key) or "").strip()
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


async def _run_tool_loop(
    messages: list[dict],
    executor: SupportToolExecutor,
    session: SupportSession,
    model: str,
) -> str:
    api_key = _openai_key()
    wiki = executor.prefetched_wiki_context() or ""
    system = build_support_voice_prompt(wiki_context=wiki)
    full = [{"role": "system", "content": system}] + messages[-24:]
    tools = support_tool_definitions()

    async with httpx.AsyncClient(timeout=90.0) as client:
        for _ in range(5):
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": full,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.5,
                },
            )
            resp.raise_for_status()
            choice = resp.json()["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                return str(choice.get("content") or "").strip()
            full.append(choice)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = executor.execute(name, args, session)
                full.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
    return "I'm having a technical issue — a representative will reach out as soon as possible. You can also email support@hammertime.com."


async def handle_elevenlabs_llm(body: dict, get_retriever_fn: Callable) -> StreamingResponse:
    if not _openai_key():
        raise HTTPException(503, "OPENAI_API_KEY not configured")

    messages: list[dict] = body.get("messages", [])
    model = _chat_model()
    call_id = _call_id_from_body(body) or f"support-{uuid.uuid4().hex[:12]}"

    if _is_opening_turn(messages):
        greeting = os.environ.get("SUPPORT_GREETING", SUPPORT_GREETING).strip() or SUPPORT_GREETING
        try:
            from support_dashboard_store import register_session_start

            register_session_start(call_id, channel="browser_voice")
        except Exception:
            pass
        return StreamingResponse(
            _sse_chunks(greeting, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    session = SupportSession(call_id=call_id, channel="browser_voice")
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    async def _generate() -> AsyncIterator[bytes]:
        # ElevenLabs drops the call if no SSE bytes arrive quickly (cascade timeout).
        async for chunk in _sse_chunks("", model, chunk_id=chunk_id, created=created, content_only=False):
            yield chunk
            break
        try:
            executor = await _get_executor(get_retriever_fn)
            text = await _run_tool_loop(messages, executor, session, model)
            async for chunk in _sse_chunks(
                text,
                model,
                chunk_id=chunk_id,
                created=created,
                content_only=True,
            ):
                yield chunk
            try:
                from support_dashboard_store import persist_session

                persist_session(session, messages, agent_reply=text)
            except Exception:
                pass
        except Exception:
            _log.exception("support_agent llm failed")
            async for chunk in _sse_chunks(
                "I'm having a technical issue — please try again or email support@hammertime.com.",
                model,
            ):
                yield chunk

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
