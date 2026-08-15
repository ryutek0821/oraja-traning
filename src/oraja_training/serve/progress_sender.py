"""Send read-only beatoraja progress snapshots to a Tailnet server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .app import _read_latest_judged


@dataclass(frozen=True, slots=True)
class ProgressCounts:
    """Latest cumulative judgement count and the newest daily delta."""

    current_judged: int
    today_judged: int | None


def read_progress_counts(
    score_db: str | Path,
    *,
    now: float | None = None,
) -> ProgressCounts:
    """Read cumulative and latest-day counts without modifying ``score.db``."""

    path = Path(score_db).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 100")
        columns = ", ".join(
            f'"{name}"'
            for name in (
                "epg", "lpg", "egr", "lgr", "egd",
                "lgd", "ebd", "lbd", "epr", "lpr",
            )
        )
        rows = connection.execute(
            f'SELECT "date", {columns} FROM player ORDER BY "date" DESC LIMIT 2'
        ).fetchall()
        if not rows:
            raise RuntimeError("player table is empty")
        totals = [sum(max(0, int(value or 0)) for value in row[1:]) for row in rows]
        current_day = datetime.fromtimestamp(time.time() if now is None else now).date()
        latest_day = datetime.fromtimestamp(int(rows[0][0])).date()
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


def send_progress_snapshot(
    url: str,
    token: str,
    current_judged: int,
    *,
    source_id: str = "RYU-DESKTOP2",
    observed_at: int | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """POST one already-read cumulative snapshot."""

    if current_judged < 0:
        raise ValueError("current_judged must be non-negative")
    if not token:
        raise ValueError("progress token is empty")
    return _post_progress_snapshot(
        url,
        token,
        current_judged,
        source_id=source_id,
        observed_at=int(time.time()) if observed_at is None else int(observed_at),
        timeout=timeout,
    )


def send_progress(
    score_db: str | Path,
    url: str,
    token: str,
    *,
    source_id: str = "RYU-DESKTOP2",
    observed_at: int | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    return send_progress_snapshot(
        url,
        token,
        _read_latest_judged(Path(score_db)),
        source_id=source_id,
        observed_at=observed_at,
        timeout=timeout,
    )


def _post_progress_snapshot(
    url: str,
    token: str,
    current_judged: int,
    *,
    source_id: str,
    observed_at: int,
    timeout: float,
) -> dict[str, object]:
    payload = json.dumps(
        {
            "source_id": source_id,
            "current_judged": current_judged,
            "observed_at": observed_at,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        url.rstrip("/") + "/api/progress",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run_sender(
    score_db: str | Path,
    url: str,
    token: str,
    *,
    source_id: str = "RYU-DESKTOP2",
    poll_interval: float = 5.0,
    heartbeat: float = 30.0,
) -> None:
    last_judged: int | None = None
    last_sent = 0.0
    while True:
        try:
            judged = _read_latest_judged(Path(score_db))
            now = time.monotonic()
            if judged != last_judged or now - last_sent >= heartbeat:
                send_progress_snapshot(
                    url, token, judged, source_id=source_id
                )
                last_judged = judged
                last_sent = now
        except (OSError, RuntimeError, sqlite3.Error, URLError, ValueError) as error:
            print(f"progress send failed; retrying: {error}", file=sys.stderr, flush=True)
        time.sleep(max(0.5, poll_interval))
