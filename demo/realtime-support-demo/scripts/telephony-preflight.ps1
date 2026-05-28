# Checks phone/SIP readiness and prints your Twilio SIP URI + next steps.
# Run from repo: .\scripts\telephony-preflight.ps1

$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$serverDir = Join-Path $demoRoot "server"
$envPath = Join-Path $serverDir ".env"
$hammerRepo = (Resolve-Path (Join-Path $demoRoot "..\..")).Path

function Read-DotEnv([string]$path) {
  $map = @{}
  if (-not (Test-Path $path)) { return $map }
  foreach ($line in Get-Content $path) {
    if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
    $map[$Matches[1]] = $Matches[2].Trim()
  }
  return $map
}

$envVars = Read-DotEnv $envPath
$checks = [ordered]@{
  "OPENAI_API_KEY" = [bool]($envVars["OPENAI_API_KEY"] -and $envVars["OPENAI_API_KEY"].Length -gt 20)
  "ZAPIER_LEAD_WEBHOOK_URL" = [bool]$envVars["ZAPIER_LEAD_WEBHOOK_URL"]
  "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL" = [bool]$envVars["ZAPIER_WEBSITE_LEAD_WEBHOOK_URL"]
  "ZAPIER_APPROVAL_CALLBACK_SECRET" = [bool]$envVars["ZAPIER_APPROVAL_CALLBACK_SECRET"]
  "OPENAI_WEBHOOK_SECRET" = [bool]$envVars["OPENAI_WEBHOOK_SECRET"]
  "OPENAI_PROJECT_ID" = [bool]$envVars["OPENAI_PROJECT_ID"]
  "DEMO_PHONE_NUMBER" = [bool]$envVars["DEMO_PHONE_NUMBER"]
}

Write-Host ""
Write-Host "=== Hammer phone voice - preflight ===" -ForegroundColor Cyan
Write-Host "Server .env: $envPath" -ForegroundColor DarkGray
Write-Host ""

foreach ($key in $checks.Keys) {
  $ok = $checks[$key]
  $mark = if ($ok) { "[OK]" } else { "[  ]" }
  $color = if ($ok) { "Green" } else { "Yellow" }
  Write-Host "$mark $key" -ForegroundColor $color
}

$proj = $envVars["OPENAI_PROJECT_ID"]
if ($proj) {
  Write-Host ""
  Write-Host "Twilio Origination URI (paste in SIP trunk):" -ForegroundColor Cyan
  Write-Host "  sip:${proj}@sip.api.openai.com;transport=tls" -ForegroundColor White
}

Write-Host ""
Write-Host "--- You still need (if marked [  ]) ---" -ForegroundColor Yellow
if (-not $checks["OPENAI_WEBHOOK_SECRET"]) {
  Write-Host "  1. OpenAI: Settings - Webhooks - Create endpoint"
  Write-Host "     Event: realtime.call.incoming"
  Write-Host "     URL: https://YOUR-PUBLIC-HOST/api/realtime/sip-webhook"
  Write-Host "     Then add OPENAI_WEBHOOK_SECRET=whsec_... to server\.env"
}
if (-not $checks["OPENAI_PROJECT_ID"]) {
  Write-Host "  2. OpenAI: Settings - General - copy Project ID into OPENAI_PROJECT_ID in server\.env"
}
if (-not $checks["DEMO_PHONE_NUMBER"]) {
  Write-Host "  3. Twilio: buy number, SIP trunk, origination URI above, attach number"
  Write-Host "     Then DEMO_PHONE_NUMBER=+1... and DEMO_PHONE_DISPLAY=... in server\.env + wiki"
}

Write-Host ""
Write-Host "Local test: .\3-START-TELEPHONY-API.ps1  then ngrok http 8780" -ForegroundColor DarkGray
Write-Host "Docs: demo\realtime-sales-demo\TELEPHONY-YOUR-CHECKLIST.md" -ForegroundColor DarkGray
Write-Host ""

# Optional live API check if server is up
try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8780/api/health" -TimeoutSec 2
  Write-Host "Local API health:" -ForegroundColor Green
  $health | ConvertTo-Json -Compress | Write-Host
} catch {
  Write-Host 'Local API not running on :8780 - run .\3-START-TELEPHONY-API.ps1' -ForegroundColor DarkGray
}
