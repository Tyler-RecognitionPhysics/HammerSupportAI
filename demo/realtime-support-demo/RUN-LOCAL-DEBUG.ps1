# Launch local stack for account-creation debugging (3 windows).
# 1) API + visible Chromium debug panel
# 2) Vite site (browser voice → local API)
# 3) Instructions in this window

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "=== Starting local Hammer debug stack ===" -ForegroundColor Cyan
Write-Host ""

$apiScript = Join-Path $root "4-START-ACCOUNT-DEBUG.ps1"
$webScript = Join-Path $root "2-START-LOCAL-WEB.ps1"

Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$apiScript`""
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$webScript`""

Write-Host "Opened two terminals:" -ForegroundColor Green
Write-Host "  1) API + debug panel  → http://127.0.0.1:8780/debug/hammer-account" -ForegroundColor White
Write-Host "  2) Website            → http://127.0.0.1:5173 (voice uses local API / Playwright)" -ForegroundColor White
Write-Host ""
Write-Host "Quick test without voice: use the debug panel → Run full sample flow" -ForegroundColor Yellow
Write-Host "Voice test: open http://127.0.0.1:5173, run through sale until account creation" -ForegroundColor Yellow
Write-Host "Phone test: also run .\start-ngrok.ps1 and point OpenAI webhook at ngrok (see LOCAL_ACCOUNT_DEBUG.md)" -ForegroundColor Yellow
Write-Host ""
Write-Host "Guide: $(Join-Path $root 'LOCAL_ACCOUNT_DEBUG.md')" -ForegroundColor DarkGray
