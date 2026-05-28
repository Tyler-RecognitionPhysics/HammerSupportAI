# Phone call path diagnostics (Twilio -> OpenAI SIP -> Fly webhook).
# Run from repo: .\demo\realtime-sales-demo\scripts\telephony-diagnose.ps1

$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $demoRoot "server\.env"
$flyUrl = "https://hammer-voice-telephony.fly.dev"

function Read-DotEnv([string]$path) {
  $map = @{}
  if (-not (Test-Path $path)) { return $map }
  foreach ($line in Get-Content $path) {
    if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
    $map[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
  }
  return $map
}

$local = Read-DotEnv $envPath

Write-Host ""
Write-Host "=== Phone (Twilio) diagnostics ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "--- Live Fly host ($flyUrl) ---" -ForegroundColor White
try {
  $h = Invoke-RestMethod -Uri "$flyUrl/api/health" -TimeoutSec 45
  $h | ConvertTo-Json -Depth 4 | Write-Host
  if (-not $h.telephony_enabled) { Write-Host "FAIL: telephony_enabled is false on Fly" -ForegroundColor Red }
  if (-not $h.telephony_webhook_secret_configured) { Write-Host "FAIL: OPENAI_WEBHOOK_SECRET missing on Fly" -ForegroundColor Red }
  if (-not $h.openai_configured) { Write-Host "FAIL: OPENAI_API_KEY missing on Fly (update fly secrets)" -ForegroundColor Red }
  if (-not $h.openai_project_id_configured) {
    Write-Host "WARN: OPENAI_PROJECT_ID not set on Fly - Twilio SIP trunk URI may be wrong" -ForegroundColor Yellow
  } elseif ($h.twilio_sip_origination_uri) {
    Write-Host "Twilio origination URI (must match trunk):" -ForegroundColor Green
    Write-Host "  $($h.twilio_sip_origination_uri)" -ForegroundColor White
  }
} catch {
  Write-Host "FAIL: Could not reach Fly ($flyUrl). Deploy: fly deploy --config fly.toml" -ForegroundColor Red
  Write-Host $_.Exception.Message -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "--- Local server/.env (for Fly secrets sync) ---" -ForegroundColor White
$phone = $local["DEMO_PHONE_NUMBER"]
Write-Host "DEMO_PHONE_NUMBER: $(if ($phone) { $phone } else { '(not set)' })"
Write-Host "OPENAI_PROJECT_ID: $(if ($local['OPENAI_PROJECT_ID']) { $local['OPENAI_PROJECT_ID'] } else { '(commented out - set for Twilio SIP URI)' })"
Write-Host "OPENAI_WEBHOOK_SECRET: $(if ($local['OPENAI_WEBHOOK_SECRET']) { 'set' } else { 'MISSING' })"
Write-Host "OPENAI_API_KEY: $(if ($local['OPENAI_API_KEY']) { 'set' } else { 'MISSING' })"

Write-Host ""
Write-Host "--- 'Welcome to Verizon Wireless' (NOT Twilio / NOT Tyler) ---" -ForegroundColor Red
Write-Host "  That means the number you DIALED is not an active Twilio voice line on your trunk."
Write-Host "  Open: https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"
Write-Host "  Copy the Voice number attached to your SIP trunk -> set DEMO_PHONE_NUMBER in server\.env"
Write-Host "  (737) 205-6753 only works if THAT is the number listed in Twilio Active Numbers."
Write-Host "  See demo\realtime-sales-demo\TELEPHONY-VERIZON-GREETING.md"
Write-Host ""
Write-Host "--- Twilio log shows Failed / 0 sec to sip.api.openai.com ---" -ForegroundColor Yellow
Write-Host "  You DID reach Twilio. Fix OpenAI IP allowlist + webhook on the same proj_ as the trunk URI."
Write-Host ""
Write-Host "--- After API key change, sync Fly (not just Vercel) ---" -ForegroundColor Cyan
Write-Host "  cd demo\realtime-sales-demo"
Write-Host "  .\scripts\fly-secrets-from-env.ps1"
Write-Host "  fly deploy --config fly.toml"
Write-Host ""
