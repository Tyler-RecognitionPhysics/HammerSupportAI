"""Locked Realtime TTS voice for browser WebRTC accept and SIP — always platform default."""

from __future__ import annotations

# Canonical Hammer demo voice profile (browser WebRTC + phone SIP):
#   model: gpt-realtime-2  (REALTIME_SALES_MODEL env)
#   voice: shimmer @ speed 1.0  (locked below — env overrides ignored; must match web/src/realtime-voice.ts)
#   reasoning: minimal on SIP when REALTIME_SALES_SIP_REASONING_MINIMAL=1 (default)
#   VAD: semantic_vad eagerness medium on phone after opening (REALTIME_SALES_SIP_VAD_EAGERNESS)
REALTIME_DEFAULT_VOICE = "shimmer"
REALTIME_DEFAULT_VOICE_SPEED = 1.0


def realtime_audio_output() -> dict[str, float | str]:
    return {
        "voice": REALTIME_DEFAULT_VOICE,
        "speed": REALTIME_DEFAULT_VOICE_SPEED,
    }
