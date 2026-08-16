param(
    [string]$ScoreDb = "",
    [string]$ServerUrl = "",
    [string]$TokenFile = "",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigFile = "$env:LOCALAPPDATA\oraja-training\progress-monitor.json",
    [string]$SourceId = "RYU-DESKTOP2",
    [double]$PollInterval = 5,
    [double]$Heartbeat = 30
)

$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$pythonArgs = @()
if (-not (Test-Path $python -PathType Leaf)) {
    $launcher = Get-Command pyw.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    }
    if ($null -eq $launcher) {
        throw "Python 3.11+ is missing."
    }
    $python = $launcher.Source
    $pythonArgs = @("-3")
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$arguments = @(
    "-m", "oraja_training.progress_sender_cli",
    "--monitor",
    "--config", $ConfigFile
)
if ($PSBoundParameters.ContainsKey("SourceId")) {
    $arguments += @("--source-id", $SourceId)
}
if ($PSBoundParameters.ContainsKey("PollInterval")) {
    $arguments += @("--poll-interval", "$PollInterval")
}
if ($PSBoundParameters.ContainsKey("Heartbeat")) {
    $arguments += @("--heartbeat", "$Heartbeat")
}
if ($ScoreDb) {
    $arguments += @("--score-db", $ScoreDb)
}
if ($ServerUrl) {
    $arguments += @("--url", $ServerUrl)
}
if ($TokenFile) {
    $arguments += @("--token-file", $TokenFile)
}

$dataRoot = Split-Path -Parent $ConfigFile
$logDir = Join-Path $dataRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "progress-monitor.log"
$mutex = [Threading.Mutex]::new($false, "Local\OrajaTrainingProgressMonitor")
if (-not $mutex.WaitOne(0)) {
    "$(Get-Date -Format o) progress monitor is already running" |
        Out-File -FilePath $logPath -Append -Encoding utf8
    exit 0
}
try {
    & $python @pythonArgs @arguments
    if ($LASTEXITCODE -ne 0) {
        "$(Get-Date -Format o) progress monitor exited with code $LASTEXITCODE" |
            Out-File -FilePath $logPath -Append -Encoding utf8
    }
    exit $LASTEXITCODE
}
catch {
    "$(Get-Date -Format o) $($_.Exception.Message)" |
        Out-File -FilePath $logPath -Append -Encoding utf8
    throw
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
