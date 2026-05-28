# First-Pass Capture Accuracy

How Hannah captures email and phone correctly the first time without adding
per-turn latency. Mirrors the approach Grok Voice uses on the Starlink hotline:
one-breath read-back, NATO phonetic only on confusable letters, scoped STT
upgrade for capture turns.

## Latency contract

Every change here is filtered through one rule: it must either save a full turn
or stay latency-neutral on non-capture turns.

| Change | Per-turn cost | Scope | Net effect |
| --- | --- | --- | --- |
| One-breath read-back (no separate "Is that exactly right?" beat) | 0 ms | Capture turns only | Saves ~1-3 s end-to-end |
| FIRST-PASS CAPTURE prompt block | +20-40 ms TTFT (prewarmed) | All turns | Saves a turn whenever Hannah would have re-asked |
| `language: "en"` lock on SIP transcription | -10-30 ms | All SIP turns | Faster |
| `prompt:` brand/domain bias on SIP transcription | 0 ms | All SIP turns | Free |
| Phase-scoped `gpt-4o-transcribe` (SIP capture turns) | +100-300 ms | 3-6 capture turns per signup | Acceptable trade-off |
| Server-side capture guard | +1-3 ms | One call per `capture_lead` | Negligible |

## Browser path (ElevenLabs dashboard)

Apply manually on the agent ID set in `ELEVENLABS_AGENT_ID`. Only the items
below — do **not** touch VAD timing or the STT model (both regress per-turn
latency for marginal accuracy gains).

1. **Language: lock to English (`en`).** Removes auto-detect drift on US calls.
2. **Custom vocabulary / boosted keywords (free):**
   ```
   Hammer, Hammertime, MarketPoster, DealerBids, Hannah, AIA, Facebook,
   Marketplace, Craigslist, CDK, NADA, VinSolutions, DealerSocket, Reynolds,
   Dominion, Promax, Dealertrack, Gmail, Outlook, Yahoo, Hotmail, iCloud,
   Proton, Fastmail, Comcast, AOL, MSN, Live
   ```
3. **Do NOT bump `silence_end_of_turn_ms`.** It costs 300-700 ms before every
   reply, not just capture turns.
4. **Do NOT swap to Scribe globally.** Test it on a separate agent ID first and
   A/B compare end-to-end latency before promoting.

## Phone / SIP path (in code)

Default config — keeps `gpt-4o-mini-transcribe`, adds language + brand prompt
bias for free. See `_sip_transcription_config()` in
[server/sip_realtime.py](server/sip_realtime.py).

Capture-phase upgrade — `_sip_transcription_config_capture()` swaps in
`gpt-4o-transcribe` only after a signup-related tool fires
(`begin_hammer_signup`, `skip_pen_challenge`, `capture_lead`,
`open_hammer_account_form`, `fill_hammer_account_field`).

### Kill switches

| Env var | Default | What it does |
| --- | --- | --- |
| `REALTIME_SALES_SIP_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Overrides STT for all SIP turns. Rarely needed. |
| `REALTIME_SALES_SIP_CAPTURE_TRANSCRIPTION_MODEL` | `gpt-4o-transcribe` | Overrides STT for capture turns. Set to `gpt-4o-mini-transcribe` to disable the upgrade without redeploying. |

Apply with `fly secrets set REALTIME_SALES_SIP_CAPTURE_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe -a hammer-voice-telephony`.

## Prompt rules (source of truth)

[web/src/voice-contact-readback.ts](web/src/voice-contact-readback.ts) defines
the single read-back rule that both the browser and SIP prompts import.

Key behaviors enforced:

- One-breath read-back ending in "that right?" — never a second confirm beat.
- NATO phonetic only on the confusable set (M/N, B/D/P/T/V/Z, F/S/X, I/E/Y, A/8,
  J/K, G/J, U/Q). Other letters stay natural.
- Provider domains (Gmail, Outlook, etc.) spoken as names, not spelled.
- Phone read as area / prefix / line in one breath.
- Full letter-by-letter spelling is the **fallback** — used only after a
  correction, for unusual local parts, or when the caller asks.
- After two failed corrections on the same value, switch to full spelling once,
  then accept whatever they confirm. No third loop.

## Server-side guard

`_suspicious_capture_warning()` in
[server/voice_tools.py](server/voice_tools.py) runs before
`capture_lead` posts to Zapier. It returns a one-line guidance string the model
can recover from in the same turn — does **not** block the call, just nudges
Hannah to re-confirm a likely STT slip (single-character local part, all-digit
local part, ambiguous letter pair in a custom domain, etc.).

## Measuring the change

After deploy:

1. Browser console (`VITE_VOICE_LATENCY_DEBUG=1`) — confirm signup turns are
   not slower than before for non-capture turns.
2. Fly logs — look for `SIP capture STT upgrade gpt-4o-mini-transcribe -> gpt-4o-transcribe`
   on the first signup-tool fire of each call.
3. Real call: time from "What's the best email?" to "got the agreement at that
   same email?" — should be roughly one fewer round-trip than before.
