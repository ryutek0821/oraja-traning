from __future__ import annotations

from io import BytesIO
import json
from email.message import Message
from http import HTTPStatus
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from oraja_training.serve.app import (
    COCKPIT_HTML,
    TrainingHTTPServer,
    TrainingRequestHandler,
    _validate_progress_bind,
)


JUDGES = ("epg", "lpg", "egr", "lgr", "egd", "lgd", "ebd", "lbd", "epr", "lpr")


def _score_db(path: Path, values: tuple[int, ...]) -> None:
    connection = sqlite3.connect(path)
    columns = ", ".join(f"{name} INTEGER" for name in JUDGES)
    connection.execute(f"CREATE TABLE player (date INTEGER PRIMARY KEY, {columns})")
    placeholders = ", ".join("?" for _ in range(11))
    connection.execute(f"INSERT INTO player VALUES ({placeholders})", (1, *values))
    connection.commit()
    connection.close()


def _server(export_dir: Path, score_db: Path | None = None) -> TrainingHTTPServer:
    # Build without binding a socket: the test sandbox intentionally forbids it.
    server = object.__new__(TrainingHTTPServer)
    server.export_dir = export_dir.resolve()
    server.score_db = score_db.resolve() if score_db else None
    server.target_judged = 100_000
    server.last_current_judged = None
    server.progress_state = None
    server.progress_token = None
    server.progress_source_id = "RYU-DESKTOP2"
    server.progress_stale_after = 90
    import threading
    server.progress_lock = threading.Lock()
    return server


def _status(export_dir: Path, score_db: Path | None) -> dict[str, object]:
    return _server(export_dir, score_db).status()


def _dispatch(export_dir: Path, path: str) -> tuple[int, bytes]:
    server = SimpleNamespace(export_dir=export_dir.resolve(), status=lambda: {})
    handler = object.__new__(TrainingRequestHandler)
    handler.server = server
    handler.path = path
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.wfile = BytesIO()
    handler._dispatch(send_body=True)
    response = handler.wfile.getvalue()
    head, body = response.split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), body


def _post(server: TrainingHTTPServer, token: str, payload: bytes) -> tuple[int, bytes]:
    handler = object.__new__(TrainingRequestHandler)
    handler.server = server
    handler.path = "/api/progress"
    handler.command = "POST"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /api/progress HTTP/1.1"
    handler.rfile = BytesIO(payload)
    handler.wfile = BytesIO()
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Length"] = str(len(payload))
    handler.headers = headers
    handler.do_POST()
    response = handler.wfile.getvalue()
    head, body = response.split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), body


def test_serves_only_allowlisted_exports_and_cockpit(tmp_path: Path) -> None:
    export = tmp_path / "export"
    (export / "table" / "today").mkdir(parents=True)
    (export / "table" / "today" / "header.json").write_text(
        '{"name":"today"}', encoding="utf-8"
    )
    (export / "secret.db").write_bytes(b"not public")

    status, body = _dispatch(export, "/")
    assert status == 200
    assert "10万打鍵 COCKPIT" in body.decode()

    status, body = _dispatch(export, "/table/today/header.json")
    assert status == 200
    assert json.loads(body) == {"name": "today"}

    assert _dispatch(export, "/secret.db")[0] == 404
    assert _dispatch(export, "/table/today/../../secret.db")[0] == 404
    assert _dispatch(export, "/table/recommend/header.json")[0] == 404


