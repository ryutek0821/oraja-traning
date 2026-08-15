from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_collector_launcher_uses_owned_database_and_daemon() -> None:
    source = (ROOT / "scripts" / "start-collector-windows.ps1").read_text()
    assert 'Join-Path $DataRoot "assistant.db"' in source
    assert "--daemon" in source
    assert "--db-dir $resolvedDbDir" in source
    assert "Local\\OrajaTrainingCollector" in source
    assert "Tee-Object -FilePath $logPath -Append" in source


def test_windows_task_is_user_scoped_restartable_and_exactly_removable() -> None:
    source = (ROOT / "scripts" / "install-collector-task.ps1").read_text()
    assert 'TaskName = "OrajaTrainingCollector"' in source
    assert "-AtLogOn" in source
    assert "-RunLevel Limited" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-RestartCount 3" in source
    assert "Unregister-ScheduledTask -TaskName $TaskName" in source


def test_progress_monitor_launcher_keeps_token_out_of_arguments_by_default() -> None:
    source = (ROOT / "scripts" / "start-progress-monitor-windows.ps1").read_text()
    assert "--monitor" in source
    assert "--config" in source
    assert 'if ($ScoreDb)' in source
    assert 'if ($TokenFile)' in source
    assert '$PSBoundParameters.ContainsKey("SourceId")' in source
    assert '$PSBoundParameters.ContainsKey("PollInterval")' in source
    assert '$PSBoundParameters.ContainsKey("Heartbeat")' in source
    assert "Local\\OrajaTrainingProgressMonitor" in source
    assert "progress-monitor.log" in source
    assert "private-token" not in source


def test_progress_monitor_task_runs_only_in_an_interactive_logon() -> None:
    source = (ROOT / "scripts" / "install-progress-monitor-task.ps1").read_text()
    assert 'TaskName = "OrajaTrainingProgressMonitor"' in source
    assert "-AtLogOn" in source
    assert "-LogonType Interactive" in source
    assert "InteractiveToken" in source
    assert "-RunLevel Limited" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-RestartCount 3" in source
    assert '$PSBoundParameters.ContainsKey("SourceId")' in source
    assert "$resolvedProjectRoot = (Resolve-Path $ProjectRoot).Path" in source
    assert "GetUnresolvedProviderPathFromPSPath($ConfigFile)" in source
    assert "Unregister-ScheduledTask -TaskName $TaskName" in source
