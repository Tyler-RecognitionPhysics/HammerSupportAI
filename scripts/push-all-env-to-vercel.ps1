# Push production env vars from demo/realtime-sales-demo/server/.env to a Vercel project.
# Requires: VERCEL_TOKEN from https://vercel.com/account/tokens
#
# Usage:
#   $env:VERCEL_TOKEN = "..."
#   .\scripts\push-all-env-to-vercel.ps1 -ProjectName your-vercel-project

param(
  [string]$ProjectName = "hammer-finalsite",
  [string]$EnvFile = "demo/realtime-sales-demo/server/.env"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$envPath = Join-Path $root $EnvFile
if (-not (Test-Path $envPath)) {
  Write-Error "Missing $envPath"
}

$token = $env:VERCEL_TOKEN
if (-not $token) {
  Write-Error "Set VERCEL_TOKEN (https://vercel.com/account/tokens) then re-run."
}

$keys = @(
  "OPENAI_API_KEY",
  "ELEVENLABS_API_KEY",
  "ELEVENLABS_AGENT_ID",
  "ELEVENLABS_CHAT_MODEL",
  "ELEVENLABS_WEBHOOK_SECRET",
  "REALTIME_SALES_ADMIN_SECRET",
  "HAMMER_AGREEMENT_SENDER_EMAIL",
  "GOOGLE_CALENDAR_TIMEZONE",
  "GOOGLE_CALENDAR_APPT_DURATION_MINUTES",
  "GOOGLE_CALENDAR_DELEGATED_USER",
  "GOOGLE_CALENDAR_CREDENTIALS_JSON",
  "REALTIME_SALES_PUBLIC_BASE_URL",
  "REALTIME_SALES_MODEL",
  "REALTIME_SALES_CHAT_MODEL",
  "ZAPIER_LEAD_WEBHOOK_URL",
  "ZAPIER_WEBSITE_LEAD_WEBHOOK_URL",
  "ZAPIER_VOICE_CALL_SUMMARY_WEBHOOK_URL",
  "ZAPIER_APPROVAL_CALLBACK_SECRET",
  "HAMMER_OFFICE_EMAIL",
  "HAMMER_OFFICE_PASSWORD",
  "HAMMER_OFFICE_USE_PLAYWRIGHT",
  "HAMMER_OFFICE_HEADLESS",
  "HAMMER_OFFICE_KEEP_OPEN",
  "HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT",
  "HAMMER_OFFICE_SLOW_MO",
  "DEMO_PHONE_NUMBER",
  "DEMO_PHONE_DISPLAY",
  "VITE_DEMO_PHONE_NUMBER",
  "VITE_ENABLE_BROWSER_VOICE",
  "VITE_ENABLE_CHAT",
  "VITE_SIGN_IN_URL"
)

# Last assignment per key wins (matches server app + fly-secrets-from-env.ps1).
$fromFile = @{}
foreach ($line in Get-Content $envPath -Encoding UTF8) {
  if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
  $fromFile[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
}

# Production site URL (emails, logo links). Use custom domain when DNS is on this project.
$fromFile["REALTIME_SALES_PUBLIC_BASE_URL"] = "https://hammer-finalsite.vercel.app"
# Browser WebRTC hero orb + mic (matches "Test our voice AI right now" hero copy).
$fromFile["VITE_ENABLE_BROWSER_VOICE"] = "1"
if (-not $fromFile.ContainsKey("VITE_ENABLE_CHAT") -or $fromFile["VITE_ENABLE_CHAT"] -match '^(0|false|no|off)$') {
  $fromFile["VITE_ENABLE_CHAT"] = "1"
}
$fromFile["HAMMER_OFFICE_USE_PLAYWRIGHT"] = "1"
$fromFile["HAMMER_OFFICE_HEADLESS"] = "1"
$fromFile["HAMMER_OFFICE_KEEP_OPEN"] = "0"
$fromFile["HAMMER_OFFICE_KEEP_OPEN_AFTER_SUBMIT"] = "0"
$fromFile["HAMMER_OFFICE_SLOW_MO"] = "0"
$fromFile.Remove("HAMMER_OFFICE_ALLOW_VISIBLE_BROWSER")
if ($fromFile.ContainsKey("DEMO_PHONE_DISPLAY") -and $fromFile["DEMO_PHONE_DISPLAY"]) {
  $fromFile["VITE_DEMO_PHONE_NUMBER"] = $fromFile["DEMO_PHONE_DISPLAY"]
}

# Google Calendar — Vercel cannot read local credential file paths; inline JSON if only path is set.
if (-not $fromFile.ContainsKey("GOOGLE_CALENDAR_CREDENTIALS_JSON") -or -not $fromFile["GOOGLE_CALENDAR_CREDENTIALS_JSON"]) {
  $credPath = $fromFile["GOOGLE_APPLICATION_CREDENTIALS"]
  if ($credPath -and (Test-Path $credPath)) {
    $fromFile["GOOGLE_CALENDAR_CREDENTIALS_JSON"] = (Get-Content $credPath -Raw -Encoding UTF8).Trim()
  }
}

$headers = @{
  Authorization  = "Bearer $token"
  "Content-Type" = "application/json"
}
$uriBase = "https://api.vercel.com/v10/projects/$ProjectName/env?upsert=true"

$set = 0
foreach ($key in $keys) {
  if (-not $fromFile.ContainsKey($key)) { continue }
  $val = $fromFile[$key]
  if (-not $val) { continue }
  $body = @{
    key    = $key
    value  = $val
    type   = "encrypted"
    target = @("production", "preview")
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri $uriBase -Headers $headers -Body $body | Out-Null
  $set++
}

Write-Host "Set $set env vars on Vercel project '$ProjectName'." -ForegroundColor Green
Write-Host "Redeploy production in Vercel dashboard." -ForegroundColor Yellow
Write-Host "Phone webhook is NOT on Vercel - use Fly (see GO-LIVE-TODAY.md)." -ForegroundColor Cyan
