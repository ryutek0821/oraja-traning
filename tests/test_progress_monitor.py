from __future__ import annotations

from dataclasses import replace
import json
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from oraja_training.serve.progress_monitor import (
    MonitorSettings,
    ProgressMonitorWorker,
    load_monitor_settings,
    save_monitor_settings,
)
from oraja_training.serve.progress_sender import ProgressCounts, read_progress_counts


_JUDGEMENTS = ("epg", "lpg", "egr", "lgr", "egd", "lgd", "ebd", "lbd", "epr", "lpr")
ROOT = Path(__file__).resolve().parents[1]


def _score_db(path: Path, *, latest: datetime | None = None) -> datetime:
    latest = latest or datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    previous = latest - timedelta(days=1)
    connection = sqlite3.connect(path)
    columns = ", ".join(f'"{name}" INTEGER' for name in _JUDGEMENTS)
    connection.execute(f'CREATE TABLE player ("date" INTEGER PRIMARY KEY, {columns})')
    placeholders = ", ".join("?" for _ in range(11))
    connection.executemany(
        f"INSERT INTO player VALUES ({placeholders})",
        [
            (int(previous.timestamp()), *(1 for _ in _JUDGEMENTS)),
            (int(latest.timestamp()), *(3 for _ in _JUDGEMENTS)),
        ],
    )
    connection.commit()
    connection.close()
    return latest


def _settings(tmp_path: Path) -> MonitorSettings:
    score = tmp_path / "score.db"
    score.touch()
    token = tmp_path / "progress-token.txt"
    token.write_text("private-token\n", encoding="utf-8")
    return MonitorSettings(
        score_db=str(score),
        server_url="http://100.64.0.1:8765/",
        token_file=str(token),
    )


def test_reads_cumulative_and_latest_daily_delta_read_only(tmp_path: Path) -> None:
    score = tmp_path / "score.db"
    latest = _score_db(score)

    counts = read_progress_counts(score, now=latest.timestamp())

    assert counts == ProgressCounts(current_judged=30, today_judged=20)
    connection = sqlite3.connect(score)
    assert connection.execute("SELECT count(*) FROM player").fetchone()[0] == 2
    connection.close()


def test_progress_count_excludes_empty_poor_columns(tmp_path: Path) -> None:
    score = tmp_path / "score.db"
    latest = _score_db(score)
    connection = sqlite3.connect(score)
    connection.execute('ALTER TABLE player ADD COLUMN "ems" INTEGER')
    connection.execute('ALTER TABLE player ADD COLUMN "lms" INTEGER')
    connection.execute('UPDATE player SET "ems" = 1000, "lms" = 2000')
    connection.commit()
    connection.close()

    counts = read_progress_counts(score, now=latest.timestamp())

    assert counts == ProgressCounts(current_judged=30, today_judged=20)


def test_daily_count_is_zero_when_latest_row_is_before_today(tmp_path: Path) -> None:
    score = tmp_path / "score.db"
    latest = _score_db(score)

    counts = read_progress_counts(
        score,
        now=(latest + timedelta(days=1)).timestamp(),
    )

    assert counts == ProgressCounts(current_judged=30, today_judged=0)


def test_daily_count_is_unknown_without_baseline_or_for_future_row(
    tmp_path: Path,
) -> None:
    score = tmp_path / "score.db"
    latest = _score_db(score)
    connection = sqlite3.connect(score)
    connection.execute('DELETE FROM player WHERE "date" < ?', (latest.timestamp(),))
    connection.commit()
    connection.close()

    assert read_progress_counts(score, now=latest.timestamp()).today_judged is None
    assert read_progress_counts(
        score,
        now=(latest - timedelta(days=1)).timestamp(),
    ).today_judged is None


def test_monitor_config_persists_token_path_but_not_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config = tmp_path / "private" / "monitor.json"

    assert save_monitor_settings(config, settings) == config.resolve()
    payload = config.read_text(encoding="utf-8")
    assert "private-token" not in payload
    assert json.loads(payload)["token_file"] == str(
        Path(settings.token_file).resolve()
    )
    assert load_monitor_settings(config) == settings.normalized()


def test_monitor_worker_sends_on_change_heartbeat_and_manual_request(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monotonic_now = [100.0]
    wall_now = [2_000.0]
    counts = [ProgressCounts(10, 4)]
    sent: list[tuple[int, int]] = []

    def sender(_url, _token, current, *, observed_at, **_kwargs):
        sent.append((current, observed_at))
        return {"status": "accepted"}

    worker = ProgressMonitorWorker(
        settings,
        lambda _snapshot: None,
        reader=lambda _path: counts[0],
        sender=sender,
        wall_clock=lambda: wall_now[0],
        monotonic=lambda: monotonic_now[0],
    )

    first = worker.poll_once()
    assert first.status == "LIVE"
    assert first.current_judged == 10
    assert sent == [(10, 2_000)]

    monotonic_now[0] += 5
    wall_now[0] += 5
    worker.poll_once()
    assert sent == [(10, 2_000)]

    counts[0] = ProgressCounts(12, 6)
    worker.poll_once()
    assert sent[-1] == (12, 2_005)

    monotonic_now[0] += settings.heartbeat
    wall_now[0] += settings.heartbeat
    worker.poll_once()
    assert sent[-1] == (12, 2_035)

    worker.poll_once(force=True)
    assert len(sent) == 4


def test_monitor_worker_survives_read_or_network_errors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def unavailable(_path):
        raise sqlite3.OperationalError("database is locked")

    worker = ProgressMonitorWorker(
        settings,
        lambda _snapshot: None,
        reader=unavailable,
    )
    snapshot = worker.poll_once()
    assert snapshot.status == "ERROR"
    assert snapshot.current_judged is None
    assert "locked" in (snapshot.error or "")


def test_monitor_reports_stale_if_heartbeat_exceeds_server_freshness(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings = replace(settings, heartbeat=120.0)
    monotonic_now = [100.0]
    wall_now = [2_000.0]
    worker = ProgressMonitorWorker(
        settings,
        lambda _snapshot: None,
        reader=lambda _path: ProgressCounts(10, 4),
        sender=lambda *_args, **_kwargs: {"status": "accepted"},
        wall_clock=lambda: wall_now[0],
        monotonic=lambda: monotonic_now[0],
    )
    assert worker.poll_once().status == "LIVE"

    monotonic_now[0] += 91
    wall_now[0] += 91

    assert worker.poll_once().status == "STALE"


def test_monitor_ui_keeps_all_issue_60_status_fields_and_controls() -> None:
    source = (
        ROOT / "src" / "oraja_training" / "serve" / "progress_monitor.py"
    ).read_text(encoding="utf-8")

    for required in (
        '"LIVE"',
        '"STALE"',
        '"ERROR"',
        'text="累積打鍵数"',
        'text="本日の打鍵数"',
        'text="最終送信"',
        'text="次回heartbeat"',
        'text="Mac URL"',
        'text="score.db"',
        'text="今すぐ送信"',
        'text="終了"',
        "filedialog.askopenfilename",
    ):
        assert required in source
