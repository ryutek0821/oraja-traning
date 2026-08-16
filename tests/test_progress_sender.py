from __future__ import annotations

from io import BytesIO
import json
import sqlite3

import pytest

from oraja_training.serve import progress_sender


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_send_progress_posts_cumulative_snapshot(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(progress_sender, "_read_latest_judged", lambda _path: 123_456)

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(b'{"status":"accepted"}')

    monkeypatch.setattr(progress_sender, "urlopen", open_request)
    result = progress_sender.send_progress(
        "score.db",
        "http://100.64.0.1:8765/",
        "secret-token",
        observed_at=2_000,
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url == "http://100.64.0.1:8765/api/progress"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert payload == {
        "source_id": "RYU-DESKTOP2",
        "current_judged": 123_456,
        "observed_at": 2_000,
    }
    assert result == {"status": "accepted"}


def test_daemon_retries_a_locked_database(monkeypatch, capsys) -> None:
    class StopDaemon(Exception):
        pass

    def stop_after_retry(_seconds: float) -> None:
        raise StopDaemon

    monkeypatch.setattr(
        progress_sender,
        "_read_latest_judged",
        lambda _path: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    monkeypatch.setattr(
        progress_sender.time,
        "sleep",
        stop_after_retry,
    )

    with pytest.raises(StopDaemon):
        progress_sender.run_sender("score.db", "http://example.test", "token")
    assert "database is locked" in capsys.readouterr().err


def test_daemon_heartbeats_when_the_counter_does_not_change(monkeypatch) -> None:
    monotonic = iter((0.0, 31.0))
    sent: list[int] = []
    sleeps = [0]
    monkeypatch.setattr(progress_sender, "_read_latest_judged", lambda _path: 42)
    monkeypatch.setattr(progress_sender.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(
        progress_sender,
        "send_progress_snapshot",
        lambda _url, _token, current, **_kwargs: sent.append(current),
    )

    def sleep(_seconds):
        sleeps[0] += 1
        if sleeps[0] == 2:
            raise StopIteration

    monkeypatch.setattr(progress_sender.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        progress_sender.run_sender("score.db", "http://example.test", "token")
    assert sent == [42, 42]
