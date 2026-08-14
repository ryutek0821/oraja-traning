param(
    [Parameter(Mandatory = $true)]
    [string]$DbDir,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = "$env:LOCALAPPDATA\oraja-training",
    [string]$TaskName = "OrajaTrainingCollector",
    [switch]$Uninstall,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    return
}
$launcher = (Resolve-Path (Join-Path $ProjectRoot "scripts\start-collector-windows.ps1")).Path
$resolvedDbDir = (Resolve-Path $DbDir).Path
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcher`" -DbDir `"$resolvedDbDir`" -ProjectRoot `"$ProjectRoot`" -DataRoot `"$DataRoot`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
}
Get-ScheduledTask -TaskName $TaskName
