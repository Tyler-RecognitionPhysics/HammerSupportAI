# Install Fly and Vercel CLIs for go-live (run once).
$ErrorActionPreference = "Stop"

Write-Host "Installing Fly CLI..." -ForegroundColor Cyan
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://fly.io/install.ps1 -useb | iex"

Write-Host "Installing Vercel CLI via npm..." -ForegroundColor Cyan
npm install -g vercel

Write-Host "Done. Restart PowerShell, then:" -ForegroundColor Green
Write-Host "  fly version"
Write-Host "  vercel --version"
