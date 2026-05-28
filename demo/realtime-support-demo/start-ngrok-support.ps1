# Expose local Support API (uvicorn :8781) for ElevenLabs Custom LLM + Zapier.
# Prereqs: ngrok installed, authtoken configured (ngrok config add-authtoken …)

$ErrorActionPreference = "Stop"
Write-Host "=== ngrok -> http://127.0.0.1:8781 (Hammer Support API) ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Start API first: .\1-START-LOCAL-API.ps1"
Write-Host "2. After ngrok starts, open http://127.0.0.1:4040 for the https Forwarding URL"
Write-Host "3. ElevenLabs agent -> Custom LLM URL:"
Write-Host "     https://<your-subdomain>.ngrok-free.dev/api/elevenlabs/llm"
Write-Host "4. Restart ngrok = update that URL in ElevenLabs each time"
Write-Host ""
ngrok http 8781
