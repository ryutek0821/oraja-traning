from pathlib import Path
import plistlib
import subprocess
import sys


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


def test_progress_server_launch_agent_is_private_and_persistent(
    tmp_path: Path, monkeypatch
) -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert '"KeepAlive": True' in source
    assert '"RunAtLoad": True' in source
    assert '"ThrottleInterval": 30' in source
    assert '"WorkingDirectory": project_root' in source
    assert '"--progress-token-file",' in source
    assert 'token_file,' in source
    assert 'Path(value).is_absolute()' in source
    assert 'cat "$TOKEN_FILE"' not in source

    marker = "<<'PY'\n"
    _, separator, tail = source.partition(marker)
    assert separator
    renderer, separator, _ = tail.partition("\nPY\n")
    assert separator

    output = tmp_path / "agent.plist"
    token_file = tmp_path / "progress-token.txt"
    token_file.write_text("private-token", encoding="utf-8")
    arguments = [
        "embedded-plist-renderer",
        str(output),
        sys.executable,
        str(ROOT),
        str(ROOT / "export" / "current"),
        "100.118.150.23",
        "8765",
        str(tmp_path / "progress.json"),
        str(token_file),
        str(tmp_path / "progress-server.log"),
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    exec(compile(renderer, str(INSTALLER), "exec"), {"__name__": "__main__"})

    raw = output.read_bytes()
    payload = plistlib.loads(raw)
    assert type(payload["Umask"]) is int
    assert payload["Umask"] == 0o077
    assert b"<integer>63</integer>" in raw
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 30
    assert payload["ProgramArguments"] == [
        sys.executable,
        "-m",
        "oraja_training.cli",
        "serve",
        "--export-dir",
        str(ROOT / "export" / "current"),
        "--host",
        "100.118.150.23",
        "--port",
        "8765",
        "--progress-state",
        str(tmp_path / "progress.json"),
        "--progress-token-file",
        str(token_file),
    ]
    assert b"private-token" not in raw


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
