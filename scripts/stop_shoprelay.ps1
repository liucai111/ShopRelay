[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$manifestPath = Join-Path $root '.runtime\shoprelay-processes.json'
if (-not (Test-Path $manifestPath)) { Write-Host 'No ShopRelay-owned runtime manifest; nothing was stopped.'; exit 0 }
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
if ([IO.Path]::GetFullPath($manifest.project_root) -ne $root) { throw 'Runtime manifest project root mismatch; refusing to stop processes.' }

foreach ($entry in @(@{Id=$manifest.vite_pid; Match='frontend\\node_modules\\vite\\bin\\vite\.js'},
                    @{Id=$manifest.api_pid; Match='uvicorn.*scripts\.shoprelay_asgi:app'})) {
    if (-not $entry.Id) { continue }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($entry.Id)" -ErrorAction SilentlyContinue
    if (-not $processInfo) { continue }
    if ($processInfo.CommandLine -notmatch [regex]::Escape($root) -or $processInfo.CommandLine -notmatch $entry.Match) {
        throw "PID $($entry.Id) no longer matches the ShopRelay process signature; refusing to stop it."
    }
    Stop-Process -Id $entry.Id -Force
    Write-Host "Stopped ShopRelay PID $($entry.Id)."
}

foreach ($container in @($manifest.started_containers)) {
    if (-not $container) { continue }
    if ($container -notin @('shoprelay-m0-postgres-1','shoprelay-m0-qdrant-1')) { throw "Unrecognized container in runtime manifest: $container" }
    $state = & docker.exe inspect --format '{{.State.Status}}' $container 2>$null
    if ($LASTEXITCODE -eq 0 -and $state -eq 'running') { & docker.exe stop $container | Out-Null; Write-Host "Stopped ShopRelay container started by this script: $container" }
}
Remove-Item -LiteralPath $manifestPath -Force
Write-Host 'ShopRelay-owned processes stopped. PostgreSQL data volumes and Qdrant stores were not removed.'
