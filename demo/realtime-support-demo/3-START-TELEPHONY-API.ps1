# Start API for phone/SIP testing (port 8780). Use with ngrok for OpenAI webhooks.
# Preflight: .\scripts\telephony-preflight.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location (Join-Path $root "server")

$hammerRepo = (Resolve-Path (Join-Path $root "..\..")).Path
$env:REALTIME_SALES_REPO_ROOT = $hammerRepo
Remove-Item Env:REALTIME_SALES_WIKI_DIR -ErrorAction SilentlyContinue

Write-Host "=== Telephony API (SIP webhook) ===" -ForegroundColor Cyan
Write-Host "Wiki: $hammerRepo\wiki" -ForegroundColor DarkGray

py -3 -m pip install -r requirements.txt -q

$envPath = Join-Path (Get-Location) ".env"
$vars = @{}
if (Test-Path $envPath) {
  foreach ($line in Get-Content $envPath) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$' -and $line -notmatch '^\s*#') {
      $vars[$Matches[1]] = $Matches[2].Trim()
    }
  }
}

if (-not $vars["OPENAI_API_KEY"]) {
  Write-Host "Missing OPENAI_API_KEY in server\.env" -ForegroundColor Red
  exit 1
}
if (-not $vars["OPENAI_WEBHOOK_SECRET"]) {
  Write-Host ""
  Write-Host "OPENAI_WEBHOOK_SECRET is not set — SIP webhook will return 503 until you add it." -ForegroundColor Yellow
  Write-Host "Create webhook at https://platform.openai.com/settings (event: realtime.call.incoming)" -ForegroundColor Yellow
  Write-Host ""
}

if ($vars["OPENAI_PROJECT_ID"]) {
  Write-Host "Twilio SIP URI: sip:$($vars['OPENAI_PROJECT_ID'])@sip.api.openai.com;transport=tls" -ForegroundColor Green
}

Write-Host ""
Write-Host "Next terminal:  ngrok http 8780" -ForegroundColor Yellow
Write-Host "OpenAI webhook URL: https://YOUR-NGROK-HOST/api/realtime/sip-webhook" -ForegroundColor Yellow
Write-Host ""
Write-Host "API: http://127.0.0.1:8780/api/health" -ForegroundColor Green
Write-Host ""

py -3 -m uvicorn app:app --host 127.0.0.1 --port 8780 --reload --reload-include .env
