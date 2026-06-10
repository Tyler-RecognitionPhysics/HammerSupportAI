# Trigger HubSpot ticket sync on the persistent Fly host.

param(
  [switch]$FullBackfill,
  [string]$AppName = "hammer-support-sync",
  [string]$EnvPath = ""
)

$ErrorActionPreference = "Stop"
$demoRoot = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "demo\realtime-support-demo"
if (-not $EnvPath) { $EnvPath = Join-Path $demoRoot "server\.env" }

$secret = ""
foreach ($line in Get-Content $EnvPath -Encoding UTF8) {
  if ($line -match '^\s*SUPPORT_ADMIN_SECRET=(.+)$') {
    $secret = $Matches[1].Trim().Trim('"').Trim("'")
    break
  }
}
if (-not $secret) { Write-Error "SUPPORT_ADMIN_SECRET not found in $EnvPath" }

$url = "https://${AppName}.fly.dev/api/admin/support/knowledge/hubspot-tickets/sync"
$body = @{ full_backfill = [bool]$FullBackfill; background = $true } | ConvertTo-Json

Write-Host "POST $url" -ForegroundColor Cyan
$response = Invoke-RestMethod -Uri $url -Method POST -Headers @{
  "Authorization" = "Bearer $secret"
  "Content-Type" = "application/json"
} -Body $body

$response | ConvertTo-Json -Depth 5
if ($response.started) {
  Write-Host "Sync started in background on Fly. Check status in /admin/support or:" -ForegroundColor Green
  Write-Host "  GET https://${AppName}.fly.dev/api/admin/support/knowledge/hubspot-tickets/status"
}
