"""Minimal CLI for Windows progress sending without collector dependencies."""

from __future__ import annotations

import argparse
from pathlib import Path

from oraja_training.serve.progress_monitor import run_progress_monitor
from oraja_training.serve.progress_sender import run_sender, send_progress


def main() -> int:
    parser = argparse.ArgumentParser(prog="oraja-progress-sender")
    parser.add_argument("--score-db", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--source-id")
    parser.add_argument("--poll-interval", type=float)
    parser.add_argument("--heartbeat", type=float)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.monitor:
        return run_progress_monitor(
            config_path=args.config,
            score_db=args.score_db,
            server_url=args.url,
            token_file=args.token_file,
            source_id=args.source_id,
            poll_interval=args.poll_interval,
            heartbeat=args.heartbeat,
        )
    missing = [
        option
        for option, value in (
            ("--score-db", args.score_db),
            ("--url", args.url),
            ("--token-file", args.token_file),
        )
        if value is None
    ]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("progress token file is empty")
    source_id = args.source_id or "RYU-DESKTOP2"
    poll_interval = 5.0 if args.poll_interval is None else args.poll_interval
    heartbeat = 30.0 if args.heartbeat is None else args.heartbeat
    if args.daemon:
        run_sender(
            args.score_db,
            args.url,
            token,
            source_id=source_id,
            poll_interval=poll_interval,
            heartbeat=heartbeat,
        )
    else:
        send_progress(args.score_db, args.url, token, source_id=source_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