def test_status_reports_live_player_delta(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text(
        json.dumps({"baseline_judged": 1_000}), encoding="utf-8"
    )
    score = tmp_path / "score.db"
    _score_db(score, (10_100,) * 10)

    payload = _status(export, score)
    assert payload["current_judged"] == 101_000
    assert payload["live_judged"] == 100_000
    assert payload["remaining_judged"] == 0
    assert payload["progress"] == 1.0
    assert payload["complete"] is True
    assert payload["stale"] is False


def test_progress_receiver_rejects_wildcard_and_lan_bindings() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.10", "training.example.com"):
        try:
            _validate_progress_bind(host, "configured")
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe progress bind was accepted: {host}")
    for host in ("127.0.0.1", "::1", "localhost", "100.118.150.23"):
        _validate_progress_bind(host, "configured")


def test_status_is_stale_when_database_or_manifest_is_missing(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    payload = _status(export, tmp_path / "missing.db")
    assert payload["stale"] is True
    assert payload["live_judged"] == 0
    assert payload["message"]


def test_remote_progress_is_authenticated_idempotent_and_used_by_status(
    tmp_path: Path, monkeypatch
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text(
        json.dumps({"baseline_judged": 1_000}), encoding="utf-8"
    )
    server = _server(export)
    server.progress_state = tmp_path / "progress.json"
    server.progress_token = "secret-token"
    server.progress_stale_after = 90
    monkeypatch.setattr("oraja_training.serve.app.time.time", lambda: 2_000)
    payload = json.dumps(
        {"source_id": "RYU-DESKTOP2", "current_judged": 2_500, "observed_at": 2_000}
    ).encode()

    assert _post(server, "wrong", payload)[0] == HTTPStatus.UNAUTHORIZED
    assert _post(server, "secret-token", payload)[0] == HTTPStatus.OK
    assert _post(server, "secret-token", payload)[0] == HTTPStatus.OK
    status = server.status()
    assert status["live_judged"] == 1_500
    assert status["progress_source"] == "RYU-DESKTOP2"
    assert status["progress_received_at"] == 2_000
    assert status["stale"] is False

    backwards = json.dumps(
        {"source_id": "RYU-DESKTOP2", "current_judged": 2_499, "observed_at": 2_001}
    ).encode()
    assert _post(server, "secret-token", backwards)[0] == HTTPStatus.CONFLICT


def test_remote_progress_rejects_non_integer_and_unexpected_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    server = _server(export)
    server.progress_state = tmp_path / "progress.json"
    server.progress_token = "secret-token"
    monkeypatch.setattr("oraja_training.serve.app.time.time", lambda: 2_000)

    for payload in (
        {"source_id": "other", "current_judged": 1, "observed_at": 2_000},
        {"source_id": "RYU-DESKTOP2", "current_judged": "1", "observed_at": 2_000},
        {"source_id": "RYU-DESKTOP2", "current_judged": True, "observed_at": 2_000},
        {"source_id": "RYU-DESKTOP2", "current_judged": 1, "observed_at": 2_000.5},
        {
            "source_id": "RYU-DESKTOP2",
            "current_judged": 1,
            "observed_at": 2_000,
            "extra": "not allowed",
        },
    ):
        encoded = json.dumps(payload).encode()
        assert _post(server, "secret-token", encoded)[0] == HTTPStatus.BAD_REQUEST

    assert _post(server, "secret-token", b" " * 4_097)[0] == (
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    )


def test_idempotent_replay_does_not_refresh_an_old_observation(
    tmp_path: Path, monkeypatch
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text(
        '{"baseline_judged":1000}', encoding="utf-8"
    )
    server = _server(export)
    server.progress_state = tmp_path / "progress.json"
    server.progress_token = "secret-token"
    server.progress_stale_after = 90
    now = [2_000]
    monkeypatch.setattr("oraja_training.serve.app.time.time", lambda: now[0])
    payload = json.dumps(
        {"source_id": "RYU-DESKTOP2", "current_judged": 2_500, "observed_at": 2_000}
    ).encode()

    assert _post(server, "secret-token", payload)[0] == HTTPStatus.OK
    now[0] = 2_091
    assert _post(server, "secret-token", payload)[0] == HTTPStatus.OK
    saved = json.loads(server.progress_state.read_text(encoding="utf-8"))
    assert saved["received_at"] == 2_000
    assert server.status()["stale"] is True


def test_old_first_observation_is_immediately_stale(tmp_path: Path, monkeypatch) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text(
        '{"baseline_judged":1000}', encoding="utf-8"
    )
    server = _server(export)
    server.progress_state = tmp_path / "progress.json"
    server.progress_token = "secret-token"
    monkeypatch.setattr("oraja_training.serve.app.time.time", lambda: 2_000)
    payload = json.dumps(
        {"source_id": "RYU-DESKTOP2", "current_judged": 2_500, "observed_at": 1_000}
    ).encode()

    assert _post(server, "secret-token", payload)[0] == HTTPStatus.OK
    assert server.status()["stale"] is True


def test_remote_progress_becomes_stale(tmp_path: Path, monkeypatch) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text('{"baseline_judged":1000}', encoding="utf-8")
    server = _server(export)
    server.progress_state = tmp_path / "progress.json"
    server.progress_stale_after = 90
    server.progress_state.write_text(
        '{"source_id":"RYU-DESKTOP2","current_judged":2000,"observed_at":1000}',
        encoding="utf-8",
    )
    monkeypatch.setattr("oraja_training.serve.app.time.time", lambda: 1_091)
    assert server.status()["stale"] is True


def test_session_review_health_and_encoded_traversal(tmp_path: Path) -> None:
    export = tmp_path / "export"
    (export / "review").mkdir(parents=True)
    (export / "session.json").write_text('{"queue":[]}', encoding="utf-8")
    (export / "review" / "latest.json").write_text('{"day":"today"}', encoding="utf-8")

    assert json.loads(_dispatch(export, "/api/session")[1]) == {"queue": []}
    assert json.loads(_dispatch(export, "/api/review/latest")[1]) == {"day": "today"}
    assert json.loads(_dispatch(export, "/healthz")[1])["status"] == "ok"
    assert _dispatch(export, "/table/today/%2e%2e/%2e%2e/session.json")[0] == 404


def test_cockpit_has_required_accessibility_and_visual_tokens() -> None:
    assert "#0B1020" in COCKPIT_HTML
    assert "#6EE7F2" in COCKPIT_HTML
    assert "Bahnschrift" in COCKPIT_HTML
    assert ":focus-visible" in COCKPIT_HTML
    assert "prefers-reduced-motion" in COCKPIT_HTML
