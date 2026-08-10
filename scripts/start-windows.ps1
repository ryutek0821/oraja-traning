param(
    [Parameter(Mandatory = $true)]
    [string]$ScoreDb,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = "$env:LOCALAPPDATA\oraja-training",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$pointer = Join-Path $DataRoot "active-release.txt"
if (-not (Test-Path $pointer -PathType Leaf)) {
    throw "No active export. Run scripts\activate-release.ps1 first."
}
$exportDir = (Get-Content $pointer -Raw).Trim()
if (-not (Test-Path $exportDir -PathType Container)) {
    throw "Active export does not exist: $exportDir"
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) {
    throw "Python environment is missing. Create .venv with Python 3.11+ first."
}
& $python -m oraja_training.cli serve `
    --export-dir $exportDir `
    --score-db $ScoreDb `
    --host 127.0.0.1 `
    --port $Port
