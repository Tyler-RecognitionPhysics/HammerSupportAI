"""One-time ElevenLabs agent tuning so browser voice calls start with Hannah speaking."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

_log = logging.getLogger(__name__)
_ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
_configure_lock = asyncio.Lock()
_configured = False


def _api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _agent_id() -> str:
    return os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def _public_llm_base() -> str:
    for key in ("SUPPORT_PUBLIC_BASE_URL", "TELEPHONY_PUBLIC_BASE_URL"):
        base = os.environ.get(key, "").strip().rstrip("/")
        if base:
            return base
    return ""


async def ensure_support_agent_speaks_first() -> dict[str, Any] | None:
    """
    Patch the support ConvAI agent so Hannah initiates on connect:
    - empty dashboard first_message (custom LLM + client override handle the line)
    - initial_wait_time=1s triggers the opening custom-LLM call quickly
    """
    global _configured
    if _configured:
        return {"ok": True, "skipped": "already_configured"}

    api_key = _api_key()
    agent_id = _agent_id()
    if not api_key or not agent_id:
        return None

    async with _configure_lock:
        if _configured:
            return {"ok": True, "skipped": "already_configured"}

        agent_block: dict[str, Any] = {"first_message": ""}
        base = _public_llm_base()
        if base:
            agent_block["prompt"] = {
                "llm": "custom-llm",
                "custom_llm": {"url": f"{base}/api/elevenlabs/llm"},
            }

        payload: dict[str, Any] = {
            "conversation_config": {
                "agent": agent_block,
                "turn": {
                    "initial_wait_time": 1,
                    "turn_eagerness": "eager",
                },
            }
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.patch(
                    f"{_ELEVENLABS_BASE}/convai/agents/{agent_id}",
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                )
            if not r.is_success:
                _log.warning(
                    "ElevenLabs support agent configure failed: HTTP %s %s",
                    r.status_code,
                    (r.text or "")[:300],
                )
                return {"ok": False, "status": r.status_code, "detail": (r.text or "")[:300]}

            _configured = True
            _log.info(
                "ElevenLabs support agent configured (initial_wait_time=1, speaks-first)"
            )
            return {
                "ok": True,
                "agent_id": agent_id,
                "initial_wait_time": 1,
                "custom_llm_base": base or None,
            }
        except Exception:
            _log.exception("ElevenLabs support agent configure error")
            return {"ok": False, "detail": "request_failed"}
