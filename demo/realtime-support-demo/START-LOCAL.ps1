# Start Hammer Support AI locally (API + web)
$ErrorActionPreference = "Stop"
$Server = Join-Path $PSScriptRoot "server"
$Web = Join-Path $PSScriptRoot "web"

function Read-DotEnvKey($path, $key) {
  if (-not (Test-Path $path)) { return "" }
  Get-Content $path | ForEach-Object {
    if ($_ -match "^\s*$key\s*=\s*(.*)\s*$") { return $matches[1].Trim() }
  }
  return ""
}

$envPath = Join-Path $Server ".env"
$openai = Read-DotEnvKey $envPath "OPENAI_API_KEY"
$elKey = Read-DotEnvKey $envPath "ELEVENLABS_API_KEY"
$elAgent = Read-DotEnvKey $envPath "ELEVENLABS_AGENT_ID"

Write-Host ""
Write-Host "Hammer Support AI - starting local servers" -ForegroundColor Cyan
Write-Host ""

if (-not $openai -or $openai -match "your-key") {
  Write-Host "WARNING: OPENAI_API_KEY is missing in server/.env" -ForegroundColor Yellow
  Write-Host "  Text chat will not work until you add it." -ForegroundColor Yellow
} else {
  Write-Host "OPENAI_API_KEY: configured" -ForegroundColor Green
}

if (-not $elKey -or -not $elAgent -or $elAgent -match "agent_\.\.\.") {
  Write-Host "WARNING: ElevenLabs keys missing - voice calls will not connect." -ForegroundColor Yellow
} else {
  Write-Host "ElevenLabs: configured" -ForegroundColor Green
}

Write-Host ""
Write-Host "URLs (after servers start):" -ForegroundColor Cyan
Write-Host "  Customer app:  http://127.0.0.1:5174"
Write-Host "  Admin:         http://127.0.0.1:8781/debug/support-dashboard"
Write-Host "  Admin password: local-dev-support  (from server/.env SUPPORT_ADMIN_SECRET)"
Write-Host ""

# Start API in new window
$apiCmd = "Set-Location '$Server'; `$env:SUPPORT_REPO_ROOT='C:\Users\tbenn\Desktop\HammerSupportAI\HAMMERFINALSITE'; py -3 -m uvicorn app:app --host 127.0.0.1 --port 8781 --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $apiCmd

Start-Sleep -Seconds 2

# Start web in new window
$webCmd = "Set-Location '$Web'; npm run dev"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $webCmd

Start-Sleep -Seconds 4

try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8781/api/health" -TimeoutSec 10
  Write-Host "API health: $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch {
  Write-Host "API not ready yet - check the API terminal window." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Opening customer app in browser..."
Start-Process "http://127.0.0.1:5174"
