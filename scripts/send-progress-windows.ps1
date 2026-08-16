param(
    [Parameter(Mandatory = $true)]
    [string]$ScoreDb,
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TokenFile = "$env:LOCALAPPDATA\oraja-training\progress-token.txt",
    [string]$SourceId = "RYU-DESKTOP2",
    [double]$PollInterval = 5,
    [double]$Heartbeat = 30
)

$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$pythonArgs = @()
if (-not (Test-Path $python -PathType Leaf)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw "Python 3.11+ is missing."
    }
    $python = $launcher.Source
    $pythonArgs = @("-3")
}
if (-not (Test-Path $ScoreDb -PathType Leaf)) {
    throw "score.db is missing: $ScoreDb"
}
if (-not (Test-Path $TokenFile -PathType Leaf)) {
    throw "Progress token is missing: $TokenFile"
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& $python @pythonArgs -m oraja_training.progress_sender_cli `
    --score-db $ScoreDb `
    --url $ServerUrl `
    --token-file $TokenFile `
    --source-id $SourceId `
    --poll-interval $PollInterval `
    --heartbeat $Heartbeat `
    --daemon
