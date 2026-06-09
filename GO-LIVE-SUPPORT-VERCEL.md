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
| `HUBSPOT_PRIVATE_APP_TOKEN` | For HubSpot KB + tickets sync on Fly |
| `SLACK_BOT_TOKEN` | For Slack sync |
| `SLACK_SUPPORT_CHANNEL_ID` | For Slack sync |
| `SUPPORT_SERVERLESS` | Set to `1` on Vercel (auto in support-index.py) |
| `SUPPORT_CORS_ORIGINS` | Your production URL(s) |
| `SUPPORT_SYNC_HOST_URL` | Fly sync host, e.g. `https://hammer-support-sync.fly.dev` |
| `SUPPORT_KB_ARTIFACT_URL` | e.g. `https://hammer-support-sync.fly.dev/api/knowledge/artifact` |
| `SUPPORT_KB_ARTIFACT_TOKEN` | Same as `SUPPORT_ADMIN_SECRET` (or dedicated token) |
| `SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE` | Set to `1` only when ready for Hannah to create real HubSpot tickets |

## 3. ElevenLabs agent

1. Create a **new** Conversational AI agent (do not reuse sales Hannah agent).
2. LLM → **Custom LLM** → `https://<your-support-domain>/api/elevenlabs/llm`
3. Set greeting in dashboard or use server `SUPPORT_GREETING`.
4. Copy Agent ID → `ELEVENLABS_AGENT_ID`.

## 4. HubSpot tickets (persistent host + Vercel)

Vercel cannot run a 36k-ticket backfill (60s timeout, ephemeral disk). Use Fly.io as the persistent sync host:

```powershell
# One-time: deploy Fly sync host + 10GB volume
.\scripts\deploy-fly-support-sync.ps1

# Fast seed: upload your local pre-built support_kb.sqlite (~32MB)
.\scripts\seed-fly-support-sync.ps1

# Or run full backfill on Fly (takes ~10+ minutes)
.\scripts\trigger-support-tickets-sync.ps1 -FullBackfill
```

Set Vercel env vars (then redeploy):

```text
SUPPORT_SYNC_HOST_URL=https://hammer-support-sync.fly.dev
SUPPORT_KB_ARTIFACT_URL=https://hammer-support-sync.fly.dev/api/knowledge/artifact
SUPPORT_KB_ARTIFACT_TOKEN=<your SUPPORT_ADMIN_SECRET>
```

On each Vercel cold start, the API downloads `support_kb.sqlite` from Fly into `/tmp` so Hannah can search all indexed tickets.

From the Vercel dashboard, **Sync HubSpot Tickets** proxies to Fly (background job). After sync completes on Fly, use **Knowledge → reload** or redeploy to pull the latest artifact.

Keep `SUPPORT_ENABLE_HUBSPOT_TICKET_CREATE` unset or `0` until the support site is fully live. With the flag off, Hannah can still log sessions locally for testing, but HubSpot ticket writes are blocked.

## 5. Initial knowledge

1. Run Slack backfill locally or from Support Control after deploy.
2. Verify **Knowledge → Test search** in `/admin/support` (try “reset password”).
3. Confirm **HubSpot Tickets** shows ~36,730 indexed (not 7).
4. Smoke test voice + chat on 5–10 real support questions.

## 6. URLs

| Path | Purpose |
|------|---------|
| `/` | Customer support voice + chat |
| `/admin/support` | Support Control dashboard |
| `/api/health` | Health check |
