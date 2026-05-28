# Hammer Support AI

Customer support voice + chat with wiki-grounded retrieval, Slack ingest, and HubSpot KB sync.

## Live URLs (Vercel)

| URL | Purpose |
|-----|---------|
| `/` | Customer support (voice + chat) |
| `/admin/support` | Support Control admin dashboard |
| `/api/health` | API health check |

Sign in to the admin dashboard with your `SUPPORT_ADMIN_SECRET` (set in Vercel project env).

## Local development

See [demo/realtime-support-demo/README.md](demo/realtime-support-demo/README.md).

```powershell
cd demo/realtime-support-demo
.\START-LOCAL.ps1
```

## Deploy

This repo is configured for Vercel via [vercel.json](vercel.json). See [GO-LIVE-SUPPORT-VERCEL.md](GO-LIVE-SUPPORT-VERCEL.md) for environment variables and ElevenLabs Custom LLM setup.

## Repo layout

- `demo/realtime-support-demo/` — web UI + FastAPI server
- `api/support-index.py` — Vercel serverless entry (Mangum)
- `wiki-support/` — synthesized support Q&A wiki
- `raw/support-data/` — Slack + HubSpot KB exports
- `knowledge_support/` — BM25 index scripts + playbook
