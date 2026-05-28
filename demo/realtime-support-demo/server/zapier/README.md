# Zapier setup — agreement email + “I approve”

Required for the voice signup flow to work end-to-end.

## Order of setup

| Step | Zap | Guide |
|------|-----|--------|
| 1 | **Send agreement email** when customer signs up on a call | Below — pick product |
| 2 | **“I approve”** from your inbox → unlocks account creation | **`ZAPIER_I_APPROVE_SETUP.md`** |
| 3 | Hammer Office account (automatic on server) | **`HAMMER_OFFICE_ACCOUNT.md`** |
| 4 | **End-of-call Slack summary** (phone required) | **`VOICE_CALL_SUMMARY_SLACK.md`** |

## Zap 1 — product-specific email templates

| Product | File |
|---------|------|
| Hammer Drive | **`HAMMER_DRIVE_EMAIL.md`** |
| Facebook AIA | **`FACEBOOK_AIA_EMAIL.md`** |
| Hammer Connect | **`HAMMER_CONNECT_EMAIL.md`** |
| MarketPoster | **`MARKETPOSTER_EMAIL.md`** |

Shared Gmail HTML snippet: **`GMAIL_BODY_TEMPLATE.txt`**

## Env vars (in `server/.env` or Vercel)

- **`ZAPIER_LEAD_WEBHOOK_URL`** — Zap 1 Catch Hook (voice AI / agreement email only), e.g. hook `4od2z1k`  
- **`ZAPIER_WEBSITE_LEAD_WEBHOOK_URL`** — separate Catch Hook for the website “Get started” form (`event: website_lead`), e.g. `https://hooks.zapier.com/hooks/catch/27649081/4o1aob8/`  
- **`ZAPIER_APPROVAL_CALLBACK_SECRET`** — must match Zap 2 header `X-Zapier-Secret`
- **`ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL`** — separate Catch Hook for end-of-call summaries (`event: voice_call_summary`)

## Local testing with Gmail → your laptop

If Zapier must reach your machine: **`../../LOCAL_NGROK.md`** and **`../../start-ngrok.ps1`**
