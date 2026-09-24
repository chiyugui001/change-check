$ErrorActionPreference = 'Stop'
$kbEnvironment = Join-Path $PSScriptRoot '.venv'
$kbPython = Join-Path $kbEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $kbPython)) {
    $kbBasePython = (Get-Command python -ErrorAction Stop).Source
    & $kbBasePython -m venv $kbEnvironment
    if ($LASTEXITCODE -ne 0) { throw '无法建立独立 Python 环境' }
}
& $kbPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，尚未修改钩子' }
& $kbPython -B -X utf8 (Join-Path $PSScriptRoot 'src\main.py') --default-root (Get-Location).Path install @args
exit $LASTEXITCODE
