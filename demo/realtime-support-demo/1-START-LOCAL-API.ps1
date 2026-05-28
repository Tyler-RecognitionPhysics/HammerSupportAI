# Start Hammer Support API on port 8781
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\server
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "Created server/.env from .env.example — add your API keys."
}
$env:SUPPORT_REPO_ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# Free port 8781 if a stale uvicorn reloader is still bound (common on Windows dev)
Get-NetTCPConnection -LocalPort 8781 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

py -3 -m uvicorn app:app --host 127.0.0.1 --port 8781 --reload --reload-include .env
