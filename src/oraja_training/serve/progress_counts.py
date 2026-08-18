"""Shared Asia/Tokyo progress-day and read-only score count handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


JUDGEMENT_COLUMNS = (
    "epg", "lpg", "egr", "lgr", "egd", "lgd", "ebd", "lbd", "epr", "lpr"
)
PROGRESS_TIMEZONE_NAME = "Asia/Tokyo"
PROGRESS_TIMEZONE = ZoneInfo(PROGRESS_TIMEZONE_NAME)


@dataclass(frozen=True, slots=True)
class ProgressCounts:
    """Latest cumulative judgement count and the safely known current-day delta."""

    current_judged: int
    today_judged: int | None


def progress_date(timestamp: float | int) -> date:
    """Return the shared progress date for an epoch timestamp."""

    return datetime.fromtimestamp(timestamp, tz=PROGRESS_TIMEZONE).date()


def read_progress_counts(
    score_db: str | Path,
    *,
    now: float | None = None,
) -> ProgressCounts:
    """Read cumulative and current-day counts without modifying ``score.db``."""

    path = Path(score_db).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 100")
        columns = ", ".join(f'"{name}"' for name in JUDGEMENT_COLUMNS)
        rows = connection.execute(
            f'SELECT "date", {columns} FROM player ORDER BY "date" DESC LIMIT 2'
        ).fetchall()
        if not rows:
            raise RuntimeError("player table is empty")
        totals = [sum(max(0, int(value or 0)) for value in row[1:]) for row in rows]
        current_day = progress_date(time.time() if now is None else now)
        latest_day = progress_date(int(rows[0][0]))
        if latest_day < current_day:
            today = 0
        elif latest_day == current_day and len(totals) == 2:
            today = max(0, totals[0] - totals[1])
        else:
            # A first-ever or future-dated cumulative row has no safe daily baseline.
            today = None
        return ProgressCounts(current_judged=totals[0], today_judged=today)
    finally:
        connection.close()
