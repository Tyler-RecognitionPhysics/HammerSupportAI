# Deploy phone/SIP server to Fly.io
# Billing org: anna-sheneman - CLI must be logged into that Fly account.
#
#   fly auth login    # use GitHub/Google for anna-sheneman dashboard
#   fly orgs list     # must show anna-sheneman
#   .\scripts\deploy-fly-telephony.ps1
#
# Override org: .\scripts\deploy-fly-telephony.ps1 -Org personal

param(
  [string]$Org = "anna-sheneman",
  [string]$AppName = "hammer-voice-telephony"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$demoRoot = Join-Path $root "demo\realtime-sales-demo"
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
  Write-Host "Run: $fly auth login (account with org '$Org')" -ForegroundColor Yellow
  & $fly auth login
}

Write-Host "Fly user: $(& $fly auth whoami 2>$null)" -ForegroundColor DarkGray
Write-Host "Target org: $Org" -ForegroundColor DarkGray
$orgs = & $fly orgs list 2>&1 | Out-String
if ($orgs -notmatch [regex]::Escape($Org)) {
  Write-Host ""
  Write-Host "Org '$Org' not visible to this CLI login." -ForegroundColor Red
  Write-Host "Billing is on https://fly.io/dashboard/$Org/billing but deploy needs:" -ForegroundColor Yellow
  Write-Host "  1. fly auth login - sign in as the owner of that org, OR" -ForegroundColor Yellow
  Write-Host "  2. Fly dashboard - org $Org - Members - invite this user" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "Then: fly orgs list   (should list $Org)" -ForegroundColor Yellow
  exit 1
}

$appList = & $fly apps list 2>&1 | Out-String
if ($appList -notmatch [regex]::Escape($AppName)) {
  Write-Host "Creating Fly app $AppName in org $Org ..." -ForegroundColor Cyan
  & $fly apps create $AppName --org $Org 2>&1 | Out-Host
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Setting secrets from server/.env ..." -ForegroundColor Cyan
Push-Location $demoRoot
& (Join-Path $demoRoot "scripts\fly-secrets-from-env.ps1")
Pop-Location

Write-Host "Deploying $AppName (from repo root — required for Docker context) ..." -ForegroundColor Cyan
Set-Location $root
& $fly deploy --config demo/realtime-sales-demo/fly.toml

$url = "https://${AppName}.fly.dev/api/realtime/sip-webhook"
Write-Host ""
Write-Host "OpenAI webhook URL:" -ForegroundColor Green
Write-Host "  $url"
Write-Host ""
Write-Host "Health: https://${AppName}.fly.dev/api/health" -ForegroundColor Green
