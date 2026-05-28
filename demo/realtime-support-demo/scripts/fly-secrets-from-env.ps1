# After installing Fly CLI (https://fly.io/docs/hands-on/install-flyctl/):
#   fly auth login
#   fly launch --config demo/realtime-sales-demo/fly.toml --no-deploy
# Then from demo/realtime-sales-demo:
#   .\scripts\fly-secrets-from-env.ps1

$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $demoRoot "server\.env"

$fly = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" }
       elseif (Get-Command flyctl -ErrorAction SilentlyContinue) { "flyctl" }
       else { $null }
if (-not $fly) {
  Write-Host "Install Fly CLI first: https://fly.io/docs/hands-on/install-flyctl/" -ForegroundColor Red
  exit 1
}
$env:Path = "$env:USERPROFILE\.fly\bin;$env:Path"
if (-not (Test-Path $envPath)) {
  Write-Host "Missing $envPath" -ForegroundColor Red
  exit 1
}

$keys = @(
  "OPENAI_API_KEY",
  "ELEVENLABS_API_KEY",
  "ELEVENLABS_AGENT_ID",
  "ELEVENLABS_CHAT_MODEL",
  "ELEVENLABS_SIP_URI",
  "ELEVENLABS_WEBHOOK_SECRET",
  "OPENAI_WEBHOOK_SECRET",
  "OPENAI_PROJECT_ID",
  "ZAPIER_LEAD_WEBHOOK_URL",
  "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL",
  "ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL",
  "ZAPIER_APPROVAL_CALLBACK_SECRET",
  "HAMMER_OFFICE_EMAIL",
  "HAMMER_OFFICE_PASSWORD",
  "HAMMER_OFFICE_USE_PLAYWRIGHT",
  "HAMMER_OFFICE_HEADLESS",
  "HAMMER_OFFICE_INSTANT",
  "DEMO_PHONE_NUMBER",
  "DEMO_PHONE_DISPLAY",
  "TWILIO_ACCOUNT_SID",
  "TWILIO_AUTH_TOKEN",
  "TWILIO_OUTBOUND_ENABLED",
  "TELEPHONY_PUBLIC_BASE_URL",
  "VOICE_PHONE_DISCLOSURE_ENABLED",
  "VOICE_PHONE_DISCLOSURE",
  "VOICE_PHONE_DISCLOSURE_AUDIO_URL",
  "REALTIME_SALES_MODEL",
  "REALTIME_SALES_SIP_REASONING_MINIMAL",
  "REALTIME_SALES_SIP_VAD_EAGERNESS",
  "REALTIME_SALES_SIP_FIRST_TTS_SETTLE_S"
)

# Last assignment per key wins (matches server app .env parsing).
$fromFile = @{}
foreach ($line in Get-Content $envPath -Encoding UTF8) {
  if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
  $name = $Matches[1]
  $val = $Matches[2].Trim().Trim('"').Trim("'")
  if ($name -in $keys -and $val) {
    $fromFile[$name] = $val
  }
}

$secretArgs = @()
foreach ($name in $keys) {
  if ($fromFile.ContainsKey($name)) {
    $secretArgs += "${name}=$($fromFile[$name])"
  }
}

$secretArgs += "REALTIME_SALES_PUBLIC_BASE_URL=https://www.hammertime.com"
$secretArgs += "REALTIME_SALES_TELEPHONY=1"
$secretArgs += "REALTIME_SALES_CORS_ORIGINS=https://www.hammertime.com,https://hammertime.com,https://hammer-finalsite.vercel.app,https://sellmeapen.vercel.app,http://127.0.0.1:5173,http://localhost:5173"
$secretArgs += "HAMMER_OFFICE_USE_PLAYWRIGHT=1"
$secretArgs += "HAMMER_OFFICE_HEADLESS=1"
$secretArgs += "HAMMER_OFFICE_INSTANT=1"

if ($secretArgs.Count -lt 6) {
  Write-Host "Few secrets from .env - ensure OPENAI_API_KEY and ZAPIER_* are set." -ForegroundColor Yellow
}

Write-Host "Setting $($secretArgs.Count) Fly secrets (values hidden)..." -ForegroundColor Cyan
& $fly secrets set @secretArgs -a hammer-voice-telephony
if ($LASTEXITCODE -ne 0) {
  Write-Host ""
  Write-Host "Fly secrets update failed (exit $LASTEXITCODE). Common fixes:" -ForegroundColor Red
  Write-Host "  1. fly auth login   (re-auth if you see invalid token)" -ForegroundColor Yellow
  Write-Host "  2. Retry in a few minutes if Fly API returns 503" -ForegroundColor Yellow
  Write-Host "  3. Dashboard: https://fly.io/apps/hammer-voice-telephony/secrets" -ForegroundColor Yellow
  exit $LASTEXITCODE
}
Write-Host "Fly secrets updated. Machines restart automatically." -ForegroundColor Green
Write-Host ("Optional code deploy from repo root: {0} deploy --config demo/realtime-sales-demo/fly.toml" -f $fly) -ForegroundColor DarkGray
