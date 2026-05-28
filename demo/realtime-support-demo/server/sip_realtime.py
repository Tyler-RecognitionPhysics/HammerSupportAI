"""OpenAI Realtime SIP webhook + sideband WebSocket tool loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

import httpx
import websockets
from openai import InvalidWebhookSignatureError, OpenAI

from voice_instructions import (
    PenOpening,
    build_pen_call_instructions,
    build_phone_accept_instructions,
    format_phone_opening_response_create,
    format_pen_opening_spoken_line,
    pick_pen_opening,
    warm_instruction_cache,
)
from lead_zapier import normalize_phone_e164
from voice_call_summary import maybe_post_voice_call_summary
from voice_tools import CallSession, VoiceToolExecutor, parse_tool_arguments, pen_challenge_tool_definitions

logger = logging.getLogger("sip_realtime")

OPENAI_REALTIME_ACCEPT = "https://api.openai.com/v1/realtime/calls/{call_id}/accept"
OPENAI_REALTIME_WS = "wss://api.openai.com/v1/realtime?call_id={call_id}"

# Tool calls that mark the start of the signup capture phase.
# Triggers the one-time SIP STT upgrade to the higher-accuracy model.
_SIP_CAPTURE_TRIGGER_TOOLS = frozenset(
    {
        "begin_hammer_signup",
        "skip_pen_challenge",
        "capture_lead",
        "open_hammer_account_form",
        "fill_hammer_account_field",
    }
)


def telephony_enabled() -> bool:
    return env_truthy(os.environ.get("REALTIME_SALES_TELEPHONY", "")) or bool(
        os.environ.get("OPENAI_WEBHOOK_SECRET", "").strip()
    )


def env_truthy(raw: str | None) -> bool:
    v = (raw or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _reasoning_minimal() -> bool:
    return os.environ.get("REALTIME_SALES_SIP_REASONING_MINIMAL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


_SIP_TRANSCRIPTION_PROMPT = (
    "Hammer, Hammertime, MarketPoster, DealerBids, Hannah, Facebook AIA, "
    "Gmail, Outlook, Yahoo, Hotmail, iCloud, Comcast, AOL, Proton, Fastmail, "
    "dealership, VIN, CDK, NADA, VinSolutions, DealerSocket, Reynolds, "
    "Dominion, Promax, Dealertrack. "
    # Digit-sequence priming: prevents Whisper from collapsing spoken digit strings
    # into years (e.g. "six oh two five" → "2025"). Including literal digit sequences
    # in the prompt context shifts the model toward per-digit transcription.
    "Individual digits spoken one at a time: "
    "6 0 2 5, 3 8 4 1, 9 5 0 2, 7 4 1 6, 8 3 6 0, 4 9 1 7, "
    "5 0 2 6, 1 6 0 2, 3 0 2 5, 6 2 0 5, 9 0 2 6, 5 6 0 2."
)


def _sip_transcription_config() -> dict[str, Any]:
    """Default SIP STT config — kept on gpt-4o-mini-transcribe for latency.

    `language` and `prompt` are free latency-wise and improve accuracy on
    brand/domain words. The capture-phase upgrade lives in
    ``_sip_transcription_config_capture``.
    """
    return {
        "model": os.environ.get(
            "REALTIME_SALES_SIP_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
        ).strip(),
        "language": "en",
        "prompt": _SIP_TRANSCRIPTION_PROMPT,
    }


def _sip_transcription_config_capture() -> dict[str, Any]:
    """Higher-accuracy SIP STT for signup capture turns only.

    Costs ~100-300 ms per turn over mini, but only runs for the 3-6 turns
    where Hannah is collecting email, phone, and address. Kill switch:
    ``REALTIME_SALES_SIP_CAPTURE_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe``.
    """
    return {
        "model": os.environ.get(
            "REALTIME_SALES_SIP_CAPTURE_TRANSCRIPTION_MODEL", "gpt-4o-transcribe"
        ).strip(),
        "language": "en",
        "prompt": _SIP_TRANSCRIPTION_PROMPT,
    }


def _sip_turn_detection_opening() -> dict[str, Any]:
    """Conservative VAD while Hannah delivers the phone greeting (line noise / hello overlap)."""
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 600,
        "create_response": False,
        "interrupt_response": False,
    }


def _sip_audio_input_opening() -> dict[str, Any]:
    return {
        "turn_detection": _sip_turn_detection_opening(),
        "transcription": _sip_transcription_config(),
    }


def _sip_audio_input_conversation() -> dict[str, Any]:
    return {
        "turn_detection": _sip_turn_detection_conversation(),
        "transcription": _sip_transcription_config(),
    }


def _sip_audio_input_capture() -> dict[str, Any]:
    """Audio-input config used during signup capture turns (PHASE A / PHASE B)."""
    return {
        "turn_detection": _sip_turn_detection_conversation(),
        "transcription": _sip_transcription_config_capture(),
    }


def _parse_phone_from_sip_header_value(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    match = re.search(r"\+[\d]{10,15}", raw)
    if match:
        return normalize_phone_e164(match.group(0))
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        return normalize_phone_e164(digits)
    return ""


def _sip_header_pairs(event: Any) -> list[tuple[str, str]]:
    data = getattr(event, "data", None)
    if data is None:
        return []
    headers = getattr(data, "sip_headers", None) or []
    pairs: list[tuple[str, str]] = []
    for item in headers:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            val = str(item.get("value", "")).strip()
        else:
            name = str(getattr(item, "name", "")).strip()
            val = str(getattr(item, "value", "")).strip()
        if name:
            pairs.append((name, val))
    return pairs


def extract_caller_phone_from_incoming_event(event: Any) -> str:
    """Inbound caller E.164 from OpenAI realtime.call.incoming sip_headers (From, etc.)."""
    for name, value in _sip_header_pairs(event):
        lower = name.lower()
        if lower in ("x-customer-phone",):
            phone = _parse_phone_from_sip_header_value(value)
            if phone:
                return phone
    for name, value in _sip_header_pairs(event):
        lower = name.lower()
        if lower in ("from", "p-asserted-identity", "x-number", "remote-party-id"):
            phone = _parse_phone_from_sip_header_value(value)
            if phone:
                return phone
    return ""


def _sip_opening_watchdog_seconds() -> float:
    raw = os.environ.get("REALTIME_SALES_SIP_OPENING_GUARD_S", "12").strip()
    try:
        return max(8.0, float(raw))
    except ValueError:
        return 18.0


def _realtime_session_patch(**fields: Any) -> dict[str, Any]:
    """OpenAI Realtime session.update requires session.type (GA API)."""
    return {"type": "realtime", **fields}


def _sip_turn_detection_conversation() -> dict[str, Any]:
    """Normal duplex after the opening line has finished playing on the line.

    Default eagerness is medium (not high) — PSTN line noise + narrowband audio
    false-trigger high eagerness and sound choppy; browser WebRTC keeps high.
    """
    eagerness = os.environ.get("REALTIME_SALES_SIP_VAD_EAGERNESS", "medium").strip().lower()
    if eagerness not in ("low", "medium", "high", "auto"):
        eagerness = "medium"
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 350,
        "create_response": True,
        "interrupt_response": True,
    }


def _sip_first_tts_settle_seconds() -> float:
    """Pause after session.update before first phone TTS (matches browser settle)."""
    raw = os.environ.get("REALTIME_SALES_SIP_FIRST_TTS_SETTLE_S", "0.4").strip()
    try:
        return max(0.15, min(1.5, float(raw)))
    except ValueError:
        return 0.4


class SipRealtimeService:
    def __init__(
        self,
        *,
        api_key: str,
        webhook_secret: str,
        get_retriever: Callable[[], Any],
        model: str | None = None,
        voice: str | None = None,
        voice_speed: float | None = None,
    ) -> None:
        from realtime_voice_config import REALTIME_DEFAULT_VOICE, REALTIME_DEFAULT_VOICE_SPEED

        self._api_key = api_key.strip()
        self._webhook_secret = webhook_secret.strip()
        self._get_retriever = get_retriever
        self._model = (model or os.environ.get("REALTIME_SALES_MODEL", "gpt-realtime-2")).strip()
        # Locked platform default — ignore env and constructor overrides so every call sounds the same.
        self._voice = REALTIME_DEFAULT_VOICE
        self._voice_speed = REALTIME_DEFAULT_VOICE_SPEED
        self._client = OpenAI(api_key=self._api_key, webhook_secret=self._webhook_secret)
        self._http = httpx.AsyncClient(timeout=30.0)
        self._tools = VoiceToolExecutor(get_retriever)
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._sideband_tasks: dict[str, asyncio.Task[None]] = {}
        self._warmup_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def schedule_warmup(self) -> None:
        """Warm static prompts + wiki index off the inbound-call critical path."""
        warm_instruction_cache()
        if self._warmup_task and not self._warmup_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._warmup_task = loop.create_task(self.warmup())

    async def warmup(self) -> None:
        try:
            await asyncio.to_thread(self._get_retriever)
            await asyncio.to_thread(self._tools.warm_wiki_context)
            logger.info("SIP telephony warmup complete (retriever + wiki prefetch)")
        except Exception:
            logger.exception("SIP telephony warmup failed")

    def unwrap_webhook(self, body: bytes, headers: dict[str, str]) -> Any:
        return self._client.webhooks.unwrap(body, headers)

    async def accept_incoming_call(self, call_id: str) -> bool:
        """Slim accept (OpenAI + Twilio pattern). Return False if call already ended."""
        logger.info("SIP accept start call_id=%s key_last4=...%s", call_id, self._api_key[-4:] if len(self._api_key) >= 4 else "?")
        status = await self._accept_call(call_id, self._build_slim_accept_payload())
        if status == 404:
            logger.warning("accept 404 for call_id=%s — caller hung up or SIP leg failed", call_id)
            return False
        if status >= 400:
            raise RuntimeError(f"accept call HTTP {status}")
        logger.info("SIP accept ok call_id=%s", call_id)
        return True

    def schedule_sideband(self, call_id: str, *, caller_phone: str = "", call_direction: str = "") -> None:
        existing = self._sideband_tasks.get(call_id)
        if existing and not existing.done():
            logger.info("sideband already running for call_id=%s", call_id)
            return
        if existing:
            self._sideband_tasks.pop(call_id, None)

        async def _run() -> None:
            try:
                await self._run_sideband(call_id, caller_phone=caller_phone, call_direction=call_direction)
            except Exception:
                logger.exception("sideband failed for call_id=%s", call_id)
            finally:
                self._sideband_tasks.pop(call_id, None)

        task = asyncio.create_task(_run())
        self._sideband_tasks[call_id] = task
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    def _build_slim_accept_payload(self) -> dict[str, Any]:
        """Accept body — lock voice + reasoning immediately; tools pushed on sideband session.update."""
        payload: dict[str, Any] = {
            "type": "realtime",
            "model": self._model,
            "instructions": build_phone_accept_instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "output": self._locked_audio_output(),
            },
        }
        if _reasoning_minimal():
            payload["reasoning"] = {"effort": "minimal"}
        return payload

    def _build_accept_payload(self) -> dict[str, Any]:
        """Full accept payload (legacy/tests). Prefer slim accept + sideband session.update."""
        payload: dict[str, Any] = {
            **self._build_slim_accept_payload(),
            "tools": pen_challenge_tool_definitions(),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "output_modalities": ["audio"],
            "audio": {
                "input": _sip_audio_input_opening(),
                "output": self._locked_audio_output(),
            },
        }
        if _reasoning_minimal():
            payload["reasoning"] = {"effort": "minimal"}
        return payload

    async def _accept_call(self, call_id: str, payload: dict[str, Any]) -> int:
        url = OPENAI_REALTIME_ACCEPT.format(call_id=call_id)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        r = await self._http.post(url, headers=headers, json=payload)
        level = "info" if r.status_code < 400 else "error"
        getattr(logger, level)(
            "accept call %s for call_id=%s body=%s",
            r.status_code,
            call_id,
            r.text[:400],
        )
        return r.status_code

    async def _connect_sideband(self, call_id: str) -> Any:
        ws_url = OPENAI_REALTIME_WS.format(call_id=call_id)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        return await websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            open_timeout=12,
        )

    async def _connect_sideband_with_retry(self, call_id: str) -> Any:
        delays_s = (0.05, 0.1, 0.15, 0.25, 0.4, 0.6, 1.0, 1.5, 2.5, 4.0)
        last_exc: Exception | None = None
        for attempt, delay in enumerate(delays_s, start=1):
            if attempt > 1:
                await asyncio.sleep(delay)
            try:
                return await self._connect_sideband(call_id)
            except websockets.exceptions.InvalidStatus as exc:
                last_exc = exc
                logger.warning(
                    "sideband WS attempt %s/%s for call_id=%s: %s",
                    attempt,
                    len(delays_s),
                    call_id,
                    exc,
                )
        assert last_exc is not None
        raise last_exc

    async def _run_sideband(self, call_id: str, *, caller_phone: str = "", call_direction: str = "") -> None:
        session = CallSession()
        session.call_id = call_id
        session.lead.call_id = call_id
        session.lead.channel = "phone"
        if call_direction:
            session.lead.call_direction = call_direction
        if caller_phone.strip():
            session.lead.set_value("phone", caller_phone.strip())
            session.lead.append_log(f"Inbound caller: {caller_phone.strip()}")
            logger.info("SIP caller phone call_id=%s phone=%s", call_id, caller_phone[:6] + "…")
        try:
            from voice_dashboard_store import register_active_session, upsert_call_record

            register_active_session(call_id, {"channel": "phone", "scenario": "pen"})
            upsert_call_record(session.lead)
        except Exception:
            pass
        prefetched = self._tools.prefetched_wiki_context()
        if prefetched:
            session.wiki_context = prefetched

        ws = await self._connect_sideband_with_retry(call_id)

        opening_watchdog: asyncio.Task[None] | None = None
        try:
            async with ws:
                opening_watchdog = asyncio.create_task(
                    self._opening_turn_detection_watchdog(ws, session)
                )
                opening = pick_pen_opening()
                # Sequential: full session before first TTS avoids racey first-call glitches.
                await self._push_full_session_instructions(ws, opening)
                await self._send_opening_greeting(ws, opening)
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(ws, event, session)
        except Exception:
            logger.exception("sideband ended for call_id=%s", call_id)
        finally:
            if opening_watchdog and not opening_watchdog.done():
                opening_watchdog.cancel()
            try:
                await asyncio.to_thread(maybe_post_voice_call_summary, session.lead)
            except Exception:
                logger.exception("voice call summary failed call_id=%s", call_id)

    async def _send_session_update(self, ws: Any, session_fields: dict[str, Any]) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": _realtime_session_patch(**session_fields),
                }
            )
        )

    async def _push_full_session_instructions(self, ws: Any, opening: PenOpening) -> None:
        """Full session after slim accept — tools, audio, pen-challenge rules."""
        fields: dict[str, Any] = {
            "instructions": build_pen_call_instructions(opening),
            "tools": pen_challenge_tool_definitions(),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "output_modalities": ["audio"],
            "audio": {
                "input": _sip_audio_input_opening(),
                "output": self._locked_audio_output(),
            },
        }
        if _reasoning_minimal():
            fields["reasoning"] = {"effort": "minimal"}
        await self._send_session_update(ws, fields)

    async def _wait_for_session_updated(
        self, ws: Any, session: CallSession, timeout_s: float = 2.5
    ) -> bool:
        """Drain WS until session.updated (confirms audio config) or timeout."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(0.35, remaining))
            except asyncio.TimeoutError:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle_event(ws, event, session)
            if event.get("type") == "session.updated":
                return True

    async def _send_opening_greeting(self, ws: Any, opening: PenOpening) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": format_phone_opening_response_create(opening),
                    },
                }
            )
        )

    async def _handle_event(self, ws: Any, event: dict[str, Any], session: CallSession) -> None:
        etype = event.get("type", "")
        if etype == "response.created":
            response = event.get("response") or {}
            rid = str(response.get("id") or "").strip()
            if rid and not session.opening_response_id:
                session.opening_response_id = rid
        elif etype == "output_audio_buffer.stopped":
            if not session.opening_response_finished:
                await self._finish_opening_phase(ws, session)
        elif etype == "session.updated":
            logger.debug("session.updated keys=%s", list((event.get("session") or {}).keys()))
        elif etype == "response.done":
            await self._handle_response_done(ws, event, session)
        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript") or "").strip()
            if not transcript:
                return
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            role = str((item or {}).get("role") or "").strip().lower()
            if role == "assistant":
                return
            session.lead.append_log(f"Visitor: {transcript[:220]}")
            try:
                from voice_dashboard_store import register_active_session, session_log_to_transcript, update_active_session, upsert_call_record

                if session.call_id:
                    register_active_session(
                        session.call_id,
                        {"channel": "phone", "scenario": "pen", "values": dict(session.lead.values or {})},
                    )
                    upsert_call_record(session.lead)
                    update_active_session(
                        session.call_id,
                        {"transcript": session_log_to_transcript(session.lead.session_log or [])},
                    )
            except Exception:
                pass
        elif etype == "error":
            err = event.get("error") or {}
            logger.warning("realtime error: %s", err)
            code = err.get("code") if isinstance(err, dict) else None
            if code == "missing_required_parameter" and not session.opening_response_finished:
                logger.error("SIP session.update rejected — opening phase may stay locked")

    def _locked_audio_output(self) -> dict[str, Any]:
        from realtime_voice_config import realtime_audio_output

        return dict(realtime_audio_output())

    async def _enable_conversation_turn_detection(self, ws: Any) -> None:
        await self._send_session_update(
            ws,
            {
                "audio": {
                    "input": _sip_audio_input_conversation(),
                    "output": self._locked_audio_output(),
                },
            },
        )

    async def _finish_opening_phase(self, ws: Any, session: CallSession) -> None:
        if session.opening_response_finished:
            return
        session.opening_response_finished = True
        logger.info(
            "SIP opening complete — enabling conversation turn_detection; "
            "waiting for caller reply to pen discovery question"
        )
        await self._enable_conversation_turn_detection(ws)
        # Do not response.create here — caller answers the opening pen question first.
        # Their speech triggers the model via create_response=True on conversation VAD.

    async def _opening_turn_detection_watchdog(self, ws: Any, session: CallSession) -> None:
        """Unlock duplex VAD if output_audio_buffer.stopped never arrives."""
        try:
            await asyncio.sleep(_sip_opening_watchdog_seconds())
            if session.opening_response_finished:
                return
            logger.warning("SIP opening watchdog: forcing conversation turn_detection")
            await self._finish_opening_phase(ws, session)
        except asyncio.CancelledError:
            return

    async def _handle_response_done(
        self, ws: Any, event: dict[str, Any], session: CallSession
    ) -> None:
        response = event.get("response") or {}
        rid = str(response.get("id") or "").strip()
        if rid and not session.opening_response_id:
            session.opening_response_id = rid
        output = response.get("output") or []
        calls: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call":
                continue
            if item.get("status") not in (None, "completed"):
                continue
            calls.append(item)
        if not calls:
            return

        async def _run_one(item: dict[str, Any]) -> tuple[str, str, str]:
            name = str(item.get("name", ""))
            tool_call_id = str(item.get("call_id", ""))
            args = parse_tool_arguments(item.get("arguments"))
            logger.info("tool call %s call_id=%s", name, tool_call_id)
            output_text = await asyncio.to_thread(self._tools.execute, session, name, args)
            return tool_call_id, output_text, name

        results = await asyncio.gather(*[_run_one(item) for item in calls])
        signup_unlocked = False
        for tool_call_id, output_text, name in results:
            if name in _SIP_CAPTURE_TRIGGER_TOOLS:
                signup_unlocked = True
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": tool_call_id,
                            "output": output_text,
                        },
                    }
                )
            )
        if signup_unlocked and not session.capture_stt_enabled:
            await self._enable_capture_transcription(ws, session)
        await ws.send(json.dumps({"type": "response.create"}))

    async def _enable_capture_transcription(self, ws: Any, session: CallSession) -> None:
        """Upgrade SIP STT to capture-quality once signup tools fire."""
        session.capture_stt_enabled = True
        capture_model = _sip_transcription_config_capture().get("model", "")
        default_model = _sip_transcription_config().get("model", "")
        if capture_model == default_model:
            logger.info(
                "SIP capture STT upgrade skipped — model already %s (kill switch active)",
                capture_model,
            )
            return
        logger.info(
            "SIP capture STT upgrade %s -> %s (signup phase active)",
            default_model,
            capture_model,
        )
        await self._send_session_update(
            ws,
            {
                "audio": {
                    "input": _sip_audio_input_capture(),
                    "output": self._locked_audio_output(),
                },
            },
        )


