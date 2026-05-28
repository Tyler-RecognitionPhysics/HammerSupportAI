# Phone voice demo (OpenAI Realtime SIP + Twilio)

The marketing site is **phone-first**: visitors tap **Call Tyler** (`tel:` link). Voice runs on OpenAI Realtime over **SIP**, not in the browser.

Browser WebRTC is optional for local dev only: set `VITE_ENABLE_BROWSER_VOICE=1` in `web/.env`.

The floating **text chat** bubble is on by default (uses `/api/chat`). Hide it with `VITE_ENABLE_CHAT=0` in `web/.env`.

## Architecture

| Piece | Role |
| --- | --- |
| **Vercel** (or static host) | Landing page + `/api/*` (wiki, chat, Zapier, lead) |
| **Always-on host** (Fly.io recommended) | `POST /api/realtime/sip-webhook` + WebSocket tool sideband |
| **Twilio** | Phone number → Elastic SIP trunk → OpenAI |
| **OpenAI** | `realtime.call.incoming` webhook → accept call → `gpt-realtime-2` |

## What you need to provide

1. **Demo phone number** (E.164, e.g. `+15125550199`) for the site and Twilio.
2. **OpenAI** project with Realtime SIP enabled:
   - `OPENAI_API_KEY`
   - `OPENAI_WEBHOOK_SECRET` (from [Project → Webhooks](https://platform.openai.com/settings))
   - Project ID (`proj_…`) for the SIP URI
3. **Twilio** Elastic SIP trunk pointing at `sip:{PROJECT_ID}@sip.api.openai.com;transport=tls`
4. **Same Zapier + Hammer Office env** as the existing demo (`ZAPIER_LEAD_WEBHOOK_URL`, etc.)

## Server environment (telephony host)

Copy `server/.env.example` and set:

```env
OPENAI_API_KEY=sk-...
OPENAI_WEBHOOK_SECRET=whsec_...
REALTIME_SALES_TELEPHONY=1

# Shown on the site (also overridable in wiki/demo-public-site-copy.md)
DEMO_PHONE_NUMBER=+15125550199
DEMO_PHONE_DISPLAY=(512) 555-0199

REALTIME_SALES_REPO_ROOT=C:\path\to\Hammer-Sell-me-a-pen-challenge
ZAPIER_LEAD_WEBHOOK_URL=...
ZAPIER_APPROVAL_CALLBACK_SECRET=...
HAMMER_OFFICE_EMAIL=...
HAMMER_OFFICE_PASSWORD=...
```

Optional voice tuning:

```env
REALTIME_SALES_MODEL=gpt-realtime-2
# Voice is locked to `shimmer` @ 1.0 (same as browser WebRTC) in realtime_voice_config.py
# Phone PSTN default: medium — high eagerness false-triggers on line noise and sounds choppy
REALTIME_SALES_SIP_VAD_EAGERNESS=medium
# Fallback delay before greeting if session.updated is slow (seconds)
REALTIME_SALES_SIP_FIRST_TTS_SETTLE_S=0.4
# Opening guard — keeps hello from cutting off the intro (seconds)
REALTIME_SALES_SIP_OPENING_GUARD_S=18
```

Phone calls **accept with shimmer + audio output locked immediately**, then sideband `session.update` with full instructions. The server waits for `session.updated` before the first TTS to avoid startup chop. The greeting stays protected until `output_audio_buffer.stopped` (not merely `response.done`).

**Phone vs browser:** PSTN/SIP is narrowband (telephony codec) — it will never match WebRTC PCM bit-for-bit, but voice preset, VAD, and startup timing are aligned with the browser demo. If Fly still has `REALTIME_SALES_SIP_VAD_EAGERNESS=high`, re-sync secrets and redeploy.

## OpenAI webhook

1. [Settings → Project → Webhooks](https://platform.openai.com/settings) → add endpoint:
   - **URL:** `https://YOUR-TELEPHONY-HOST/api/realtime/sip-webhook`
   - **Events:** `realtime.call.incoming`
2. Copy the signing secret → `OPENAI_WEBHOOK_SECRET`.

## Twilio (summary)

Follow [OpenAI + Twilio Elastic SIP](https://www.twilio.com/en-us/blog/developers/tutorials/product/openai-realtime-api-elastic-sip-trunking):

1. Buy a number in Twilio.
2. Create an **Elastic SIP Trunk** with termination URI `sip:{proj_id}@sip.api.openai.com;transport=tls`.
3. Associate the number with the trunk.
4. **IP Access Control (required if calls show Failed / 0 sec in Twilio):**  
   Twilio Console → your SIP trunk → **Termination** → **IP Access Control Lists** → allow OpenAI SIP ranges ([OpenAI SIP docs](https://platform.openai.com/docs/guides/realtime-sip)):
   - `13.79.45.80/28`
   - `23.98.140.64/28`
   - `40.67.149.176/28`
   - `40.83.204.240/28`

## One OpenAI project everywhere (critical)

Twilio, the webhook, and Fly must use the **same** `proj_…` id:

| Piece | Must match |
|-------|------------|
| Twilio origination URI | `sip:proj_XXXX@sip.api.openai.com;transport=tls` |
| OpenAI webhook | Created under **that** project → `realtime.call.incoming` |
| `OPENAI_API_KEY` on Fly | API key from **that** project |
| `OPENAI_WEBHOOK_SECRET` on Fly | Secret from **that** project's webhook |
| `OPENAI_PROJECT_ID` in `server/.env` | Same `proj_XXXX` (for docs/scripts) |

If Twilio shows **Failed** and **0 sec** with `To: sip:proj_…@sip.api.openai.com`, OpenAI never accepted the SIP leg. Fix IP allowlist and project/key alignment first — then check [OpenAI webhook delivery logs](https://platform.openai.com/settings) for `realtime.call.incoming`.

## Deploy telephony on Fly.io

From repo root (`Hammer-Sell-me-a-pen-challenge`):

```bash
cd demo/realtime-sales-demo
fly launch --no-deploy --config fly.toml   # first time; app name hammer-voice-telephony
./scripts/fly-secrets-from-env.ps1
cd ../..
fly deploy --config demo/realtime-sales-demo/fly.toml
```

Or: `.\scripts\deploy-fly-telephony.ps1` from repo root.

`fly.toml` and `Dockerfile` live next to this file. The app listens on port **8780**.

Point the OpenAI webhook at:

`https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook`

## Site / Vercel

Set on Vercel (public-safe):

```env
DEMO_PHONE_NUMBER=+15125550199
DEMO_PHONE_DISPLAY=(512) 555-0199
```

Wiki keys (optional override):

- `rt_demo_phone` / `rt_demo_phone_display` — shown in UI
- `rt_demo_phone_tel` — digits only if different from display

Do **not** put `OPENAI_WEBHOOK_SECRET` on Vercel unless you also run the SIP webhook there (not recommended on serverless).

## Why phone “worked on Vercel launch” but broke later

| What you tested | Where it runs | Breaks when… |
|-----------------|---------------|--------------|
| **Browser voice** (mic on site) | **Vercel** `hammer-finalsite` | `OPENAI_API_KEY` missing/wrong on Vercel only |
| **Call Tyler** (real phone) | **Twilio → OpenAI SIP → Fly** webhook | Twilio/Fly/OpenAI project drift — **not** fixed by Vercel redeploys |

Common regressions:

1. **Wrong number on site** (`DEMO_PHONE_NUMBER` must be your **Twilio** number, not your personal cell). In Twilio logs, **From** is usually **the caller** (your cell), not the Twilio DID — confirm under **SIP Trunk → Phone Numbers**.
2. **`OPENAI_API_KEY` rotated on Vercel** but **Fly secrets not updated** → SIP may connect but Tyler never accepts; or webhook/API key from different `proj_` than Twilio URI.
3. **`OPENAI_PROJECT_ID` / Twilio origination URI / OpenAI webhook** not the same project.
4. **Twilio Termination IP ACL** added without all four OpenAI ranges → **Failed / 0 sec** in Twilio (fix: add ranges or **remove ACL** to test).
5. **Fly scaled to zero** (cold) → missed webhooks; `fly.toml` now keeps `min_machines_running = 1`.

After any API key change in `server/.env`, sync everywhere:

```powershell
cd demo/realtime-sales-demo
.\scripts\sync-secrets-from-server-env.ps1   # Fly + Vercel from server/.env
fly deploy --config demo/realtime-sales-demo/fly.toml   # from repo root
```

Fly/Vercel **never** read `server/.env` from the container — only process env set by the scripts above.

## Test checklist

1. `GET https://hammer-voice-telephony.fly.dev/api/health` → `telephony_webhook_secret_configured: true`, `openai_project_id_configured: true`, `demo_phone_number` matches Twilio trunk, `zapier_voice_call_summary_webhook_configured: true`
2. Call the Twilio number → Tyler speaks the pen opener
3. Concede the pen → Hammer signup tools unlock
4. `capture_lead` → agreement email → reply **I approve** → PHASE B/C
5. **End-of-call Slack** (same Zap as browser): hang up → Catch Hook `event: voice_call_summary`, `channel: phone`, `phoneNumber` from caller ID — see `server/zapier/VOICE_CALL_SUMMARY_SLACK.md`

## Local SIP webhook (ngrok)

```bash
cd demo/realtime-sales-demo/server
pip install -r requirements.txt
uvicorn app:app --port 8780
# another terminal:
ngrok http 8780
```

Point OpenAI webhook to `https://….ngrok-free.app/api/realtime/sip-webhook` and configure Twilio to route to your OpenAI project SIP endpoint.

## Outbound "Call me" (optional)

Visitors can enter their phone number on the site; Twilio calls them and bridges to the **same** OpenAI SIP + Tyler sideband as inbound.

### Fly env (in addition to inbound SIP vars)

```env
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_OUTBOUND_ENABLED=1
TELEPHONY_PUBLIC_BASE_URL=https://hammer-voice-telephony.fly.dev
```

Use the same `DEMO_PHONE_NUMBER` as outbound caller ID. For Twilio trial accounts, destination numbers must be [Verified Caller IDs](https://help.twilio.com/articles/223180048).

Optional staging restrict:

```env
TWILIO_OUTBOUND_ALLOWLIST=+15551234567
```

### Endpoints (Fly only)

| Route | Role |
| --- | --- |
| `POST /api/telephony/callback` | Site submits phone + consent → starts Twilio outbound call |
| `GET /api/telephony/callback/{cid}` | Poll call status for UI |
| `POST /api/twilio/voice/outbound-bridge` | Twilio TwiML → `<Dial><Sip>` to OpenAI |
| `POST /api/twilio/voice/status` | Twilio status callbacks |

Inbound `tel:` dialing is unchanged — Elastic SIP trunk config stays as-is.

### CORS

Production sites POST to Fly cross-origin. Set on Fly:

```env
REALTIME_SALES_CORS_ORIGINS=https://www.hammertime.com,https://hammer-finalsite.vercel.app,http://127.0.0.1:5173
```

(`fly-secrets-from-env.ps1` adds a default list.)
