$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
# Some Windows environments define DEBUG=release, which conflicts with bool settings.
$env:DEBUG = "false"
$pythonCommand = "python"
if (Test-Path -LiteralPath "$projectRoot/.venv/Scripts/python.exe") {
    $pythonCommand = "$projectRoot/.venv/Scripts/python.exe"
} elseif (Test-Path -LiteralPath "$projectRoot/.python_packages") {
    $env:PYTHONPATH = "$projectRoot/.python_packages" + [IO.Path]::PathSeparator + $env:PYTHONPATH
}
& $pythonCommand -m uvicorn agent_system.api.routes:app --reload --reload-dir agent_system --host 127.0.0.1 --port 8000 --limit-concurrency 128 --backlog 128
exit $LASTEXITCODE