_sip_service: SipRealtimeService | None = None


def get_sip_service(
    *,
    api_key: str,
    webhook_secret: str,
    get_retriever: Callable[[], Any],
) -> SipRealtimeService:
    global _sip_service
    if _sip_service is None:
        _sip_service = SipRealtimeService(
            api_key=api_key,
            webhook_secret=webhook_secret,
            get_retriever=get_retriever,
        )
        _sip_service.schedule_warmup()
    return _sip_service


def reset_sip_service_for_tests() -> None:
    global _sip_service
    _sip_service = None


async def handle_incoming_call_safe(service: SipRealtimeService, event: Any) -> None:
    """Await accept in webhook, then run sideband (OpenAI Realtime SIP pattern)."""
    try:
        call_id = str(event.data.call_id)
        caller_phone = extract_caller_phone_from_incoming_event(event)
        call_direction = "inbound"
        try:
            from outbound_telephony import resolve_sip_caller_for_summary

            caller_phone, call_direction = resolve_sip_caller_for_summary(caller_phone)
            if not call_direction:
                call_direction = "inbound"
        except ImportError:
            pass
        logger.info("SIP incoming call_id=%s caller_phone=%s direction=%s", call_id, caller_phone or "(unknown)", call_direction)
        if await service.accept_incoming_call(call_id):
            service.schedule_sideband(call_id, caller_phone=caller_phone, call_direction=call_direction)
    except Exception:
        logger.exception("incoming SIP call handler failed")
