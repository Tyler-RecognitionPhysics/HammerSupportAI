# Voice Latency Report

Updated: 2026-05-22

## What Changed

The implementation preserves full prompts and wiki grounding. It moves expensive work earlier, keeps shared caches hot, and adds timestamps so cold and warm paths can be measured from browser console logs and Fly logs.

## Instrumented Metrics

| Path | Metric | Where to read it |
| --- | --- | --- |
| Browser | tap feedback delay | Browser console, `VITE_VOICE_LATENCY_DEBUG=1`, label `tap_feedback` |
| Browser | token fetch/cache | Browser console labels `elevenlabs_token_fetch`, `elevenlabs_token_cache_hit`, `token_ready_for_start`; Fly log `elevenlabs_token route` |
| Browser | WebRTC connect | Browser console label `webrtc_on_connect` |
| Browser | first speaking event | Browser console label `first_speaking` |
| ElevenLabs LLM | first SSE byte | Fly log `elevenlabs_llm first_sse` |
| ElevenLabs LLM | GPT first content | Fly log `elevenlabs_gpt first_content` |
| Wiki/tooling | wiki retrieval and tool calls | Fly logs `wiki_query`, `search_wiki`, `tool done` |
| Phone | Twilio webhook timing | Fly logs `inbound-connect OK` and `outbound-bridge OK` |

## Cold vs Warm Budget

| Scenario | Cold path expectation | Warm path expectation | 300ms note |
| --- | --- | --- | --- |
| Browser tap to connected | Token API + WebRTC session setup + provider connect | Prefetched token + warmed backend + WebRTC setup | Full tap-to-audio under 300ms is unlikely unless the user already hovered/focused/pointered the CTA. |
| Browser user speech to reply start | Custom LLM + prompt build + GPT + ElevenLabs TTS | Cached executor/wiki + immediate SSE role chunk + GPT stream | Server first byte can be near the target; audible speech depends on ElevenLabs STT/TTS and network. |
| Phone answer to Hannah | Twilio disclosure + SIP bridge + ElevenLabs startup | Same disclosure + warmed Fly/ElevenLabs executor | Not a valid sub-300ms target because the legal disclosure intentionally plays first. |
| Phone disclosure end to Hannah | SIP bridge + ElevenLabs startup | Warmed Fly executor + optional pre-rendered disclosure audio | This is the correct first-call phone metric after compliance notice. |
| Phone user speech to reply start | ElevenLabs SIP + custom LLM + GPT | Cached executor/wiki + immediate SSE role chunk + GPT stream | Best measured from `elevenlabs_llm first_sse` and `elevenlabs_gpt first_content`. |

## Operational Settings

- Set `VITE_VOICE_LATENCY_DEBUG=1` in the browser build to show client-side latency marks.
- `VITE_ELEVENLABS_TOKEN_PREFETCH` defaults on; set it to `0` to disable strong-intent token prefetch.
- Keep Fly always warm with `min_machines_running = 1` and `auto_stop_machines = false`.
- Optional phone optimization: set `VOICE_PHONE_DISCLOSURE_AUDIO_URL` to a public WAV/MP3 file to use Twilio `<Play>` instead of runtime `<Say>` TTS.
- The Docker image now builds `knowledge/data/company_kb.sqlite` during image build, so production no longer pays that cost on first boot.

## How To Compare

1. Deploy the latest Fly image and web build.
2. Browser cold test: open an incognito window with `VITE_VOICE_LATENCY_DEBUG=1`, tap without hovering, capture console labels.
3. Browser warm test: hover/focus the voice CTA for 1-2 seconds before tapping, capture the same labels.
4. Phone cold test: after deploy, call the Twilio number once and inspect Fly logs for `inbound-connect`, `elevenlabs_llm first_sse`, and `elevenlabs_gpt first_content`.
5. Phone warm test: call again within a few minutes and compare the same labels.

The target for improvement is warmed turn-taking and disclosure-end-to-Hannah latency. Compliance disclosure time is intentionally outside the sub-300ms target.
