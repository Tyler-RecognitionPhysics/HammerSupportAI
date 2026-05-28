# Start Hammer Support web on port 5174
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\web
if (-not (Test-Path node_modules)) { npm ci }
npm run dev
