# Hammer Support AI

Customer support voice + chat agent with wiki-grounded retrieval and Slack channel ingest.

## Run locally (fastest path)

**One-time setup** (deps + knowledge index):

```powershell
cd HAMMERFINALSITE\demo\realtime-support-demo
.\0-SETUP-LOCAL.ps1
```

**Add API keys** in `server\.env`:

| Variable | Required for |
|----------|----------------|
| `OPENAI_API_KEY` | Text chat, Slack Q&A synthesis |
| `ELEVENLABS_API_KEY` + `ELEVENLABS_AGENT_ID` | Voice calls |
| `SLACK_BOT_TOKEN` + `SLACK_SUPPORT_CHANNEL_ID` | Slack ingest (optional locally) |
| `HUBSPOT_PRIVATE_APP_TOKEN` | HubSpot Help Center KB sync (optional) |

`SUPPORT_ADMIN_SECRET` is preset to `local-dev-support` for testing the admin dashboard.

**Start both servers** (opens two terminal windows + browser):

```powershell
.\START-LOCAL.ps1
```

Or manually in two terminals:

```powershell
# Terminal 1 — API on http://127.0.0.1:8781
cd server
py -3 -m uvicorn app:app --host 127.0.0.1 --port 8781 --reload

# Terminal 2 — web on http://localhost:5174
cd web
npm run dev
```

### Local URLs

| URL | Purpose |
|-----|---------|
| http://localhost:5174 | Customer support app (voice + chat) |
| http://127.0.0.1:8781/debug/support-dashboard | Admin — **Support Control** |
| http://127.0.0.1:8781/api/health | API health check |

Admin login password: value of `SUPPORT_ADMIN_SECRET` in `server/.env` (default `local-dev-support`).

After editing `.env`, restart the API terminal so uvicorn reloads secrets.

## Local development (detailed)

1. Copy `server/.env.example` to `server/.env` and set keys.
2. Build knowledge index (once):
   ```powershell
   py -3 knowledge_support\scripts\sync_sqlite.py
   ```
3. Start API (port **8781**):
   ```powershell
   cd demo\realtime-support-demo\server
   py -3 -m uvicorn app:app --host 127.0.0.1 --port 8781 --reload
   ```
4. Start web (port **5174**):
   ```powershell
   cd demo\realtime-support-demo\web
   npm ci
   npm run dev
   ```
5. Admin dashboard (local): http://127.0.0.1:8781/debug/support-dashboard

## Slack ingest

1. Create a Slack app with `channels:history`, `channels:read` (and `groups:history` for private channels).
2. Invite the bot to your support Q&A channel.
3. Set `SLACK_BOT_TOKEN` and `SLACK_SUPPORT_CHANNEL_ID` in `server/.env`.
4. Run sync from Support Control → **Sync Slack channel**, or:
   ```powershell
   py -3 -c "from demo.realtime_support_demo.server.slack_sync import run_slack_sync; print(run_slack_sync(full_backfill=True))"
   ```
   (from repo root with `SUPPORT_REPO_ROOT` set and server on PYTHONPATH)

## HubSpot Knowledge Base ingest

Imports articles from your [Hammer Help Center KB](https://app.hubspot.com/knowledge/3355079/206977575318/articles/state/all) into `raw/support-data/hubspot-kb/` and rebuilds the BM25 index so chat and voice can retrieve them.

1. In HubSpot, create a **private app** with scopes that include CMS/site search (e.g. `content`, `cms.domains.read`). See [HubSpot site search API](https://developers.hubspot.com/docs/api-reference/legacy/cms/site-search/guide).
2. Set `HUBSPOT_PRIVATE_APP_TOKEN` in `server/.env` (defaults: portal `3355079`, KB `206977575318`).
3. Support Control → Overview → **Sync HubSpot KB**, or from `server/`:
   ```powershell
   $env:SUPPORT_REPO_ROOT = "C:\path\to\HAMMERFINALSITE"
   py -3 -c "from hubspot_kb_sync import run_hubspot_kb_sync; print(run_hubspot_kb_sync())"
   ```
4. Re-run sync when articles change in HubSpot.

## Deploy (separate Vercel project)

1. Import repo; set **Root Directory** to `HAMMERFINALSITE`.
2. Override project config with `vercel-support.json` (rename or point Vercel settings to match).
3. Set environment variables from `server/.env.example`.
4. Create a **separate ElevenLabs agent** with Custom LLM URL: `https://<your-support-domain>/api/elevenlabs/llm`

## Architecture

- **wiki-support/** — LLM wiki (synthesized support Q&A)
- **raw/support-data/slack/** — immutable Slack thread exports
- **knowledge_support/** — BM25 SQLite index + playbook
- **demo/realtime-support-demo/** — voice (ElevenLabs WebRTC) + text chat + admin

Sales site (`demo/realtime-sales-demo`) is unchanged.
