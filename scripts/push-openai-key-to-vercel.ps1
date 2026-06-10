# Sync OPENAI_API_KEY from demo/realtime-sales-demo/server/.env to a Vercel project.
# Usage:
#   $env:VERCEL_TOKEN = "<token from https://vercel.com/account/tokens>"
#   .\scripts\push-openai-key-to-vercel.ps1 -ProjectName sellmeapen

param(
  [string]$ProjectName = "sellmeapen",
  [string]$EnvFile = "demo/realtime-sales-demo/server/.env"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$envPath = Join-Path $root $EnvFile
if (-not (Test-Path $envPath)) {
  Write-Error "Missing $envPath — add OPENAI_API_KEY=sk-... first."
}

$key = $null
foreach ($line in Get-Content $envPath -Encoding UTF8) {
  $t = $line.Trim()
  if ($t -match '^\s*OPENAI_API_KEY\s*=\s*(.+)\s*$') {
    $key = $Matches[1].Trim().Trim('"').Trim("'")
  }
}
if (-not $key -or $key.Length -lt 10) {
  Write-Error "OPENAI_API_KEY not found in $envPath"
}

$token = $env:VERCEL_TOKEN
if (-not $token) {
  Write-Error "Set VERCEL_TOKEN (create at https://vercel.com/account/tokens) then re-run."
}

$body = @{
  key    = "OPENAI_API_KEY"
  value  = $key
  type   = "encrypted"
  target = @("production", "preview", "development")
} | ConvertTo-Json

$uri = "https://api.vercel.com/v10/projects/$ProjectName/env?upsert=true"
$headers = @{
  Authorization  = "Bearer $token"
  "Content-Type" = "application/json"
}

Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $body | Out-Null
Write-Host "OPENAI_API_KEY set on Vercel project '$ProjectName'. Redeploy production for it to take effect."
