# Zap — end-of-call voice summary → Slack

Posts when a **browser or phone** voice session ends and **any contact info** was captured (email, phone, name, dealership, or agreement email sent).

## Create the Zap

1. **Trigger:** Webhooks by Zapier → **Catch Hook** (new hook — do not reuse lead or website hooks).
2. Copy the hook URL into **`ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL`** (`server/.env`, **Fly secrets**, and **Vercel** — browser summaries post from Vercel `/api/voice/call-summary`).
3. **Filter (recommended):** Only continue if `event` **equals** `voice_call_summary`.
4. **Action:** Slack → Send Channel Message (or DM).

## Slack message (recommended)

Dialer-style, simplified for Slack. Easiest: post **`interactionSummary`** only (one field, already formatted).

Copy into **Slack → Message Text**:

```
{{interactionSummary}}
```

Optional status chip on top:

```
*{{notes}}*
{{interactionSummary}}
```

In the Slack step: **Unfurl links = off**.

### Example (what reps see)

```
*Speedlag · David · Phone · 8 min*
+15551234567 · david@speedlag.com

*Summary*
Call with David at Speedlag about Hammer AI lead engagement. ~20 leads from third-party sites; no AI today. Boss not available — David asked for info by email.

*Decisions*
• Decision deferred — needs manager or owner
• Requested details by email
• Agreement thread started — david@speedlag.com

*Next*
• Send agreement and product info to david@speedlag.com
• Follow up for I approve — david@speedlag.com
```

### Separate Zap fields (optional)

| Field | Use |
|-------|-----|
| `callSummary` | Summary paragraph only |
| `decisionsAndAgreements` | Bullet list (already has `•`) |
| `actionItems` | Next steps bullets |
| `interactionSummary` | Full message (header + all sections) |

Label fields for filters: `agreementEmailSentLabel`, `agreementApprovedLabel`, `accountCreatedLabel` (Yes / No).

## When it fires

| Channel | How |
|---------|-----|
| **Browser (ElevenLabs WebRTC)** | **Primary:** ElevenLabs **Post-call webhook** → `POST /api/elevenlabs/call-end` (Fly or Vercel) → Zapier. **Also:** user taps End call → `POST /api/voice/call-summary` as backup. |
| **Phone (OpenAI SIP)** | Call disconnect → sideband `finally` → server posts to Zapier |
| **Phone (ElevenLabs SIP)** | Same as browser — ElevenLabs post-call webhook |

### ElevenLabs post-call webhook (required for browser Slack)

In **ElevenLabs → Agents → your agent → Post-call webhook**:

- **URL:** `https://hammer-voice-telephony.fly.dev/api/elevenlabs/call-end` (or `https://hammer-finalsite.vercel.app/api/elevenlabs/call-end` if you prefer Vercel)
- Copy the **Signing secret** into **`ELEVENLABS_WEBHOOK_SECRET`** on Fly **and** Vercel (`fly-secrets-from-env.ps1` + `scripts/push-all-env-to-vercel.ps1`)

If Fly logs show `invalid HMAC signature`, the signing secret does not match the dashboard — fix the env var, or leave it unset (summaries still post; set `ELEVENLABS_WEBHOOK_STRICT=1` only if you want to reject bad signatures).

Minimum gate: **any** of email (with `@`), phone (5+ digits), name, dealership name, or **`capture_lead_fired`** / agreement email sent.

**Phone (Twilio):** caller ID is taken from the SIP **`From`** header at call start, so Slack usually fires even if the visitor hangs up before PHASE B. No extra Twilio webhook.

**Browser:** posts on **End call** when Tyler collected contact info (e.g. email in PHASE A) — phone is not required.

**Production:** set `ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL` on **Fly** (phone path) **and Vercel** (browser path). Use `fly-secrets-from-env.ps1` + `push-all-env-to-vercel.ps1`, then redeploy both.

## Test

**Browser**

1. Set `ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL` in `server/.env`; restart API.
2. Short voice call; give phone in PHASE B; end call.
3. Zapier Catch Hook: `event: voice_call_summary`, `channel: browser`.

**Phone**

1. Same Zap URL on Fly secrets; deploy.
2. Call the Twilio demo number; talk briefly; hang up.
3. Zapier: `channel: phone`, `phoneNumber` = your cell (from SIP From), `interactionSummary` header shows `Phone`.

## Health check

`GET /api/health` includes `zapier_voice_call_summary_webhook_configured` and `zapier_voice_call_summary_webhook_hook_id`.
