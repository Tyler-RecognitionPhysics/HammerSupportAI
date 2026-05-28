# Local testing with ngrok (Zap 2 → your PC)

## Running right now

1. **API:** `.\1-START-LOCAL-API.ps1` (port **8780**)
2. **Web:** `cd web; npm run dev` (port **5173**)
3. **ngrok:** `.\start-ngrok.ps1` or `ngrok http 8780`

Open the ngrok dashboard for the current public URL: http://127.0.0.1:4040

Your tunnel URL **changes** when you restart ngrok — update Zap 2 each time.

## Zap 2 — Webhooks POST

**URL:** `https://<your-subdomain>.ngrok-free.dev/api/zapier/approval`

**Headers (3 required on free ngrok):**

| Header | Value |
|--------|--------|
| `Content-Type` | `application/json` |
| `X-Zapier-Secret` | `ZAPIER_APPROVAL_CALLBACK_SECRET` from `server/.env` |
| `ngrok-skip-browser-warning` | `true` |

**Data:**

| Key | Gmail field |
|-----|-------------|
| `email` | **From Email** |
| `approved` | `true` |
| `reply_text` | **Snippet** or **Body Plain** |

## Verify

```powershell
$base = "https://YOUR-SUBDOMAIN.ngrok-free.dev"
$h = @{ "ngrok-skip-browser-warning" = "true" }
Invoke-RestMethod "$base/api/health" -Headers $h
Invoke-RestMethod "$base/api/zapier/approval-status?email=YOUR@EMAIL.com" -Headers $h
```

## Voice demo

- Browser: http://localhost:5173 (proxies `/api` → 8780)
- `capture_lead` email must match Gmail **From Email** on the I approve reply
- Agent uses `check_agreement_approval` before account setup

## ngrok setup (one time)

```powershell
ngrok config add-authtoken YOUR_TOKEN
ngrok update   # need 3.20+ ; winget may install older — run update once
```
