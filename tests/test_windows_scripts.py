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
