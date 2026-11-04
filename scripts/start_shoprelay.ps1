[CmdletBinding()]
param(
    [ValidateSet('Demo', 'Integration')][string]$Mode = 'Demo',
    [ValidateSet('Stub', 'Configured')][string]$ProviderMode = 'Stub',
    [switch]$StartExistingDependencies,
    [switch]$RequireHybrid,
    [switch]$PrewarmHybrid,
    [switch]$AllowRemoteProvider,
    [switch]$EnableIsolatedTestActions,
    [string]$AdminUserId,
    [switch]$NoFrontend
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $root '.runtime'
$logDir = Join-Path $runtimeDir 'logs'
$manifestPath = Join-Path $runtimeDir 'shoprelay-processes.json'
$python = Join-Path $root '.venv\Scripts\python.exe'
$viteCli = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue

if (-not (Test-Path $python)) { throw 'Project .venv Python is missing. Do not install globally; follow docs/runtime-and-deployment.md.' }
if (-not (Test-Path $viteCli) -and -not $NoFrontend) { throw 'Frontend dependencies are missing. Run npm ci from frontend/ using the approved registry.' }
if (-not $NoFrontend -and -not $nodeCommand) { throw 'node.exe is unavailable.' }
if ($Mode -eq 'Demo' -and $AdminUserId) { throw 'Demo mode cannot grant administrator access.' }
if ($Mode -eq 'Demo' -and $EnableIsolatedTestActions) { throw 'Demo mode never enables business actions.' }
if ($Mode -eq 'Integration' -and $AdminUserId -and $AdminUserId.Length -gt 64) { throw 'AdminUserId exceeds the application identifier limit.' }
if ($Mode -ne 'Integration' -and $PrewarmHybrid) { throw 'Hybrid prewarm is available only in explicit integration mode.' }
if ($PrewarmHybrid -and -not $RequireHybrid) { throw 'PrewarmHybrid requires -RequireHybrid so missing models cannot be hidden.' }
if ($Mode -ne 'Integration' -and $EnableIsolatedTestActions) { throw 'Isolated test actions require explicit Integration mode.' }
if ($ProviderMode -eq 'Configured' -and -not $AllowRemoteProvider) { throw 'Configured provider may send data externally; pass -AllowRemoteProvider only after reviewing the outbound data.' }
if (Test-Path $manifestPath) { throw "A ShopRelay process manifest already exists. Run scripts/stop_shoprelay.ps1 after checking ownership." }

$ports = @(8000)
if (-not $NoFrontend) { $ports += 5173 }
foreach ($port in $ports) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { throw "Port $port is already occupied by PID $($listener.OwningProcess); refusing to stop or replace it." }
}

$startedContainers = [System.Collections.Generic.List[string]]::new()
try {
if ($StartExistingDependencies) {
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $docker) { throw 'Docker CLI is unavailable; no container was created or changed.' }
    $server = & $docker.Source info --format '{{.ServerVersion}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $server) { throw 'Docker Engine is unavailable; start Docker Desktop and rerun. No container was changed.' }
    foreach ($container in @('shoprelay-m0-postgres-1','shoprelay-m0-qdrant-1')) {
        $containerState = & $docker.Source inspect --format '{{.State.Status}}' $container 2>$null
        if ($LASTEXITCODE -ne 0) {
            if ($container -eq 'shoprelay-m0-postgres-1') { throw 'Existing shoprelay-m0-postgres-1 container was not found; this script never creates containers or volumes.' }
            continue
        }
        if ($containerState -ne 'running') {
            & $docker.Source start $container | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not start existing container $container." }
            $startedContainers.Add($container)
        }
    }
}
} catch {
    foreach ($container in $startedContainers) { & docker.exe stop $container 2>$null | Out-Null }
    throw
}

$saved = @{}
$keys = @('PYTHONPATH','SHOPRELAY_ORCHESTRATOR','SHOPRELAY_M3_SUPERVISOR_ENABLED',
          'SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS','SHOPRELAY_RETRIEVAL_PREWARM','SHOPRELAY_API_ORIGIN',
          'SHOPRELAY_RUNTIME_MODE','SHOPRELAY_PROVIDER_MODE','SHOPRELAY_ENABLE_ISOLATED_TEST_ACTIONS')
foreach ($key in $keys) { $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }

