param(
    [string]$DbDir = "",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = "$env:LOCALAPPDATA\oraja-training",
    [string]$TaskName = "OrajaTrainingCollector",
    [switch]$Uninstall,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    return
}
if (-not $DbDir) {
    throw "DbDir is required when installing the collector task."
}
$resolvedProjectRoot = (Resolve-Path $ProjectRoot).Path
$launcher = (Resolve-Path (Join-Path $resolvedProjectRoot "scripts\start-collector-windows.ps1")).Path
$resolvedDbDir = (Resolve-Path $DbDir).Path
$resolvedDataRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataRoot)
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -DbDir `"$resolvedDbDir`" -ProjectRoot `"$resolvedProjectRoot`" -DataRoot `"$resolvedDataRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}
Get-ScheduledTask -TaskName $TaskName
