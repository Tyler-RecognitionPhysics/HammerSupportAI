# Push Support AI env vars to Vercel hammer-support-ai-final
Set-Location (Join-Path $PSScriptRoot "..")

$skip = @("SUPPORT_REPO_ROOT", "SUPPORT_CORS_ORIGINS")
$extra = @{
    SUPPORT_SERVERLESS      = "1"
    SUPPORT_PUBLIC_BASE_URL = "https://hammer-support-ai-final.vercel.app"
}

$vars = @{}
Get-Content "demo\realtime-support-demo\server\.env" | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
        $n = $matches[1].Trim()
        $v = $matches[2].Trim()
        if ($v -and ($skip -notcontains $n)) { $vars[$n] = $v }
    }
}
foreach ($k in $extra.Keys) { $vars[$k] = $extra[$k] }

$targets = @("production", "preview")
foreach ($name in ($vars.Keys | Sort-Object)) {
    $val = $vars[$name]
    foreach ($target in $targets) {
        Write-Host "[$target] $name"
        cmd /c "vercel env add `"$name`" $target --value `"$val`" --yes --force --non-interactive" 2>nul
    }
}
Write-Host "Complete."
