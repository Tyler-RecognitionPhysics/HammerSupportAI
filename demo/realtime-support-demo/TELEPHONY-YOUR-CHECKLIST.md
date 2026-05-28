# Phone voice — what’s done vs what you do

## Already done in this repo

| Item | Status |
|------|--------|
| OpenAI API key in `server/.env` | Yes (used for chat + phone) |
| Zapier lead webhook + approval secret | Yes |
| Hammer Office credentials | Yes |
| SIP webhook route `/api/realtime/sip-webhook` | Yes |
| Pen + Hammer voice tools in Python | Yes |
| Site phone-first CTA (`tel:` link) | Yes |
| `REALTIME_SALES_TELEPHONY=1` + repo root in `.env` | Yes |
| Helper scripts (below) | Yes |

## Your checklist (~20 minutes)

### 1. OpenAI webhook (required)

1. Run `.\scripts\open-telephony-consoles.ps1` (or open [OpenAI Settings](https://platform.openai.com/settings)).
2. **General** → copy **Project ID** (`proj_…`).
3. Add to `server/.env` (uncomment and fill):
   ```env
   OPENAI_PROJECT_ID=proj_xxxxxxxx
   ```
4. **Webhooks** → **Create**:
   - **URL:** start with ngrok (step 3), then switch to Fly when deployed  
     `https://YOUR-HOST/api/realtime/sip-webhook`
   - **Event:** `realtime.call.incoming`
5. Copy signing secret into `server/.env`:
   ```env
   OPENAI_WEBHOOK_SECRET=whsec_...
   ```

### 2. Twilio number + SIP trunk

1. [Buy a voice number](https://console.twilio.com/us1/develop/phone-numbers/search).
2. [Create SIP trunk](https://console.twilio.com/us1/develop/voice/sip-trunks) → **Origination** → add:
   ```text
   sip:proj_YOUR_ID@sip.api.openai.com;transport=tls
   ```
   (Use the same `OPENAI_PROJECT_ID` from step 1.)
3. Trunk → **Phone Numbers** → attach your new number.
4. Add to `server/.env`:
   ```env
   DEMO_PHONE_NUMBER=+1XXXXXXXXXX
   DEMO_PHONE_DISPLAY=(XXX) XXX-XXXX
   ```
5. Update `wiki/demo-public-site-copy.md` keys `rt_demo_phone` and `rt_demo_phone_display` to match (or rely on env via `/api/health`).

### 3. Local test call

```powershell
cd demo\realtime-sales-demo
.\scripts\telephony-preflight.ps1
.\3-START-TELEPHONY-API.ps1
```

Second terminal:

```powershell
ngrok http 8780
```

- Paste ngrok URL into OpenAI webhook: `https://xxxx.ngrok-free.app/api/realtime/sip-webhook`
- Call your Twilio number → Tyler should open with the pen line.

### 4. Production host (recommended)

Fly is not installed on this machine yet.

1. Install: https://fly.io/docs/hands-on/install-flyctl/
2. From `demo/realtime-sales-demo`:
   ```powershell
   fly auth login
   fly launch --config fly.toml --no-deploy
   .\scripts\fly-secrets-from-env.ps1
   fly deploy --config fly.toml
   ```
3. Point OpenAI webhook to `https://YOUR-APP.fly.dev/api/realtime/sip-webhook`
4. On **Vercel**, set only `DEMO_PHONE_NUMBER` and `DEMO_PHONE_DISPLAY` (public).

## Helper commands

| Script | Purpose |
|--------|---------|
| `.\scripts\telephony-preflight.ps1` | Green/yellow checklist + SIP URI |
| `.\scripts\open-telephony-consoles.ps1` | Open Twilio + OpenAI tabs |
| `.\3-START-TELEPHONY-API.ps1` | API on :8780 for SIP |
| `.\scripts\fly-secrets-from-env.ps1` | Push `server/.env` secrets to Fly |

## What only you can do

- Create the OpenAI webhook (needs your OpenAI login).
- Buy/configure the Twilio number (billing + console).
- Run ngrok or deploy Fly (public HTTPS URL).
- Paste your real phone number into `.env` / wiki when you have it.

We **cannot** complete Twilio or OpenAI dashboard steps from code without your accounts.
