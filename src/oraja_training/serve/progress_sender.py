"""Send read-only beatoraja progress snapshots to a Tailnet server."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .progress_counts import ProgressCounts, read_progress_counts


def send_progress_snapshot(
    url: str,
    token: str,
    current_judged: int,
    today_judged: int | None = None,
    *,
    source_id: str = "RYU-DESKTOP2",
    observed_at: int | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    """POST one already-read cumulative snapshot."""

    if type(current_judged) is not int or current_judged < 0:
        raise ValueError("current_judged must be non-negative")
    if (
        today_judged is not None
        and (
            type(today_judged) is not int
            or today_judged < 0
            or today_judged > current_judged
        )
    ):
        raise ValueError("today_judged must be a valid cumulative subset")
    if not token:
        raise ValueError("progress token is empty")
    return _post_progress_snapshot(
        url,
        token,
        current_judged,
        today_judged,
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
    timestamp = int(time.time()) if observed_at is None else int(observed_at)
    counts = read_progress_counts(score_db, now=timestamp)
    return send_progress_snapshot(
        url,
        token,
        counts.current_judged,
        counts.today_judged,
        source_id=source_id,
        observed_at=timestamp,
        timeout=timeout,
    )


def _post_progress_snapshot(
    url: str,
    token: str,
    current_judged: int,
    today_judged: int | None,
    *,
    source_id: str,
    observed_at: int,
    timeout: float,
) -> dict[str, object]:
    payload = json.dumps(
        {
            "source_id": source_id,
            "current_judged": current_judged,
            "today_judged": today_judged,
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
    last_counts: ProgressCounts | None = None
    last_sent = 0.0
    while True:
        try:
            counts = read_progress_counts(score_db)
            now = time.monotonic()
            if counts != last_counts or now - last_sent >= heartbeat:
                send_progress_snapshot(
                    url,
                    token,
                    counts.current_judged,
                    counts.today_judged,
                    source_id=source_id,
                )
                last_counts = counts
                last_sent = now
        except (OSError, RuntimeError, sqlite3.Error, URLError, ValueError) as error:
            print(f"progress send failed; retrying: {error}", file=sys.stderr, flush=True)
        time.sleep(max(0.5, poll_interval))
