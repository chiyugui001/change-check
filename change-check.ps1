# Forward arguments as an argv array; never evaluate command text.
$ErrorActionPreference = 'Stop'
$kbPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $kbPython)) {
    $kbPython = (Get-Command python -ErrorAction Stop).Source
}
& $kbPython -B -X utf8 (Join-Path $PSScriptRoot 'src\main.py') --default-root (Get-Location).Path @args
exit $LASTEXITCODE
