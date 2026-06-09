# Go live today — hammertime.com

## What runs where (final)

| Service | URL |
|---------|-----|
| **Website + chat + Zapier APIs** | `https://www.hammertime.com` (Vercel) |
| **Phone Tyler (OpenAI webhook)** | `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook` (Fly) |
| **Zapier “I approve”** | `https://hammer-finalsite.vercel.app/api/zapier/approval` (until hammertime.com DNS → hammer-finalsite) |
| **Twilio SIP** | `sip:proj_YOUR_ID@sip.api.openai.com;transport=tls` |

---

## Step 1 — Vercel website (30–60 min)

1. Push this repo to GitHub.
2. [vercel.com](https://vercel.com) → Import project → **root** = repo root (where `vercel.json` is).
3. Run locally (optional check):
   ```powershell
   cd demo\realtime-sales-demo\web
   npm ci
   npm run build
   ```
4. Set env vars on Vercel (Production):
   ```powershell
   $env:VERCEL_TOKEN = "your-token"
   .\scripts\push-all-env-to-vercel.ps1 -ProjectName YOUR_VERCEL_PROJECT
   ```
   Or paste from `server/.env` manually — see `GO-LIVE-VERCEL.md`.
5. **Domains:** add `www.hammertime.com` and `hammertime.com` in Vercel → point DNS.
6. **Redeploy** after env vars.

**Test:** `https://www.hammertime.com/api/health` → `"ok": true`

---

## Step 2 — Fly phone server (45–90 min)

1. Install Fly: https://fly.io/docs/hands-on/install-flyctl/
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   ```
2. Login and deploy:
   ```powershell
   cd demo\realtime-sales-demo
   fly auth login
   fly launch --config fly.toml --no-deploy
   .\scripts\fly-secrets-from-env.ps1
   ```
3. Add webhook secret (from OpenAI, step 3):
   ```powershell
   fly secrets set OPENAI_WEBHOOK_SECRET=whsec_...
   ```
4. Deploy:
   ```powershell
   fly deploy --config fly.toml
   ```
5. Note app URL, e.g. `https://hammer-voice-telephony.fly.dev`

Optional custom domain: `voice.hammertime.com` → CNAME to Fly app.

---

## Step 3 — OpenAI webhook (10 min)

1. https://platform.openai.com/settings → **General** → copy `proj_…` into `server/.env` as `OPENAI_PROJECT_ID=`
2. **Webhooks** → Create:
   - URL: `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook`
   - Event: `realtime.call.incoming`
3. Copy `whsec_…` → Fly: `fly secrets set OPENAI_WEBHOOK_SECRET=whsec_...`

---

## Step 4 — Twilio (30 min)

1. Buy voice number.
2. SIP trunk → Origination: `sip:proj_YOUR_ID@sip.api.openai.com;transport=tls`
3. Attach number to trunk.
4. `DEMO_PHONE_NUMBER` + `DEMO_PHONE_DISPLAY` on Vercel → redeploy.
5. `.\scripts\sync-demo-phone-to-wiki.ps1` (optional wiki sync).

**Test:** call the number — Tyler answers.

---

## Quick local check before DNS

```powershell
cd demo\realtime-sales-demo
.\scripts\telephony-preflight.ps1
.\1-START-LOCAL-API.ps1
```

New terminal: `.\2-START-LOCAL-WEB.ps1` → http://127.0.0.1:5173

---

## You must do manually (we cannot log in for you)

- [ ] Vercel project + DNS for hammertime.com
- [ ] OpenAI webhook + project ID
- [ ] Twilio number + SIP trunk
- [ ] Fly account + `fly auth login`
