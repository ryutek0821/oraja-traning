from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-progress-server-launch-agent.sh"


def test_progress_server_launch_agent_installer_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", INSTALLER], check=True)
    result = subprocess.run(
        ["bash", INSTALLER, "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--uninstall" in result.stdout


def test_progress_server_launch_agent_is_private_and_persistent() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert '"KeepAlive": True' in source
    assert '"RunAtLoad": True' in source
    assert '"ThrottleInterval": 30' in source
    assert '"Umask": "077"' in source
    assert '"WorkingDirectory": project_root' in source
    assert '"--progress-token-file",' in source
    assert 'token_file,' in source
    assert 'Path(value).is_absolute()' in source
    assert 'cat "$TOKEN_FILE"' not in source


def test_progress_server_launch_agent_preserves_the_submitted_job_arguments() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python' in source
    assert 'EXPORT_DIR:-$PROJECT_ROOT/export/current' in source
    assert 'PROGRESS_STATE:-$PROJECT_ROOT/runtime/progress.json' in source
    assert 'TOKEN_FILE:-$PROJECT_ROOT/runtime/progress-token.txt' in source
    assert '"-m",' in source
    assert '"oraja_training.cli",' in source
    assert '"serve",' in source
    assert '"--export-dir",' in source
    assert '"--host",' in source
    assert '"--port",' in source
    assert '"--progress-state",' in source
    assert '"--score-db",' not in source


def test_progress_server_launch_agent_fails_closed_on_host_and_job_identity() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert '"$TAILSCALE_BIN" ip -4' in source
    assert '"$TAILSCALE_BIN" ip -6' in source
    assert "--host does not match this Mac's tailscale ip -4/-6 output" in source
    assert 'type = Submitted' in source
    assert 'job_is_ours "$existing_description"' in source
    assert 'refusing to replace an unrecognized job' in source
    assert '/bin/launchctl bootout "$SERVICE_TARGET"' in source
    assert '/bin/launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_PATH"' in source
    assert '/bin/rm -f "$PLIST_PATH"' in source
    assert "launchctl remove" not in source
