# Windows launcher. Keep this machine's state and notes beside the project.
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Claire needs the project Python environment at .venv\Scripts\python.exe. See GETTING_STARTED.md (Windows).'
}

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $PSScriptRoot
$env:PATH = (Split-Path -Parent $python) + [IO.Path]::PathSeparator + $env:PATH
if (-not $env:CLAIRE_STATE) { $env:CLAIRE_STATE = Join-Path $PSScriptRoot '.claire' }
if (-not $env:CLAIRE_VAULT) { $env:CLAIRE_VAULT = Join-Path $PSScriptRoot 'claire-vault' }
if (-not $env:TZ -and (Get-TimeZone).Id -eq 'India Standard Time') { $env:TZ = 'Asia/Kolkata' }

$claireArgs = @($args)
if ($claireArgs.Count -eq 0) { $claireArgs = @('app') }
# Local commands (including stop/status) should not wake the model server.
if ($claireArgs[0] -notin @('stop', 'status', 'models', 'tools', 'tasks', 'inbox', 'skills', 'schedules', 'autostart', 'approvals', '--help', '-h')) {
    $studioReady = $false
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:1234/api/v1/models' -TimeoutSec 3
        $studioReady = $true
    } catch {}
    if (-not $studioReady) {
        $lms = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
        if (-not (Test-Path -LiteralPath $lms)) { throw 'LM Studio was not found. Open LM Studio and start its local server on port 1234.' }
        & $lms server start --port 1234 --bind 127.0.0.1
        if ($LASTEXITCODE -ne 0) { throw 'LM Studio could not start its local server.' }
    }
    & $python (Join-Path $PSScriptRoot 'scripts\windows_model.py')
    if ($LASTEXITCODE -ne 0) { throw 'The LM Studio model configuration could not be checked.' }
}

& $python -m claire_core @claireArgs
exit $LASTEXITCODE
