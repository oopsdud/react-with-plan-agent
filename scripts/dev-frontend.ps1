$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath "$projectRoot/frontend"
& npm.cmd run dev
exit $LASTEXITCODE
