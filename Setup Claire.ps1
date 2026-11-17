$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $python)) {
    if ($uv) {
        & $uv.Source venv --python 3.12 .venv
    } else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) { & $py.Source -3 -m venv .venv }
        else {
            $basePython = Get-Command python -ErrorAction SilentlyContinue
            if (-not $basePython) { throw 'Install Python 3.9 or newer, or uv, then run setup again.' }
            & $basePython.Source -m venv .venv
        }
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) { throw 'A Python environment could not be created. Install Python 3.9+ or uv and retry.' }
}
if ($uv) { & $uv.Source pip install --python $python -r (Join-Path $PSScriptRoot 'requirements-windows.txt') }
else { & $python -m pip install -r (Join-Path $PSScriptRoot 'requirements-windows.txt') }
if ($LASTEXITCODE -ne 0) { throw 'Windows dependencies could not be installed. Check the connection and retry setup.' }
Write-Output 'Claire is ready. Double-click Start Claire.cmd to open the app.'
