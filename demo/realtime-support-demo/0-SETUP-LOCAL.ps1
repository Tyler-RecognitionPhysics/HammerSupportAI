# One-time / refresh local setup for Hammer Support AI
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Server = Join-Path $PSScriptRoot "server"
$Web = Join-Path $PSScriptRoot "web"

Write-Host "Hammer Support AI - local setup" -ForegroundColor Cyan
Write-Host "Repo: $Repo"

if (-not (Test-Path (Join-Path $Server ".env"))) {
  Copy-Item (Join-Path $Server ".env.example") (Join-Path $Server ".env")
  Write-Host "Created server/.env - add OPENAI_API_KEY (and ElevenLabs for voice)." -ForegroundColor Yellow
}

Write-Host "Installing Python dependencies..."
Set-Location $Server
py -3 -m pip install -q -r requirements.txt

Write-Host "Building support knowledge index..."
Set-Location $Repo
py -3 knowledge_support\scripts\sync_sqlite.py

Write-Host "Installing web dependencies..."
Set-Location $Web
if (-not (Test-Path node_modules)) { npm ci } else { Write-Host "node_modules OK" }

Write-Host ""
Write-Host "Setup complete. Next:" -ForegroundColor Green
Write-Host "  1. Edit demo\realtime-support-demo\server\.env and set OPENAI_API_KEY"
Write-Host "  2. For voice: ELEVENLABS_API_KEY + ELEVENLABS_AGENT_ID"
Write-Host "  3. Run: .\START-LOCAL.ps1"
