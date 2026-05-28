GO-LIVE-SUPPORT-VERCEL.md
# Hammer Support AI — Vercel deploy

Separate deployment from the sales site. Use this checklist when creating a **new Vercel project**.

## 1. Project settings

- **Root directory:** `HAMMERFINALSITE` (or repo root if monorepo only contains this folder)
- Copy settings from [`vercel-support.json`](vercel-support.json):
  - Build: `cd demo/realtime-support-demo/web && npm run build`
  - Output: `demo/realtime-support-demo/web/dist`
  - API function: `api/support-index.py`

## 2. Environment variables

From [`demo/realtime-support-demo/server/.env.example`](demo/realtime-support-demo/server/.env.example):

| Variable | Required |
|----------|----------|
| `OPENAI_API_KEY` | Yes |
| `ELEVENLABS_API_KEY` | Yes |
| `ELEVENLABS_AGENT_ID` | Yes (support-specific agent) |
| `SUPPORT_ADMIN_SECRET` | Yes (for `/admin/support`) |
| `SLACK_BOT_TOKEN` | For Slack sync |
| `SLACK_SUPPORT_CHANNEL_ID` | For Slack sync |
| `SUPPORT_SERVERLESS` | Set to `1` on Vercel (auto in support-index.py) |
| `SUPPORT_CORS_ORIGINS` | Your production URL(s) |

## 3. ElevenLabs agent

1. Create a **new** Conversational AI agent (do not reuse sales Hannah agent).
2. LLM → **Custom LLM** → `https://<your-support-domain>/api/elevenlabs/llm`
3. Set greeting in dashboard or use server `SUPPORT_GREETING`.
4. Copy Agent ID → `ELEVENLABS_AGENT_ID`.

## 4. Initial knowledge

1. Run Slack backfill locally or from Support Control after deploy.
2. Verify **Knowledge → Test search** in `/admin/support`.
3. Smoke test voice + chat on 5–10 real support questions.

## 5. URLs

| Path | Purpose |
|------|---------|
| `/` | Customer support voice + chat |
| `/admin/support` | Support Control dashboard |
| `/api/health` | Health check |
