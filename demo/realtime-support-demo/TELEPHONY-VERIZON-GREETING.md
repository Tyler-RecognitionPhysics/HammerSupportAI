# “Welcome to Verizon Wireless” when calling Tyler

That recording is **Verizon’s mobile network**, not Twilio, OpenAI, or your app. The dialed number is being handled as a **normal cell/disconnected line**, not your Elastic SIP trunk.

## Fix (5 minutes in Twilio)

1. Open **[Twilio → Phone Numbers → Manage → Active numbers](https://console.twilio.com/us1/develop/phone-numbers/manage/incoming)**.
2. Find the number with **Voice** capability that is attached to your **OpenAI SIP trunk** (`TKb3acdd853c8caeb2f8e4e6c5502925dc` or your trunk name).
3. **Call that exact number** from your cell (type it manually — do not use an old contact).
4. Put **that** E.164 value in `server/.env`:
   ```env
   DEMO_PHONE_NUMBER=+1XXXXXXXXXX
   DEMO_PHONE_DISPLAY=(XXX) XXX-XXXX
   ```
5. Run:
   ```powershell
   .\scripts\push-all-env-to-vercel.ps1 -ProjectName hammer-finalsite
   cd demo\realtime-sales-demo
   .\scripts\fly-secrets-from-env.ps1
   ```
   Redeploy Vercel + Fly (see `scripts\deploy-fly-telephony.ps1`).

## Common mistakes

| Symptom | Cause |
|--------|--------|
| Verizon greeting | Number is **not** an active Twilio voice DID on your trunk |
| Twilio log **Failed / 0 sec** to `sip:proj_…@sip.api.openai.com` | You **did** reach Twilio — fix OpenAI SIP allowlist + webhook (see `TELEPHONY_SETUP.md`) |
| Works on `hammer-finalsite.vercel.app` but not `www.hammertime.com` | Custom domain still on **Wix** — use Vercel URL or move DNS |

## How to tell the difference

- **Verizon message** → PSTN never hits Twilio. No Tyler, no Twilio SIP log for that test call.
- **Ring then silence / fast hangup** → Twilio → OpenAI issue (check Twilio call log + OpenAI webhook deliveries).

## After Twilio number is correct

1. Trunk → **Origination**: `sip:proj_YRg5STB5poYBshOuqACO4vzP@sip.api.openai.com;transport=tls`
2. Trunk → **Phone Numbers** → your active Twilio number attached
3. OpenAI → project `proj_YRg5STB5poYBshOuqACO4vzP` → webhook → `https://hammer-voice-telephony.fly.dev/api/realtime/sip-webhook`

Verify: `GET https://hammer-finalsite.vercel.app/api/health` → `demo_phone_number` must match the Twilio active number.
