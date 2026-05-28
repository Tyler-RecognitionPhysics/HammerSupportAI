# Copies DEMO_PHONE_* from server/.env into wiki/demo-public-site-copy.md
$ErrorActionPreference = "Stop"
$demoRoot = Split-Path $PSScriptRoot -Parent
$envPath = Join-Path $demoRoot "server\.env"
$wikiPath = Join-Path (Resolve-Path (Join-Path $demoRoot "..\..")).Path "wiki\demo-public-site-copy.md"

$vars = @{}
foreach ($line in Get-Content $envPath) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$' -and $line -notmatch '^\s*#') {
    $vars[$Matches[1]] = $Matches[2].Trim()
  }
}
$num = $vars["DEMO_PHONE_NUMBER"]
$display = $vars["DEMO_PHONE_DISPLAY"]
if (-not $num) {
  Write-Host "Set DEMO_PHONE_NUMBER in server\.env first." -ForegroundColor Yellow
  exit 1
}
if (-not $display) { $display = $num }
$digits = ($num -replace '\D', '')

$content = Get-Content $wikiPath -Raw
$content = $content -replace '(?ms)(## rt_demo_phone\r?\n\r?\n)[^\r\n#]+', "`${1}$display"
$content = $content -replace '(?ms)(## rt_demo_phone_display\r?\n\r?\n)[^\r\n#]+', "`${1}$display"
$content = $content -replace '(?ms)(## rt_demo_phone_tel\r?\n\r?\n)[^\r\n#]+', "`${1}$digits"
Set-Content -Path $wikiPath -Value $content -Encoding utf8
Write-Host "Updated wiki phone keys: $display ($digits)" -ForegroundColor Green
