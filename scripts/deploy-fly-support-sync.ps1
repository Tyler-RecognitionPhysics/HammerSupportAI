# Deploy persistent HubSpot ticket sync host to Fly.io
#
#   fly auth login
#   .\scripts\deploy-fly-support-sync.ps1
#
# First-time only (creates 10GB volume in iad):
#   fly volumes create support_sync_data --size 10 --region iad -a hammer-support-sync

param(
  [string]$Org = "anna-sheneman",
  [string]$AppName = "hammer-support-sync"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$demoRoot = Join-Path $root "demo\realtime-support-demo"
Set-Location $demoRoot

$fly = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" }
       elseif (Get-Command flyctl -ErrorAction SilentlyContinue) { "flyctl" }
       else { $null }
if (-not $fly) {
  Write-Error "Install Fly CLI: iwr https://fly.io/install.ps1 -useb | iex"
}
$env:Path = "$env:USERPROFILE\.fly\bin;$env:Path"

$who = & $fly auth whoami 2>&1 | Out-String
if ($who -match "Error|not logged") {
  Write-Host "Run: $fly auth login" -ForegroundColor Yellow
  & $fly auth login
}

$appList = & $fly apps list 2>&1 | Out-String
if ($appList -notmatch [regex]::Escape($AppName)) {
  Write-Host "Creating Fly app $AppName in org $Org ..." -ForegroundColor Cyan
  & $fly apps create $AppName --org $Org 2>&1 | Out-Host
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$volList = & $fly volumes list -a $AppName 2>&1 | Out-String
if ($volList -notmatch "support_sync_data") {
  Write-Host "Creating volume support_sync_data (10GB, iad) ..." -ForegroundColor Cyan
  & $fly volumes create support_sync_data --size 10 --region iad -a $AppName -y 2>&1 | Out-Host
}

Write-Host "Setting secrets from server/.env ..." -ForegroundColor Cyan
& (Join-Path $demoRoot "scripts\fly-secrets-support-sync.ps1")

Write-Host "Deploying $AppName ..." -ForegroundColor Cyan
Set-Location $root
& $fly deploy --config demo/realtime-support-demo/fly.toml

$base = "https://${AppName}.fly.dev"
Write-Host ""
Write-Host "Persistent sync host:" -ForegroundColor Green
Write-Host "  $base/api/health"
Write-Host "  $base/api/knowledge/artifact/manifest.json"
Write-Host ""
Write-Host "Set on Vercel (hammer-support-ai-final):" -ForegroundColor Yellow
Write-Host "  SUPPORT_SYNC_HOST_URL=$base"
Write-Host "  SUPPORT_KB_ARTIFACT_URL=$base/api/knowledge/artifact"
Write-Host "  SUPPORT_KB_ARTIFACT_TOKEN=<same as SUPPORT_ADMIN_SECRET>"
Write-Host ""
Write-Host "Seed local tickets to Fly volume (first time):" -ForegroundColor Yellow
Write-Host "  .\scripts\seed-fly-support-sync.ps1"
Write-Host ""
Write-Host "Trigger full backfill on Fly:" -ForegroundColor Yellow
Write-Host "  .\scripts\trigger-support-tickets-sync.ps1 -FullBackfill"
