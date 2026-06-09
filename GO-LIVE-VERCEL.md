# Go live on Vercel — checklist

Use this when the site should replace the public Hammer website.

## 1. Vercel project

- Connect the GitHub repo containing this folder.
- **Root directory:** repo root (where `vercel.json` lives).
- **Framework preset:** Other (Vercel uses `vercel.json` commands).
- Do **not** change `outputDirectory` or `api/index.py` path unless you update `vercel.json` to match.

## 2. Required environment variables (Production)

Set these in **Vercel → Project → Settings → Environment Variables**.

| Variable | What it is |
|----------|------------|
| `OPENAI_API_KEY` | OpenAI billing key (server only) |
| `ZAPIER_LEAD_WEBHOOK_URL` | Voice Zap 1 Catch Hook (agreement email) |
| `ZAPIER_WEBSITE_LEAD_WEBHOOK_URL` | Website signup form Catch Hook (separate Zap) |
| `ZAPIER_APPROVAL_CALLBACK_SECRET` | Long random string; Zap 2 sends as `X-Zapier-Secret` |
| `HAMMER_OFFICE_EMAIL` | Staff login for office.hammer-corp.com automation |
| `HAMMER_OFFICE_PASSWORD` | Staff password (server only) |
| `REALTIME_SALES_PUBLIC_BASE_URL` | `https://www.hammertime.com` |
| `DEMO_PHONE_NUMBER` | E.164 Twilio number for Call Tyler button |
| `DEMO_PHONE_DISPLAY` | Display format e.g. `(512) 555-0199` |
| `REALTIME_SALES_ADMIN_SECRET` | Password for `/admin/voice` dashboard (e.g. `Admin` for testing) |
| `ELEVENLABS_API_KEY` | Browser voice STT/TTS (also powers live call history in dashboard) |
| `ELEVENLABS_AGENT_ID` | ElevenLabs Conversational AI agent ID |

Optional but useful:

| Variable | What it is |
|----------|------------|
| `REALTIME_SALES_CORS_ORIGINS` | Extra allowed origins if you use multiple domains |
| `HAMMER_OFFICE_USE_PLAYWRIGHT` | `1` for real account creation (required for signup) |
| `HAMMER_OFFICE_HEADLESS` | `1` on Vercel (no visible browser) |

**Never** add secrets as `VITE_*` — those are embedded in public JavaScript.

Template with comments: `demo/realtime-sales-demo/server/.env.example`

## 3. Zapier (required for signup flow)

| Zap | Purpose | Setup doc |
|-----|---------|-----------|
| Zap 1 | Send agreement email when customer signs up on call | `server/zapier/README.md` → Hammer Drive / Facebook AIA |
| Zap 2 | “I approve” email → unlock account creation | `server/zapier/ZAPIER_I_APPROVE_SETUP.md` |

Zap 2 must POST to:

`https://YOUR-DOMAIN/api/zapier/approval`

with header `X-Zapier-Secret: <same as ZAPIER_APPROVAL_CALLBACK_SECRET>`.

## 4. Custom domain

- Vercel → **Domains** → add your Hammer domain.
- Update `REALTIME_SALES_PUBLIC_BASE_URL` to that HTTPS URL.
- Redeploy after changing env vars.

## Phone voice (separate from Vercel)

OpenAI phone webhook must run on **Fly.io** (long calls). See **`GO-LIVE-TODAY.md`**.

- Website: `https://www.hammertime.com`
- Phone webhook: `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook` (set in OpenAI dashboard)

Do **not** put `OPENAI_WEBHOOK_SECRET` on Vercel unless testing only.

## 5. Smoke test after deploy

1. Open the site → **Start call** (microphone allowed).
2. `GET https://YOUR-DOMAIN/api/health` — should show `"ok": true` and `openai_configured: true` (no secret values).
3. Run a test lead with a test email; confirm Zap 1 fires.
4. Reply “I approve” from that inbox; confirm Zap 2 hits `/api/zapier/approval`.
5. Complete voice signup; confirm Hammer Office account is created.

## 6. Security (production)

- Debug routes `/api/debug/*` are **disabled** on Vercel automatically.
- Voice admin dashboard: **`https://YOUR-DOMAIN/admin/voice`** — requires `REALTIME_SALES_ADMIN_SECRET` (returns 404 if unset). Not linked from the public site.
- `/api/zapier/approval` **rejects** requests if `ZAPIER_APPROVAL_CALLBACK_SECRET` is missing in production.
- Do not commit `server/.env` — it is gitignored.

## 7. Helper script (optional)

`scripts/push-openai-key-to-vercel.ps1` — copies `OPENAI_API_KEY` from local `server/.env` into a Vercel project (requires Vercel CLI logged in).

## Troubleshooting

| Problem | Check |
|---------|--------|
| Voice won’t connect | `OPENAI_API_KEY` in Vercel Production + redeploy |
| Agreement email not sent | `ZAPIER_LEAD_WEBHOOK_URL`, Zap 1 filter `event = agreement_email_request` |
| “I approve” never works | Zap 2 URL, secret header, `ZAPIER_APPROVAL_CALLBACK_SECRET` |
| Account not created | `HAMMER_OFFICE_*` creds, `HAMMER_OFFICE_USE_PLAYWRIGHT=1` |
| Wrong homepage copy | Edit `wiki/demo-public-site-copy.md`, redeploy |
