$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& py -3 -m harness.cli @args
exit $LASTEXITCODE
