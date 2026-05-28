# Single source of truth: demo/realtime-sales-demo/server/.env
# Pushes the same OPENAI_API_KEY (and telephony secrets) to Fly + optional Vercel.
#
# Usage (from demo/realtime-sales-demo):
#   .\scripts\sync-secrets-from-server-env.ps1
#   .\scripts\sync-secrets-from-server-env.ps1 -SkipVercel
#   .\scripts\sync-secrets-from-server-env.ps1 -SkipFly

param(
  [switch]$SkipFly,
  [switch]$SkipVercel,
  [string]$VercelProject = "hammer-finalsite"
)

$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$repoRoot = (Resolve-Path (Join-Path $demoRoot "..\..")).Path
$envPath = Join-Path $demoRoot "server\.env"

if (-not (Test-Path $envPath)) {
  Write-Host "Missing $envPath - copy server/.env.example to server/.env and set OPENAI_API_KEY." -ForegroundColor Red
  exit 1
}

$fromFile = @{}
foreach ($line in Get-Content $envPath -Encoding UTF8) {
  if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
  $fromFile[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
}

if (-not $fromFile["OPENAI_API_KEY"]) {
  Write-Host "OPENAI_API_KEY missing in server/.env" -ForegroundColor Red
  exit 1
}

$key = $fromFile["OPENAI_API_KEY"]
$last4 = if ($key.Length -ge 4) { $key.Substring($key.Length - 4) } else { $key }
Write-Host "Canonical key from server/.env (last4=...$last4)" -ForegroundColor Cyan

if (-not $SkipFly) {
  Write-Host "`n--- Fly (hammer-voice-telephony) ---" -ForegroundColor Yellow
  & (Join-Path $PSScriptRoot "fly-secrets-from-env.ps1")
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not $SkipVercel) {
  Write-Host "`n--- Vercel ($VercelProject) ---" -ForegroundColor Yellow
  if (-not $env:VERCEL_TOKEN) {
    Write-Host "VERCEL_TOKEN not set - skip Vercel or run:" -ForegroundColor Yellow
    Write-Host "  `$env:VERCEL_TOKEN = '...'; .\scripts\sync-secrets-from-server-env.ps1" -ForegroundColor DarkGray
  } else {
    & (Join-Path $repoRoot "scripts\push-all-env-to-vercel.ps1") -ProjectName $VercelProject
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  }
}

Write-Host "`nDone. Redeploy Fly from repo root:" -ForegroundColor Green
Write-Host "  fly deploy --config demo/realtime-sales-demo/fly.toml" -ForegroundColor White
Write-Host "Then redeploy Vercel production if you updated Vercel env." -ForegroundColor DarkGray
