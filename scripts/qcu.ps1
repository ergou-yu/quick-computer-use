# Windows entrypoint, with the same resident daemon and fast routing as qcu.
$ErrorActionPreference = 'Stop'
$qcuRoot = Split-Path -Parent $PSScriptRoot
$qcuPython = Join-Path $qcuRoot '.venv\Scripts\python.exe'
if ($env:QCU_PYTHON) {
    $qcuPython = $env:QCU_PYTHON
} elseif (-not (Test-Path -LiteralPath $qcuPython)) {
    $qcuPython = (Get-Command python -ErrorAction Stop).Source
}
if (-not $env:QCU_PYTHON) {
    $qcuWindowless = Join-Path (Split-Path -Parent $qcuPython) 'pythonw.exe'
    if (Test-Path -LiteralPath $qcuWindowless) { $qcuPython = $qcuWindowless }
}
# Windows PowerShell 5.1 defaults to ASCII for native stdin pipelines.
$qcuPipelineEncoding = $global:OutputEncoding
$global:OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$qcuConsoleEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = $global:OutputEncoding
$qcuExitCode = 1
Push-Location -LiteralPath $qcuRoot
try {
    if ($args[0] -in @('act', 'batch') -and $args[1] -eq '-') {
        $input | & $qcuPython -X utf8 -m qcu @args
    } else {
        & $qcuPython -X utf8 -m qcu @args
    }
    $qcuExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    [Console]::OutputEncoding = $qcuConsoleEncoding
    $global:OutputEncoding = $qcuPipelineEncoding
}
exit $qcuExitCode
