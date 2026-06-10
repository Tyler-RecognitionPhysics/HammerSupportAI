# Upload pre-built KB sqlite to Fly (fast initial seed).
# For a full ticket markdown corpus on Fly, run: .\scripts\trigger-support-tickets-sync.ps1 -FullBackfill

param(
  [string]$AppName = "hammer-support-sync"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$kbDb = Join-Path $root "knowledge_support\data\support_kb.sqlite"
$stateDb = Join-Path $root "knowledge_support\data\hubspot_tickets_sync.sqlite"

if (-not (Test-Path $kbDb)) {
  Write-Error "Missing $kbDb - run sync_hubspot_tickets.py --full-backfill first"
}

$fly = if (Get-Command fly -ErrorAction SilentlyContinue) { "fly" } else { "flyctl" }
$env:Path = "$env:USERPROFILE\.fly\bin;$env:Path"

Write-Host "Uploading support_kb.sqlite to $AppName ..." -ForegroundColor Cyan
& $fly ssh sftp -a $AppName put $kbDb /data/support_kb.sqlite
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path $stateDb) {
  Write-Host "Uploading hubspot_tickets_sync.sqlite ..." -ForegroundColor Cyan
  & $fly ssh sftp -a $AppName put $stateDb /data/hubspot_tickets_sync.sqlite
}

Write-Host "Writing manifest on Fly ..." -ForegroundColor Cyan
$manifestCmd = 'python -c "import sys; sys.path.insert(0,\"/app\"); from knowledge_support.kb_artifact import write_manifest; print(write_manifest())"'
& $fly ssh console -a $AppName -C $manifestCmd

Write-Host "Seed complete. Set Vercel env vars and redeploy, or call artifact URL to verify." -ForegroundColor Green
