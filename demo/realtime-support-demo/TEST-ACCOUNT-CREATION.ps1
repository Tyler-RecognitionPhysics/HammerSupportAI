# Visible Chromium test — Hammer Office account creation (legal name: Tyler's Auto)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location (Join-Path $root "server")

$env:HAMMER_TEST_DEALERSHIP = "Tyler's Auto"
# Optional: $env:HAMMER_TEST_EMAIL = "your-test@email.com"

Write-Host "=== Hammer Office account creation test (visible browser) ===" -ForegroundColor Cyan
Write-Host "Legal/display name: $($env:HAMMER_TEST_DEALERSHIP)" -ForegroundColor Yellow
Write-Host ""

py -3 scripts/test_create_account_tylers.py
exit $LASTEXITCODE
