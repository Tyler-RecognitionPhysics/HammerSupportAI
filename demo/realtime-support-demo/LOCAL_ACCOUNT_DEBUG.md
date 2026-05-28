# Local account creation debug (visible Chromium)

Use this on your laptop **before** pushing to Fly/Vercel. You will see a real Chromium window fill Hammer Office at `https://office.hammer-corp.com/accounts/new`.

## One-time setup

1. Copy `server/.env.example` → `server/.env` if you have not already.
2. Set in `server/.env`:
   - `OPENAI_API_KEY`
   - `HAMMER_OFFICE_EMAIL` / `HAMMER_OFFICE_PASSWORD`
   - `HAMMER_OFFICE_USE_PLAYWRIGHT=1`
3. Install [ngrok](https://ngrok.com/download) only if you want **phone** testing locally.

## Fastest path — debug panel only (no voice)

```powershell
cd demo\realtime-sales-demo
.\4-START-ACCOUNT-DEBUG.ps1
```

Your browser opens **http://127.0.0.1:8780/debug/hammer-account**.

1. Click **Refresh status** — `visible_chromium=true`
2. Click **Run full sample flow** — Chromium opens, logs in, fills fields step by step
3. Watch the form; fix issues locally, then redeploy Fly when ready

## Full stack — browser voice + visible Chromium

```powershell
cd demo\realtime-sales-demo
.\RUN-LOCAL-DEBUG.ps1
```

This opens two terminals:

| Terminal | URL | Purpose |
|----------|-----|---------|
| API | http://127.0.0.1:8780 | Playwright + debug routes |
| Web | http://127.0.0.1:5173 | Same UI as production; `/api` proxies to local API |

Run a voice session on **http://127.0.0.1:5173** (not the Vercel URL — that uses Fly headless and you cannot see Chromium).

When the agent reaches account creation (`fill_hammer_account_field`), Chromium opens on your PC.

**I approve without Zapier:** On the debug panel, click **Approve email** using the **exact same email** you gave the agent in PHASE A. Use **Check I approve** to confirm `approved: true` before account fields.

**Real email reply on local 5173:** Gmail → Zap 2 must POST to your PC, not Fly/Vercel. Run `.\start-ngrok.ps1`, copy the https URL from http://127.0.0.1:4040, set Zap 2 to `https://<ngrok-host>/api/zapier/approval` with headers from `LOCAL_NGROK.md`. The `email` field must be the buyer's **From Email** on the reply (same as capture_lead). Restart ngrok = update Zap URL again.

Then click **Voice session** during the call to see `agreement_approved`, `missing_for_submit`, and `submit_error`.

**Debug panel works but voice does not?** Common causes:

| Symptom | Fix |
|---------|-----|
| `agreement_approved: false` | Approve email on the panel (or real Zapier I approve) **before** the last field triggers submit |
| `session_open: false` | Agent never ran `capture_lead` or used a different email than the panel |
| `submit_error` in Voice session | Read the message; watch Chromium and the API terminal |
| Testing on hammertime.com / Vercel | Use **127.0.0.1:5173** + local API, or phone via ngrok — production Fly is headless only |

With a visible browser, the server waits up to ~25s on the final field for Create account to finish, then tells the agent whether it succeeded (instead of only “submitting in background”).

## Phone call + visible Chromium (optional)

1. `.\4-START-ACCOUNT-DEBUG.ps1`
2. Second terminal: `.\start-ngrok.ps1`
3. Open http://127.0.0.1:4040 — copy the `https://….ngrok-free.app` URL
4. OpenAI → Project → Webhooks → set endpoint to  
   `https://YOUR-NGROK-HOST/api/realtime/sip-webhook`
5. Call your Twilio number — tools run on your laptop; Chromium is visible

Restore the webhook to `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook` when done.

## What `HAMMER_OFFICE_DEBUG=1` changes

| Setting | Debug value | Effect |
|---------|-------------|--------|
| `HAMMER_OFFICE_HEADLESS` | `0` | Chromium window on desktop |
| `HAMMER_OFFICE_INSTANT` | `0` | Waits for Playwright per field (not fire-and-forget) |
| `HAMMER_OFFICE_SLOW_MO` | `400` | 400 ms between actions |
| `HAMMER_OFFICE_KEEP_OPEN` | `300` | Keep browser open 5 minutes after fills |

`4-START-ACCOUNT-DEBUG.ps1` sets these automatically. They are **ignored on Fly** (no display server).

## Debug API (local only)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/debug/hammer-account` | HTML control panel |
| GET | `/api/debug/hammer/config` | headless / credentials / debug flag |
| POST | `/api/debug/hammer/approve` | Skip Zapier — mark email approved |
| POST | `/api/debug/hammer/open-form` | Open Hammer form (visible browser) |
| POST | `/api/debug/hammer/fill-field` | One field (same as voice tool) |
| POST | `/api/debug/hammer/run-sample` | Full PHASE B sample flow |
| POST | `/api/debug/hammer/close?email=` | Close Chromium session |

## Production site

https://hammer-finalsite.vercel.app/ uses **Fly.io** for voice and **headless** Playwright. Local debug does not change production until you deploy.

```powershell
cd <repo-root>
fly deploy --config demo/realtime-sales-demo/fly.toml
```

## Troubleshooting

- **503 — Set HAMMER_OFFICE_DEBUG=1** — Start API with `4-START-ACCOUNT-DEBUG.ps1`, not plain `1-START-LOCAL-API.ps1`.
- **No Chromium window** — Run `py -3 -m playwright install chromium` from `server/`.
- **Agreement not approved** — Use debug panel **Approve email** or reply I approve via Zapier → ngrok.
- **Role / address errors** — Use **Run full sample flow** and read the log panel + terminal stack traces.