$pythonPid = $null
$vitePid = $null
try {
    $env:PYTHONPATH = "$root\.vendor\g2;$root\.vendor;$root"
    $env:SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS = ''
    $env:SHOPRELAY_RETRIEVAL_PREWARM = if ($PrewarmHybrid) { 'true' } else { 'false' }
    $env:SHOPRELAY_RUNTIME_MODE = $Mode.ToLowerInvariant()
    $env:SHOPRELAY_PROVIDER_MODE = $ProviderMode.ToLowerInvariant()
    $env:SHOPRELAY_ENABLE_ISOLATED_TEST_ACTIONS = if ($EnableIsolatedTestActions) { 'true' } else { 'false' }
    if ($Mode -eq 'Demo') {
        $env:SHOPRELAY_ORCHESTRATOR = 'legacy'
        $env:SHOPRELAY_M3_SUPERVISOR_ENABLED = 'false'
    } else {
        $env:SHOPRELAY_ORCHESTRATOR = 'langgraph'
        $env:SHOPRELAY_M3_SUPERVISOR_ENABLED = 'true'
        if ($AdminUserId) { $env:SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS = $AdminUserId }
    }

    $preflightArgs = @((Join-Path $root 'scripts\check_shoprelay_runtime.py'), '--mode', $Mode.ToLowerInvariant())
    if ($RequireHybrid) { $preflightArgs += '--require-hybrid' }
    & $python @preflightArgs
    if ($LASTEXITCODE -ne 0) { throw 'Runtime preflight failed; API and frontend were not started.' }

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $apiOut = Join-Path $logDir 'api.stdout.log'
    $apiErr = Join-Path $logDir 'api.stderr.log'
    $apiArgs = @('-m','uvicorn','scripts.shoprelay_asgi:app','--app-dir',$root,'--host','127.0.0.1','--port','8000')
    $api = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $root -PassThru -WindowStyle Hidden -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
    $pythonPid = $api.Id

    if (-not $NoFrontend) {
        $env:SHOPRELAY_API_ORIGIN = 'http://127.0.0.1:8000'
        $viteOut = Join-Path $logDir 'vite.stdout.log'
        $viteErr = Join-Path $logDir 'vite.stderr.log'
        $viteArgs = @($viteCli,'--host','127.0.0.1','--port','5173','--strictPort')
        $vite = Start-Process -FilePath $nodeCommand.Source -ArgumentList $viteArgs -WorkingDirectory (Join-Path $root 'frontend') -PassThru -WindowStyle Hidden -RedirectStandardOutput $viteOut -RedirectStandardError $viteErr
        $vitePid = $vite.Id
    }

    $deadline = (Get-Date).AddSeconds(75)
    do {
        Start-Sleep -Milliseconds 750
        try { $live = Invoke-RestMethod 'http://127.0.0.1:8000/health/live' -TimeoutSec 2 } catch { $live = $null }
        try { $ready = Invoke-RestMethod 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2 } catch { $ready = $null }
        if ($live.status -eq 'live' -and $ready.status -eq 'ready') { break }
        if ((Get-Date) -gt $deadline) { throw 'API did not become ready; check .runtime/logs (may contain request diagnostics).' }
    } while ($true)

    if (-not $NoFrontend) {
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 500
            try { $web = Invoke-WebRequest 'http://127.0.0.1:5173/' -TimeoutSec 2 } catch { $web = $null }
            if ($web.StatusCode -eq 200) { break }
            if ((Get-Date) -gt $deadline) { throw 'Vite did not become available; check .runtime/logs.' }
        } while ($true)
    }

    $manifest = [ordered]@{
        project_root = $root
        mode = $Mode
        api_pid = $pythonPid
        vite_pid = $vitePid
        started_containers = @($startedContainers)
        started_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $manifestPath
    Write-Host "ShopRelay is ready. Mode=$Mode; API=http://127.0.0.1:8000; UI=$(if ($NoFrontend) {'not started'} else {'http://127.0.0.1:5173'})."
    Write-Host 'High-risk action flags remain at application defaults (closed). Stop only with scripts/stop_shoprelay.ps1.'
} catch {
    foreach ($pidToStop in @($vitePid,$pythonPid)) { if ($pidToStop) { Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue } }
    foreach ($container in $startedContainers) { & docker.exe stop $container 2>$null | Out-Null }
    throw
} finally {
    foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
}
