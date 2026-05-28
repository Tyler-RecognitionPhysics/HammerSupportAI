"""ElevenLabs agent admin helpers — list voices, read/update agent config."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException

from voice_call_outcomes import enrich_call_outcomes, infer_outcomes_from_summary

_ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"


def _api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def _agent_id() -> str:
    return os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def _headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise HTTPException(503, "ELEVENLABS_API_KEY is not configured")
    return {"xi-api-key": key}


def elevenlabs_configured() -> bool:
    return bool(_api_key() and _agent_id())


async def list_voices() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_ELEVENLABS_BASE}/voices", headers=_headers())
    if not r.is_success:
        raise HTTPException(502, f"ElevenLabs voices API returned HTTP {r.status_code}")
    data = r.json()
    voices = data.get("voices") if isinstance(data, dict) else data
    if not isinstance(voices, list):
        return []
    out: list[dict[str, Any]] = []
    for v in voices:
        if not isinstance(v, dict):
            continue
        out.append(
            {
                "voice_id": v.get("voice_id") or v.get("id") or "",
                "name": v.get("name") or "Unknown",
                "category": v.get("category") or "",
                "preview_url": v.get("preview_url") or "",
                "labels": v.get("labels") or {},
            }
        )
    out.sort(key=lambda x: (x.get("category") or "", x.get("name") or ""))
    return out


async def get_agent() -> dict[str, Any]:
    agent_id = _agent_id()
    if not agent_id:
        raise HTTPException(503, "ELEVENLABS_AGENT_ID is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_ELEVENLABS_BASE}/convai/agents/{agent_id}",
            headers=_headers(),
        )
    if not r.is_success:
        raise HTTPException(502, f"ElevenLabs agent API returned HTTP {r.status_code}")
    data = r.json()
    tts = (data.get("conversation_config") or {}).get("tts") or {}
    agent_cfg = (data.get("conversation_config") or {}).get("agent") or {}
    prompt_cfg = agent_cfg.get("prompt") or {}
    custom_llm = prompt_cfg.get("custom_llm") or {}
    prompt_text = prompt_cfg.get("prompt") or ""
    return {
        "agent_id": agent_id,
        "name": data.get("name") or "",
        "voice_id": tts.get("voice_id") or "",
        "model_id": tts.get("model_id") or "",
        "first_message": agent_cfg.get("first_message") or "",
        "language": agent_cfg.get("language") or "",
        "llm": prompt_cfg.get("llm") or "",
        "custom_llm_url": custom_llm.get("url") or "",
        "custom_llm_api_type": custom_llm.get("api_type") or "",
        "dashboard_prompt_length": len(prompt_text) if isinstance(prompt_text, str) else 0,
        "dashboard_tools_count": len(prompt_cfg.get("tools") or []),
        "dashboard_tool_ids_count": len(prompt_cfg.get("tool_ids") or []),
    }


async def update_agent_voice(voice_id: str) -> dict[str, Any]:
    agent_id = _agent_id()
    if not agent_id:
        raise HTTPException(503, "ELEVENLABS_AGENT_ID is not configured")
    voice_id = voice_id.strip()
    if not voice_id:
        raise HTTPException(400, "voice_id is required")
    payload = {"conversation_config": {"tts": {"voice_id": voice_id}}}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.patch(
            f"{_ELEVENLABS_BASE}/convai/agents/{agent_id}",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
    if not r.is_success:
        detail = r.text[:300] if r.text else f"HTTP {r.status_code}"
        raise HTTPException(502, f"ElevenLabs agent update failed: {detail}")
    return {"ok": True, "voice_id": voice_id, "agent_id": agent_id}


async def tune_agent_latency_settings() -> dict[str, Any]:
    agent_id = _agent_id()
    if not agent_id:
        raise HTTPException(503, "ELEVENLABS_AGENT_ID is not configured")
    
    # Dynamically build LLM URL from environment base URL
    base_url = os.environ.get("TELEPHONY_PUBLIC_BASE_URL", "").strip()
    
    payload = {
        "conversation_config": {
            "tts": {
                "model_id": "eleven_flash_v2_5",
                "optimize_streaming_latency": "4"
            },
            "turn": {
                "turn_eagerness": "eager",
                "speculative_turn": True
            }
        }
    }
    
    if base_url:
        payload["conversation_config"]["agent"] = {
            "prompt": {
                "custom_llm": {
                    "url": f"{base_url}/api/elevenlabs/llm"
                }
            }
        }

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.patch(
            f"{_ELEVENLABS_BASE}/convai/agents/{agent_id}",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
        )
    if not r.is_success:
        # Fall back to eleven_flash_v2 if the brand new eleven_flash_v2_5 model is not allowed on their subscription tier
        if "model_id" in r.text or "tts" in r.text or r.status_code in (400, 422):
            payload["conversation_config"]["tts"]["model_id"] = "eleven_flash_v2"
            async with httpx.AsyncClient(timeout=15.0) as client2:
                r = await client2.patch(
                    f"{_ELEVENLABS_BASE}/convai/agents/{agent_id}",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
        if not r.is_success:
            detail = r.text[:300] if r.text else f"HTTP {r.status_code}"
            raise HTTPException(502, f"ElevenLabs agent tuning failed: {detail}")
    return {"ok": True, "agent_id": agent_id, "tuned_settings": payload}


def _channel_from_conversation(item: dict[str, Any]) -> str:
    source = str(item.get("conversation_initiation_source") or "").lower()
    direction = str(item.get("direction") or "").lower()
    if "phone" in source or "sip" in source or direction in ("inbound", "outbound"):
        return "phone"
    if "widget" in source or "webrtc" in source or "web" in source:
        return "browser"
    return "browser"


def _unix_to_iso(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _duration_from_item(item: dict[str, Any]) -> int | None:
    for key in ("call_duration_secs", "duration_secs"):
        val = item.get(key)
        if val is not None:
            try:
                secs = int(val)
                if secs > 0:
                    return secs
            except (TypeError, ValueError):
                pass
    meta = item.get("metadata") or {}
    if isinstance(meta, dict):
        val = meta.get("call_duration_secs")
        if val is not None:
            try:
                secs = int(val)
                if secs > 0:
                    return secs
            except (TypeError, ValueError):
                pass
    start = item.get("start_time_unix_secs") or item.get("call_start_unix_secs")
    end = item.get("end_time_unix_secs") or item.get("call_end_unix_secs")
    if start is not None and end is not None:
        try:
            return max(0, int(end) - int(start))
        except (TypeError, ValueError):
            pass
    return None


def _normalize_conversation_summary(item: dict[str, Any]) -> dict[str, Any]:
    conv_id = str(item.get("conversation_id") or item.get("id") or "")
    started = _unix_to_iso(item.get("start_time_unix_secs") or item.get("call_start_unix_secs"))
    ended = _unix_to_iso(item.get("end_time_unix_secs") or item.get("call_end_unix_secs"))
    summary = str(item.get("call_summary_title") or item.get("transcript_summary") or "")
    analysis = item.get("analysis") or {}
    if isinstance(analysis, dict):
        summary = summary or str(analysis.get("transcript_summary") or analysis.get("call_summary_title") or "")
    call: dict[str, Any] = {
        "call_id": conv_id,
        "channel": _channel_from_conversation(item),
        "call_direction": str(item.get("direction") or ""),
        "started_at": started,
        "ended_at": ended,
        "values": {},
        "session_log": [],
        "interaction_summary": summary,
        "capture_lead_fired": False,
        "agreement_email_sent": False,
        "i_approve_approved": False,
        "account_created": False,
        "pen_challenge_skipped": False,
        "pen_hammer_close_active": False,
        "summary_sent": True,
        "status": str(item.get("status") or ""),
        "call_successful": item.get("call_successful"),
        "source": "elevenlabs",
    }
    dur = _duration_from_item(item)
    if dur is not None:
        call["duration_secs"] = dur
    if summary:
        infer_outcomes_from_summary(call, summary)
    return enrich_call_outcomes(call, item if isinstance(item, dict) else None)


async def list_conversations(*, page_size: int = 50, cursor: str | None = None) -> dict[str, Any]:
    agent_id = _agent_id()
    if not agent_id:
        raise HTTPException(503, "ELEVENLABS_AGENT_ID is not configured")
    params: dict[str, str | int] = {"agent_id": agent_id, "page_size": max(1, min(page_size, 100))}
    if cursor:
        params["cursor"] = cursor
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{_ELEVENLABS_BASE}/convai/conversations",
            headers=_headers(),
            params=params,
        )
    if not r.is_success:
        raise HTTPException(502, f"ElevenLabs conversations API returned HTTP {r.status_code}")
    data = r.json()
    raw = data.get("conversations") if isinstance(data, dict) else []
    if not isinstance(raw, list):
        raw = []
    return {
        "calls": [_normalize_conversation_summary(c) for c in raw if isinstance(c, dict)],
        "next_cursor": data.get("next_cursor") if isinstance(data, dict) else None,
        "has_more": bool(data.get("has_more")) if isinstance(data, dict) else False,
    }


def _values_from_init_data(data: dict[str, Any]) -> dict[str, str]:
    """Extract prospect fields from ElevenLabs conversation initiation payload."""
    values: dict[str, str] = {}
    init_data = data.get("conversation_initiation_client_data") or {}
    if not isinstance(init_data, dict):
        return values

    dyn = init_data.get("dynamic_variables") or {}
    if isinstance(dyn, dict):
        skip_prefix = ("system__",)
        key_map = {
            "email": "email",
            "phone": "phone",
            "name": "name",
            "dealership": "dealership_name",
            "dealership_name": "dealership_name",
            "display_name": "display_name",
            "legal_name": "legal_name",
            "selected_plan": "selected_plan",
            "product": "product_interest",
            "product_interest": "product_interest",
            "appointment_time": "appointment_time",
        }
        for raw_key, val in dyn.items():
            if not val or not isinstance(raw_key, str):
                continue
            if raw_key.startswith(skip_prefix):
                if raw_key == "system__caller_id" and val:
                    values.setdefault("phone", str(val).strip())
                continue
            norm = key_map.get(raw_key.strip().lower(), raw_key.strip().lower())
            values[norm] = str(val).strip()

    extra = init_data.get("custom_llm_extra_body") or {}
    if isinstance(extra, dict):
        scenario = str(extra.get("voice_scenario") or "").strip()
        if scenario:
            values["voice_scenario"] = scenario

    return values


def _parse_transcript(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    transcript = data.get("transcript") or []
    turns: list[dict[str, Any]] = []
    log_lines: list[str] = []
    if not isinstance(transcript, list):
        return turns, log_lines

    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "unknown").lower()
        message = str(turn.get("message") or turn.get("text") or "").strip()
        tool_calls = turn.get("tool_calls") or turn.get("toolCalls") or []
        tool_notes: list[str] = []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = str(tc.get("tool_name") or tc.get("name") or "tool")
                result = tc.get("result") or tc.get("output") or tc.get("response")
                if result is not None:
                    text = str(result).strip()
                    if len(text) > 200:
                        text = text[:200] + "…"
                    tool_notes.append(f"{name}: {text}")
                else:
                    tool_notes.append(name)

        if message:
            label = "Agent" if role == "agent" else "Visitor" if role == "user" else role.title()
            log_lines.append(f"{label}: {message}")
            turns.append(
                {
                    "role": role,
                    "message": message,
                    "time_secs": turn.get("time_in_call_secs"),
                    "tool_calls": tool_notes,
                }
            )
        elif tool_notes:
            label = "Agent" if role == "agent" else role.title()
            joined = "; ".join(tool_notes)
            log_lines.append(f"{label} [tool]: {joined}")
            turns.append(
                {
                    "role": role,
                    "message": "",
                    "time_secs": turn.get("time_in_call_secs"),
                    "tool_calls": tool_notes,
                }
            )

    return turns, log_lines


async def get_conversation(conversation_id: str) -> dict[str, Any]:
    conversation_id = conversation_id.strip()
    if not conversation_id:
        raise HTTPException(400, "conversation_id is required")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{_ELEVENLABS_BASE}/convai/conversations/{conversation_id}",
            headers=_headers(),
        )
    if r.status_code == 404:
        raise HTTPException(404, "Conversation not found")
    if not r.is_success:
        raise HTTPException(502, f"ElevenLabs conversation API returned HTTP {r.status_code}")
    data = r.json()
    call = _normalize_conversation_summary(data)
    call["call_id"] = conversation_id

    turns, log_lines = _parse_transcript(data)
    call["transcript"] = turns
    call["session_log"] = log_lines[-80:]

    analysis = data.get("analysis") or {}
    if isinstance(analysis, dict):
        summary = analysis.get("transcript_summary") or analysis.get("call_summary_title")
        if summary:
            call["interaction_summary"] = str(summary)

    metadata = data.get("metadata") or {}
    if isinstance(metadata, dict):
        for key in ("email", "phone", "name", "dealership", "dealership_name"):
            val = metadata.get(key)
            if val:
                norm = "dealership_name" if key == "dealership" else key
                call["values"][norm] = str(val)
        dur = metadata.get("call_duration_secs")
        if dur is not None:
            try:
                call["duration_secs"] = int(dur)
            except (TypeError, ValueError):
                pass
        start_ts = metadata.get("start_time_unix_secs")
        if start_ts and not call.get("started_at"):
            call["started_at"] = _unix_to_iso(start_ts)

    init_values = _values_from_init_data(data)
    call["values"].update({k: v for k, v in init_values.items() if v})

    call["events"] = []
    call["source"] = "elevenlabs"
    return enrich_call_outcomes(call, data)
