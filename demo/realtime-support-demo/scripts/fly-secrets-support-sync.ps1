# Push support sync secrets to Fly (hammer-support-sync).
# Usage: .\scripts\fly-secrets-support-sync.ps1

$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $demoRoot "server\.env"

$fly = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" }
       elseif (Get-Command flyctl -ErrorAction SilentlyContinue) { "flyctl" }
       else { $null }
if (-not $fly) {
  Write-Host "Install Fly CLI first: https://fly.io/docs/hands-on/install-flyctl/" -ForegroundColor Red
  exit 1
}
$env:Path = "$env:USERPROFILE\.fly\bin;$env:Path"
if (-not (Test-Path $envPath)) {
  Write-Host "Missing $envPath" -ForegroundColor Red
  exit 1
}

$keys = @(
  "OPENAI_API_KEY",
  "HUBSPOT_PRIVATE_APP_TOKEN",
  "HUBSPOT_ACCESS_TOKEN",
  "HUBSPOT_PORTAL_ID",
  "HUBSPOT_KNOWLEDGE_BASE_ID",
  "HUBSPOT_CLOSED_STAGE_IDS",
  "HUBSPOT_TICKET_PROPERTIES",
  "SUPPORT_ADMIN_SECRET",
  "SUPPORT_KB_ARTIFACT_TOKEN",
  "SLACK_BOT_TOKEN",
  "SLACK_SUPPORT_CHANNEL_ID"
)

$fromFile = @{}
foreach ($line in Get-Content $envPath -Encoding UTF8) {
  if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
  $name = $Matches[1]
  $val = $Matches[2].Trim().Trim('"').Trim("'")
  if ($name -in $keys -and $val) {
    $fromFile[$name] = $val
  }
}

$secretArgs = @()
foreach ($name in $keys) {
  if ($fromFile.ContainsKey($name)) {
    $secretArgs += "${name}=$($fromFile[$name])"
  }
}

$secretArgs += "SUPPORT_REPO_ROOT=/app"
$secretArgs += "SUPPORT_DATA_DIR=/data"
$secretArgs += "SUPPORT_RAW_DIR=/data/raw/support-data"
$secretArgs += "SUPPORT_KB_DB=/data/support_kb.sqlite"
$secretArgs += "SUPPORT_HUBSPOT_TICKETS_STATE_DB=/data/hubspot_tickets_sync.sqlite"

if ($secretArgs.Count -lt 3) {
  Write-Host "Few secrets from .env - ensure HUBSPOT_PRIVATE_APP_TOKEN and SUPPORT_ADMIN_SECRET are set." -ForegroundColor Yellow
}

Write-Host "Setting $($secretArgs.Count) Fly secrets on hammer-support-sync (values hidden)..." -ForegroundColor Cyan
& $fly secrets set @secretArgs -a hammer-support-sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Fly secrets updated." -ForegroundColor Green
