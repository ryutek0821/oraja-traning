from __future__ import annotations

import shutil
import sqlite3

from oraja_training.collect import snapshot


def test_daily_snapshot_baseline_and_idempotency(player_db_dir, tmp_path) -> None:
    score = tmp_path / "score-copy.db"
    log = tmp_path / "scoredatalog-copy.db"
    shutil.copy2(player_db_dir / "score.db", score)
    shutil.copy2(player_db_dir / "scoredatalog.db", log)
    assistant = tmp_path / "assistant.db"

    first = snapshot.run(score, log, assistant)
    assert first.cumulative_playcount == 507
    assert first.baseline_judged == 1_080_762
    assert first.new_play_rows == 0
    assert first.effective_date == "2026-08-08"

    again = snapshot.run(score, log, assistant)
    assert again.idempotent
    assert again.import_id == first.import_id

    conn = sqlite3.connect(assistant)
    try:
        assert conn.execute(
            "SELECT delta_judged FROM player_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()[0] == 67_546
        assert conn.execute("SELECT count(*) FROM score_state").fetchone()[0] == 933
    finally:
        conn.close()


def test_daily_snapshot_records_latest_and_lost_events(player_db_dir, tmp_path) -> None:
    score = tmp_path / "score-copy.db"
    log = tmp_path / "scoredatalog-copy.db"
    shutil.copy2(player_db_dir / "score.db", score)
    shutil.copy2(player_db_dir / "scoredatalog.db", log)
    assistant = tmp_path / "assistant.db"
    snapshot.run(score, log, assistant)

    conn = sqlite3.connect(log)
    key = conn.execute(
        "SELECT sha256, mode, playcount FROM scoredatalog ORDER BY sha256 LIMIT 1"
    ).fetchone()
    conn.execute(
        "UPDATE scoredatalog SET playcount = playcount + 3, date = date + 60 "
        "WHERE sha256 = ? AND mode = ?",
        key[:2],
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(score)
    conn.execute(
        "UPDATE score SET playcount = playcount + 3, date = date + 60 "
        "WHERE sha256 = ? AND mode = ?",
        key[:2],
    )
    conn.execute(
        "UPDATE player SET playcount = playcount + 3, epg = epg + 300 "
        "WHERE date = (SELECT max(date) FROM player)"
    )
    conn.commit()
    conn.close()

    result = snapshot.run(score, log, assistant)
    assert result.new_play_rows == 1
    assert result.lost_events == 2

    conn = sqlite3.connect(assistant)
    try:
        assert conn.execute(
            "SELECT lost_events FROM plays WHERE source='daily_snapshot'"
        ).fetchone()[0] == 2
    finally:
        conn.close()
