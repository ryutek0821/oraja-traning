from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from oraja_training.serve.app import COCKPIT_HTML, TrainingHTTPServer, TrainingRequestHandler


JUDGES = ("epg", "lpg", "egr", "lgr", "egd", "lgd", "ebd", "lbd", "epr", "lpr")


def _score_db(path: Path, values: tuple[int, ...]) -> None:
    connection = sqlite3.connect(path)
    columns = ", ".join(f"{name} INTEGER" for name in JUDGES)
    connection.execute(f"CREATE TABLE player (date INTEGER PRIMARY KEY, {columns})")
    placeholders = ", ".join("?" for _ in range(11))
    connection.execute(f"INSERT INTO player VALUES ({placeholders})", (1, *values))
    connection.commit()
    connection.close()


def _status(export_dir: Path, score_db: Path | None) -> dict[str, object]:
    # Build without binding a socket: the test sandbox intentionally forbids it.
    server = object.__new__(TrainingHTTPServer)
    server.export_dir = export_dir.resolve()
    server.score_db = score_db.resolve() if score_db else None
    server.target_judged = 100_000
    server.last_current_judged = None
    return server.status()


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


def test_status_is_stale_when_database_or_manifest_is_missing(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    payload = _status(export, tmp_path / "missing.db")
    assert payload["stale"] is True
    assert payload["live_judged"] == 0
    assert payload["message"]


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
