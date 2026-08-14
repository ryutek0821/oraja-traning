param(
    [Parameter(Mandatory = $true)]
    [string]$DbDir,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = "$env:LOCALAPPDATA\oraja-training",
    [double]$PollInterval = 5.0
)

$ErrorActionPreference = "Stop"
$resolvedDbDir = (Resolve-Path $DbDir).Path
foreach ($name in @("score.db", "scoredatalog.db")) {
    if (-not (Test-Path (Join-Path $resolvedDbDir $name) -PathType Leaf)) {
        throw "Source database is missing: $name"
    }
}
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) {
    throw "Python environment is missing. Create .venv with Python 3.11+ first."
}
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$logDir = Join-Path $DataRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$assistantDb = Join-Path $DataRoot "assistant.db"
$logPath = Join-Path $logDir "collector.log"
$mutex = [Threading.Mutex]::new($false, "Local\OrajaTrainingCollector")
if (-not $mutex.WaitOne(0)) {
    Write-Output "collector is already running"
    exit 0
}
try {
    & $python -m oraja_training.cli collect `
        --db-dir $resolvedDbDir `
        --assistant-db $assistantDb `
        --poll-interval $PollInterval `
        --daemon 2>&1 | Tee-Object -FilePath $logPath -Append
    exit $LASTEXITCODE
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
