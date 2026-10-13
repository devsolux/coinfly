# Native Windows AMD64 convenience wrapper. No execution policy changes.
$ErrorActionPreference = 'Stop'
& py -3 "$PSScriptRoot/dev.py" @args
exit $LASTEXITCODE
