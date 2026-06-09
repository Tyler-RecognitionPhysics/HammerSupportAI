# Support tickets — HubSpot + Slack (AI voice/chat)

When Hannah completes a support conversation, she calls **`create_support_ticket`** with all required contact fields. The server:

1. Creates or updates a HubSpot **contact**
2. Creates a HubSpot **ticket** (pipeline/stage from env)
3. Posts a **Slack** message to the support channel
4. Stores an audit row in the Support Control SQLite DB

## Required env (`server/.env`)

| Variable | Purpose |
|----------|---------|
| `HUBSPOT_PRIVATE_APP_TOKEN` | Private app token (needs `crm.objects.contacts.write`, `crm.objects.tickets.write`) |
| `HUBSPOT_NEW_TICKET_PIPELINE_ID` | Ticket pipeline ID for new AI tickets |
| `HUBSPOT_NEW_TICKET_STAGE_ID` | Initial stage ID (e.g. New) |
| `HUBSPOT_PORTAL_ID` | For ticket URLs in Slack (default `3355079`) |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write` |
| `SLACK_SUPPORT_CHANNEL_ID` | Channel for alerts (or `SUPPORT_TICKET_SLACK_CHANNEL_ID`) |

Optional: `HUBSPOT_TICKET_SOURCE_PROPERTY` — custom ticket property for `ai_voice` / `ai_chat`.

## ElevenLabs post-call webhook (incomplete sessions)

If a voice session ends **without** `create_support_ticket`, the server can alert Slack.

1. **ElevenLabs → Agents → your support agent → Post-call webhook**
2. **URL:** `{SUPPORT_PUBLIC_BASE_URL}/api/elevenlabs/call-end`
3. Copy **Signing secret** → `ELEVENLABS_WEBHOOK_SECRET` (optional; set `ELEVENLABS_WEBHOOK_STRICT=1` to reject bad signatures)

## Health check

`GET /api/health` includes:

- `hubspot_ticket_create_configured`
- `slack_ticket_notify_configured`

## Admin API

- `GET /api/admin/support/tickets` — recent AI-created tickets (auth required)
