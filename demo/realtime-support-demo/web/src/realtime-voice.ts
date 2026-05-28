/**
 * Locked Realtime TTS voice for every live demo session (browser + server WebRTC accept).
 * Canonical Hammer demo: gpt-realtime-2 + shimmer @ 1.0 + reasoning minimal + VAD high.
 * Do not override via env (prevents a different voice on each reconnect).
 */
export const REALTIME_DEFAULT_VOICE = "shimmer";

/** `audio.output.speed` — platform default 1.0 */
export const REALTIME_DEFAULT_VOICE_SPEED = 1;

// format is intentionally omitted — OpenAI now rejects a string for
// session.audio.output.format; WebRTC sessions fix encoding at the transport level.
export const REALTIME_AUDIO_OUTPUT = {
  voice: REALTIME_DEFAULT_VOICE,
  speed: REALTIME_DEFAULT_VOICE_SPEED,
};

/** Minimal transport surface for re-locking voice output mid-session. */
type VoiceOutputTransport = {
  sendEvent: (event: { type: string; session: Record<string, unknown> }) => void;
};

/**
 * Re-apply locked voice + speed after updateAgent or any partial session.update.
 * Do not set RealtimeAgent.voice — it injects a legacy top-level voice field that can
 * drop GA audio.output.speed on agent handoffs (pen → Hammer, wiki patch, etc.).
 */
export function reapplyLockedVoiceOutput(transport: VoiceOutputTransport): void {
  transport.sendEvent({
    type: "session.update",
    session: {
      type: "realtime",
      audio: {
        output: REALTIME_AUDIO_OUTPUT,
      },
    },
  });
}

/** Update the live agent and immediately re-lock voice output speed (shimmer @ 1.0). */
export async function updateLiveVoiceAgent(
  session: { updateAgent: (agent: unknown) => Promise<unknown>; transport: VoiceOutputTransport },
  agent: unknown,
): Promise<void> {
  await session.updateAgent(agent);
  reapplyLockedVoiceOutput(session.transport);
}
