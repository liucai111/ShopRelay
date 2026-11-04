[CmdletBinding()]
param([ValidateSet('Demo','Integration')][string]$Mode = 'Demo', [switch]$RequireHybrid)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Project .venv Python is missing.' }
$oldPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH','Process')
try {
    $env:PYTHONPATH = "$root\.vendor\g2;$root\.vendor;$root"
    $pyArgs = @((Join-Path $root 'scripts\check_shoprelay_runtime.py'), '--mode', $Mode.ToLowerInvariant())
    if ($RequireHybrid) { $pyArgs += '--require-hybrid' }
    # A live app holds the embedded Qdrant directory lock. Startup performs the
    # full active-collection/count check before opening it; a health recheck must
    # not open a second embedded client against that same store.
    try {
        $existingReady = Invoke-RestMethod 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2
        if ($existingReady.status -eq 'ready') { $pyArgs += '--runtime-ready' }
    } catch { }
    & $python @pyArgs
    $preflightExitCode = $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable('PYTHONPATH',$oldPythonPath,'Process')
}
if ($preflightExitCode -ne 0) { exit $preflightExitCode }

foreach ($item in @(@{Name='API live'; Url='http://127.0.0.1:8000/health/live'},
                    @{Name='API ready'; Url='http://127.0.0.1:8000/health/ready'},
                    @{Name='Web'; Url='http://127.0.0.1:5173/'})) {
    try {
        $response = Invoke-WebRequest -Uri $item.Url -TimeoutSec 4
        Write-Host "$($item.Name): HTTP $($response.StatusCode)"
    } catch {
        Write-Host "$($item.Name): unavailable ($($_.Exception.GetType().Name))"
        exit 1
    }
}
