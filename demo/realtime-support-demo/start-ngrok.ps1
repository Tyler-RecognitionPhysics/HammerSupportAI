# Expose local API (uvicorn :8780) to Zapier for POST /api/zapier/approval
# Prereqs: ngrok installed, authtoken configured, ngrok update (need 3.20+)
# Keep run-demo.ps1 running in another terminal first.

$ErrorActionPreference = "Stop"
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
  [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host "=== ngrok tunnel -> http://127.0.0.1:8780 ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Start API first:  .\1-START-LOCAL-API.ps1"
Write-Host "2. After ngrok starts, open http://127.0.0.1:4040 for the https Forwarding URL"
Write-Host "3. Zap 2 POST URL:  https://<subdomain>.ngrok-free.dev/api/zapier/approval"
Write-Host "4. Zap 2 headers:   Content-Type, X-Zapier-Secret, ngrok-skip-browser-warning=true"
Write-Host "5. See LOCAL_NGROK.md for Data field mapping"
Write-Host ""
ngrok http 8780
