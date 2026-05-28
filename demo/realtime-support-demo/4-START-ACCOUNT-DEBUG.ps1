# Local Hammer account creation debug - visible Chromium on your desktop.
# Opens http://127.0.0.1:8780/debug/hammer-account when the API is ready.
#
# Prereqs: server\.env with OPENAI_API_KEY, HAMMER_OFFICE_EMAIL, HAMMER_OFFICE_PASSWORD
# Optional voice test: second terminal -> .\2-START-LOCAL-WEB.ps1 -> http://127.0.0.1:5173

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location (Join-Path $root "server")

$hammerRepo = (Resolve-Path (Join-Path $root "..\..")).Path
$env:REALTIME_SALES_REPO_ROOT = $hammerRepo
Remove-Item Env:REALTIME_SALES_WIKI_DIR -ErrorAction SilentlyContinue
Remove-Item Env:REALTIME_SALES_PRODUCTION -ErrorAction SilentlyContinue
Remove-Item Env:FLY_APP_NAME -ErrorAction SilentlyContinue
Remove-Item Env:VERCEL -ErrorAction SilentlyContinue
Remove-Item Env:VERCEL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:REALTIME_SALES_SERVERLESS -ErrorAction SilentlyContinue
$env:REALTIME_SALES_FORCE_LOCAL = "1"

$env:HAMMER_OFFICE_DEBUG = "1"
$env:HAMMER_OFFICE_USE_PLAYWRIGHT = "1"
$env:HAMMER_OFFICE_HEADLESS = "0"
# 0 = wait for each Playwright fill so you can watch the form (voice debug)
$env:HAMMER_OFFICE_INSTANT = "0"
$env:HAMMER_OFFICE_SLOW_MO = "400"
$env:HAMMER_OFFICE_KEEP_OPEN = "120"
$env:HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT = "1"

Write-Host "=== Hammer account DEBUG (visible Chromium) ===" -ForegroundColor Cyan
Write-Host "Wiki: $hammerRepo\wiki" -ForegroundColor DarkGray
Write-Host "HAMMER_OFFICE_DEBUG=1  HEADLESS=0  INSTANT=0  SLOW_MO=400" -ForegroundColor Green
Write-Host ""

py -3 -m pip install -r requirements.txt -q
Write-Host "Installing Playwright Chromium (first run may take a minute)..." -ForegroundColor DarkGray
py -3 -m playwright install chromium 2>&1 | Out-Host

$envPath = Join-Path (Get-Location) ".env"
if (-not (Test-Path $envPath)) {
  Write-Host "Missing server\.env - copy from server\.env.example" -ForegroundColor Red
  exit 1
}

$debugUrl = "http://127.0.0.1:8780/debug/hammer-account"
Write-Host "Debug panel: $debugUrl" -ForegroundColor Yellow
Write-Host "API health:  http://127.0.0.1:8780/api/debug/hammer/config" -ForegroundColor DarkGray
Write-Host "Full guide: LOCAL_ACCOUNT_DEBUG.md" -ForegroundColor DarkGray
Write-Host ""

Start-Job -ScriptBlock {
  param($url)
  Start-Sleep -Seconds 4
  Start-Process $url
} -ArgumentList $debugUrl | Out-Null

Write-Host "API: http://127.0.0.1:8780" -ForegroundColor Green
# No --reload: file watching restarts the API mid-Playwright and causes 500/freeze.
py -3 -m uvicorn app:app --host 127.0.0.1 --port 8780
