param(
    [string]$ScoreDb = "",
    [string]$ServerUrl = "",
    [string]$TokenFile = "",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigFile = "$env:LOCALAPPDATA\oraja-training\progress-monitor.json",
    [string]$SourceId = "RYU-DESKTOP2",
    [string]$TaskName = "OrajaTrainingProgressMonitor",
    [switch]$Uninstall,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    return
}

$resolvedProjectRoot = (Resolve-Path $ProjectRoot).Path
$resolvedConfigFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigFile)
$launcher = (Resolve-Path (Join-Path $resolvedProjectRoot "scripts\start-progress-monitor-windows.ps1")).Path
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`" -ProjectRoot `"$resolvedProjectRoot`" -ConfigFile `"$resolvedConfigFile`""
if ($PSBoundParameters.ContainsKey("SourceId")) {
    $arguments += " -SourceId `"$SourceId`""
}
if ($ScoreDb) {
    $resolvedScoreDb = (Resolve-Path $ScoreDb).Path
    $arguments += " -ScoreDb `"$resolvedScoreDb`""
}
if ($ServerUrl) {
    $arguments += " -ServerUrl `"$ServerUrl`""
}
if ($TokenFile) {
    $resolvedTokenFile = (Resolve-Path $TokenFile).Path
    $arguments += " -TokenFile `"$resolvedTokenFile`""
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$userId = "$env:USERDOMAIN\$env:USERNAME"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
# PowerShell calls Task Scheduler's InteractiveToken logon type "Interactive".
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}
Get-ScheduledTask -TaskName $TaskName
