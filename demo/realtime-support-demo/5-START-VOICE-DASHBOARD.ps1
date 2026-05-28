# Voice AI dashboard — starts local API (if needed) and opens the control panel.
# Requires: server\.env with OPENAI_API_KEY (+ ELEVENLABS_* for voice settings tab)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$apiUrl = "http://127.0.0.1:8780"
$dashboardUrl = "$apiUrl/debug/voice-dashboard"

function Test-ApiUp {
  try {
    $r = Invoke-WebRequest -Uri "$apiUrl/api/health" -UseBasicParsing -TimeoutSec 2
    return $r.StatusCode -eq 200
  } catch {
    return $false
  }
}

Write-Host "=== Voice AI Dashboard ===" -ForegroundColor Cyan

if (Test-ApiUp) {
  Write-Host "API already running on $apiUrl" -ForegroundColor Green
} else {
  Write-Host "Starting API in a new window..." -ForegroundColor Yellow
  Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $root "1-START-LOCAL-API.ps1")
  )
  $deadline = (Get-Date).AddSeconds(25)
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-ApiUp) { break }
  }
  if (-not (Test-ApiUp)) {
    Write-Host "API did not start in time. Run .\1-START-LOCAL-API.ps1 manually and retry." -ForegroundColor Red
    exit 1
  }
  Write-Host "API is up." -ForegroundColor Green
}

Write-Host "Opening dashboard: $dashboardUrl" -ForegroundColor Cyan
Write-Host "Production (after deploy + REALTIME_SALES_ADMIN_SECRET): https://YOUR-DOMAIN/admin/voice" -ForegroundColor DarkGray
Write-Host "(Local admin with password: http://127.0.0.1:8780/admin/voice — set REALTIME_SALES_ADMIN_SECRET in server/.env)" -ForegroundColor DarkGray
Start-Process $dashboardUrl
