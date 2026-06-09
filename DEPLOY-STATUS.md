# HAMMERFINALSITE — deploy status

Last updated: 2026-05-19

## GitHub

- **Repo:** https://github.com/Tyler-RecognitionPhysics/HAMMERFINALSITE
- **Branch:** `main`

## Vercel (website + APIs)

- **Project:** `hammer-finalsite`
- **Production URL:** https://hammer-finalsite.vercel.app
- **Phone diagnostics:** https://hammer-finalsite.vercel.app/api/telephony/status
- **Preview URLs** (`hammer-finalsite-*-tyler-s-projects9.vercel.app`) may return **401** if Vercel Deployment Protection is on — use production for testing, or disable protection for previews.
- **Dashboard:** https://vercel.com/tyler-s-projects9/hammer-finalsite
- **Git:** Connected to `Tyler-RecognitionPhysics/HAMMERFINALSITE` (push to `main` auto-deploys)

### Health check

```text
GET https://hammer-finalsite.vercel.app/api/health
```

Expected for full marketing site:

| Field | Status |
|-------|--------|
| `ok` | `true` |
| `openai_configured` | `true` |
| `zapier_lead_webhook_configured` | `true` (voice signup) |
| `zapier_website_lead_webhook_configured` | set `ZAPIER_WEBSITE_LEAD_WEBHOOK_URL` if website form should fire Zapier |
| `demo_phone_configured` | `true` |
| `telephony_enabled` | `false` on Vercel (phone uses Fly) |

### Sync env from local `server/.env`

```powershell
$authPath = "$env:APPDATA\xdg.data\com.vercel.cli\auth.json"
$env:VERCEL_TOKEN = (Get-Content $authPath -Raw | ConvertFrom-Json).token
.\scripts\push-all-env-to-vercel.ps1 -ProjectName hammer-finalsite
vercel deploy --prod --yes
```

## Phone voice (Fly.io — not Vercel)

Inbound calls: `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook`

See [GO-LIVE-TODAY.md](./GO-LIVE-TODAY.md).

## Custom domain (hammertime.com)

`www.hammertime.com` may still point at the older Vercel project (`hammer-sell-me-a-pen-challenge`). To cut over:

1. Vercel → **hammer-finalsite** → **Domains** → add `www.hammertime.com` + `hammertime.com`
2. Update DNS per Vercel instructions (domain admin)
3. Remove domain from the old project to avoid conflicts
4. Redeploy after setting `REALTIME_SALES_PUBLIC_BASE_URL=https://www.hammertime.com`
