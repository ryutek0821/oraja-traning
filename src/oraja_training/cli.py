"""Command-line interface for initial import and live collection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import time
from typing import Sequence

from oraja_training.collect import backfill, snapshot
from oraja_training.collect.poller import Poller, TickResult
from oraja_training.db import readers, store
from oraja_training.db.recommendation_adapter import SQLiteRecommendationRepository
from oraja_training.domain import ProfileContext
from oraja_training.features import build_all
from oraja_training.model import fit_latest
from oraja_training.plan import build_session, recommendation_output, write_export
from oraja_training.plan.experiment import (
    assign_session,
    build_candidate_sets,
    list_targets,
    report_experiment,
    resolve_targets,
    start_experiment,
)
from oraja_training.serve import create_progress_token, run_sender, send_progress, serve
from oraja_training.tables import fetch_table, resolve


DEFAULT_TABLES = (
    (
        "genocide",
        "https://nekokan.dyndns.info/~lobsak/genocide/insane.html",
    ),
    ("overjoy", "https://lr2.sakura.ne.jp/data/header.json"),
    ("satellite", "https://stellabms.xyz/sl/table.html"),
    ("stella", "https://stellabms.xyz/st/table.html"),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oraja-training")
    subcommands = parser.add_subparsers(dest="command", required=True)

    backfill_parser = subcommands.add_parser(
        "backfill", help="import a static player DB snapshot"
    )
    backfill_parser.add_argument("--db-dir", type=Path, required=True)
    backfill_parser.add_argument(
        "--assistant-db", type=Path, default=Path("assistant.db")
    )

    collect_parser = subcommands.add_parser(
        "collect", help="collect latest plays from a live player directory"
    )
    collect_parser.add_argument("--db-dir", type=Path, required=True)
    collect_parser.add_argument(
        "--assistant-db", type=Path, default=Path("assistant.db")
    )
    collect_parser.add_argument("--poll-interval", type=float, default=5.0)
    collect_parser.add_argument(
        "--daemon", action="store_true", help="continue polling until interrupted"
    )
    collect_parser.add_argument(
        "--force", action="store_true", help="scan once even when mtimes are unchanged"
    )

    initialize = subcommands.add_parser(
        "initialize", help="build the initial personal assistant database"
    )
    initialize.add_argument("--db-dir", type=Path, required=True)
    initialize.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))

    daily = subcommands.add_parser(
        "daily-update", help="ingest a submitted DB pair and generate tomorrow's menu"
    )
    daily.add_argument("--score-db", type=Path, required=True)
    daily.add_argument("--scoredatalog-db", type=Path, required=True)
    daily.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    daily.add_argument("--output-dir", type=Path, default=Path("export/current"))
    daily.add_argument("--menu-date")
    daily.add_argument("--readiness", choices=("normal", "tired"), default="normal")
    daily.add_argument("--profile-id", default="local-profile")
    daily.add_argument("--profile-name", default="Personal")
    daily.add_argument("--timezone", default="Asia/Tokyo")

    tables = subcommands.add_parser("tables", help="difficulty table operations")
    table_commands = tables.add_subparsers(dest="tables_command", required=True)
    refresh = table_commands.add_parser("refresh", help="fetch and match source tables")
    refresh.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    refresh.add_argument("--cache-dir", type=Path, default=Path(".cache/tables"))
    refresh.add_argument(
        "--table", action="append", default=[], metavar="ID=URL",
        help="source table page/header; defaults to GENOCIDE, Overjoy, Satellite and Stella",
    )

    features = subcommands.add_parser("features", help="songinfo feature operations")
    feature_commands = features.add_subparsers(dest="features_command", required=True)
    feature_build = feature_commands.add_parser("build", help="build chart features")
    feature_build.add_argument("--songinfo-db", type=Path, required=True)
    feature_build.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))

    menu = subcommands.add_parser("menu", help="generate a deterministic menu export")
    menu.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    menu.add_argument("--output-dir", type=Path, default=Path("export/current"))
    menu.add_argument("--date", default=date.today().isoformat())
    menu.add_argument("--readiness", choices=("normal", "tired"), default="normal")
    menu.add_argument("--profile-id", default="local-profile")
    menu.add_argument("--profile-name", default="Personal")
    menu.add_argument("--timezone", default="Asia/Tokyo")

    review = subcommands.add_parser("review", help="show latest daily training summary")
    review.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))

    experiment = subcommands.add_parser(
        "experiment", help="run the randomized coach/control self-experiment"
    )
    experiment_commands = experiment.add_subparsers(
        dest="experiment_command", required=True
    )
    experiment_start = experiment_commands.add_parser("start")
    experiment_start.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    experiment_start.add_argument("--name", required=True)
    experiment_start.add_argument("--seed", required=True)
    experiment_start.add_argument("--starts-at", type=int, default=None)
    experiment_start.add_argument("--days", type=int, default=14)
    experiment_start.add_argument("--min-samples-per-arm", type=int, default=20)

    experiment_assign = experiment_commands.add_parser(
        "assign",
        help="assign one arm and reserve a distinct transfer chart per interval",
    )
    experiment_assign.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    experiment_assign.add_argument("--experiment-id", type=int, required=True)
    experiment_assign.add_argument("--session-key", required=True)
    experiment_assign.add_argument("--session-at", type=int, default=None)
    experiment_assign.add_argument(
        "--candidates-json", type=Path,
        help="optional JSON candidate sets; defaults to the latest Daily Menu",
    )

    experiment_candidates = experiment_commands.add_parser(
        "candidates", help="show the auditable arm and eligible transfer pools"
    )
    experiment_candidates.add_argument(
        "--assistant-db", type=Path, default=Path("assistant.db")
    )
    experiment_candidates.add_argument("--experiment-id", type=int, required=True)
    experiment_candidates.add_argument("--session-key", required=True)

    experiment_resolve = experiment_commands.add_parser("resolve")
    experiment_resolve.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    experiment_resolve.add_argument("--experiment-id", type=int, required=True)
    experiment_resolve.add_argument("--now", type=int, default=None)

    experiment_targets = experiment_commands.add_parser(
        "targets", help="show exact chart modes, probabilities, and play windows"
    )
    experiment_targets.add_argument(
        "--assistant-db", type=Path, default=Path("assistant.db")
    )
    experiment_targets.add_argument("--experiment-id", type=int, required=True)
    experiment_targets.add_argument(
        "--status",
        choices=("pending", "resolved", "missing", "duplicate", "all"),
        default="pending",
    )

    experiment_report = experiment_commands.add_parser("report")
    experiment_report.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    experiment_report.add_argument("--experiment-id", type=int, required=True)

    server = subcommands.add_parser("serve", help="serve tables and training cockpit")
    server.add_argument("--export-dir", type=Path, default=Path("export/current"))
    server.add_argument("--score-db", type=Path)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--progress-state", type=Path)
    server.add_argument("--progress-token-file", type=Path)
    server.add_argument("--progress-source-id", default="RYU-DESKTOP2")
    server.add_argument("--progress-stale-after", type=int, default=90)

    sender = subcommands.add_parser(
        "progress-send", help="send live score progress to a training server"
    )
    sender.add_argument("--score-db", type=Path, required=True)
    sender.add_argument("--url", required=True)
    sender.add_argument("--token-file", type=Path, required=True)
    sender.add_argument("--source-id", default="RYU-DESKTOP2")
    sender.add_argument("--poll-interval", type=float, default=5.0)
    sender.add_argument("--heartbeat", type=float, default=30.0)
    sender.add_argument("--daemon", action="store_true")

    token = subcommands.add_parser(
        "progress-token-create", help="create a progress token file"
    )
    token.add_argument("--output", type=Path, required=True)
    return parser


def _render_tick(result: TickResult) -> None:
    if result.scanned or result.new_plays or result.lost_events:
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True), flush=True)


def _profile_context(args: argparse.Namespace) -> ProfileContext:
    return ProfileContext(
        profile_id=args.profile_id,
        display_name=args.profile_name,
        timezone=args.timezone,
    )


def _table_specs(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    if not values:
        return DEFAULT_TABLES
    specs: list[tuple[str, str]] = []
    for value in values:
        table_id, separator, url = value.partition("=")
        if not separator or not table_id.strip() or not url.strip():
            raise ValueError(f"invalid --table {value!r}; expected ID=URL")
        specs.append((table_id.strip(), url.strip()))
    return tuple(specs)


def _refresh_tables(
    assistant_db: Path, cache_dir: Path, specs: Sequence[tuple[str, str]]
) -> dict[str, object]:
    conn = store.init(assistant_db)
    conn.row_factory = sqlite3.Row
    try:
        charts = [dict(row) for row in conn.execute("SELECT * FROM charts")]
        if not charts:
            raise RuntimeError("assistant DB has no charts; run initialize first")
        summaries: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        for table_id, url in specs:
            try:
                table = fetch_table(table_id, url, cache_dir=cache_dir)
                report = resolve(table.entries, charts)
                with conn:
                    conn.execute("DELETE FROM table_entries WHERE table_id = ?", (table_id,))
                    conn.executemany(
                        """
                        INSERT INTO table_entries(table_id, level, sha256, md5, title, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                table_id,
                                match.entry.level,
                                str(match.chart["sha256"]),
                                match.chart.get("md5"),
                                match.entry.title or match.chart.get("title"),
                                table.fetched_at,
                            )
                            for match in report.matches
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO table_sources(
                          table_id, page_url, header_url, data_url, fetched_at,
                          last_error, entry_count, matched_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(table_id) DO UPDATE SET
                          page_url=excluded.page_url, header_url=excluded.header_url,
                          data_url=excluded.data_url, fetched_at=excluded.fetched_at,
                          last_error=excluded.last_error,
                          entry_count=excluded.entry_count,
                          matched_count=excluded.matched_count
                        """,
                        (
                            table_id,
                            url,
                            table.header_url,
                            table.data_url,
                            table.fetched_at,
                            "stale cache used after refresh failure" if table.stale else None,
                            len(table.entries),
                            report.matched,
                        ),
                    )
                summary = report.for_table(table_id)
                summaries.append(
                    {
                        "table_id": table_id,
                        "entries": len(table.entries),
                        "matched": 0 if summary is None else summary.matched,
                        "match_rate": 0.0 if summary is None else summary.match_rate,
                        "stale": table.stale,
                    }
                )
            except Exception as exc:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO table_sources(table_id, page_url, last_error)
                        VALUES (?, ?, ?)
                        ON CONFLICT(table_id) DO UPDATE SET
                          page_url=excluded.page_url, last_error=excluded.last_error
                        """,
                        (table_id, url, f"{type(exc).__name__}: {exc}"),
                    )
                errors.append({"table_id": table_id, "error": str(exc)})
        if not summaries:
            raise RuntimeError(f"no difficulty table refreshed: {errors}")
        return {"tables": summaries, "errors": errors}
    finally:
        conn.close()


def _build_features(songinfo_db: Path, assistant_db: Path) -> int:
    source = readers.open_snapshot(songinfo_db)
    destination = store.init(assistant_db)
    try:
        return build_all(source, destination)
    finally:
        source.close()
        destination.close()


def _persist_session(assistant_db: Path, session: object) -> None:
    conn = store.init(assistant_db)
    try:
        SQLiteRecommendationRepository(conn).save_output(recommendation_output(session))
    finally:
        conn.close()


def _latest_review(assistant_db: Path) -> dict[str, object]:
    conn = store.init(assistant_db)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            "SELECT * FROM daily_imports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return {"status": "empty", "message": "daily snapshot has not been imported"}
        daily = conn.execute(
            "SELECT * FROM player_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
        changes = conn.execute(
            """
            SELECT count(*) changed,
                   sum(CASE WHEN new_clear > old_clear THEN 1 ELSE 0 END) lamp_updates,
                   sum(CASE WHEN new_ex > old_ex THEN 1 ELSE 0 END) ex_updates,
                   sum(CASE WHEN new_minbp < old_minbp THEN 1 ELSE 0 END) bp_updates
            FROM score_changes WHERE import_id = ?
            """,
            (latest["id"],),
        ).fetchone()
        recommendation = conn.execute(
            "SELECT model_version, readiness, menu_date FROM recommendation_versions "
            "ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        model = conn.execute(
            "SELECT version, n_train, logloss, brier, baseline_logloss "
            "FROM model_state WHERE target='observed_completion' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return {
            "status": "ok",
            "effective_date": latest["effective_date"],
            "judged": None if daily is None else daily["delta_judged"],
            "plays": None if daily is None else daily["delta_playcount"],
            "lamp_updates": int(changes["lamp_updates"] or 0),
            "ex_updates": int(changes["ex_updates"] or 0),
            "bp_updates": int(changes["bp_updates"] or 0),
            "captured_latest_rows": int(latest["new_play_rows"]),
            "lost_events": int(latest["lost_events"]),
            "next_menu": None if recommendation is None else recommendation["menu_date"],
            "readiness": None if recommendation is None else recommendation["readiness"],
            "model": (
                {"status": "cold_start"}
                if model is None
                else {
                    "status": "validated_model",
                    "version": int(model["version"]),
                    "n_train": int(model["n_train"]),
                    "logloss": float(model["logloss"]),
                    "brier": float(model["brier"]),
                    "baseline_logloss": float(model["baseline_logloss"]),
                }
            ),
        }
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backfill":
        result = backfill.run(args.db_dir, args.assistant_db)
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "initialize":
        imported = backfill.run(args.db_dir, args.assistant_db)
        daily = snapshot.run(
            args.db_dir / "score.db",
            args.db_dir / "scoredatalog.db",
            args.assistant_db,
        )
        features = _build_features(args.db_dir / "songinfo.db", args.assistant_db)
        print(
            json.dumps(
                {"backfill": asdict(imported), "baseline": asdict(daily), "features": features},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "daily-update":
        result = snapshot.run(
            args.score_db, args.scoredatalog_db, args.assistant_db
        )
        menu_date = args.menu_date or (
            date.fromisoformat(result.effective_date) + timedelta(days=1)
        ).isoformat()
        conn = store.init(args.assistant_db)
        try:
            model = fit_latest(conn)
            session = build_session(
                conn,
                menu_date=menu_date,
                readiness=args.readiness,
                profile=_profile_context(args),
            )
        finally:
            conn.close()
        write_export(session, args.output_dir)
        _persist_session(args.assistant_db, session)
        print(
            json.dumps(
                {
                    "import": asdict(result),
                    "model": asdict(model),
                    "session": {
                        "menu_date": session.menu_date,
                        "readiness": session.readiness,
                        "seed": session.seed,
                        "model_status": session.model_status,
                        "model_version": session.model_version,
                        "table_warnings": list(session.table_warnings),
                        "warmup_adjustment": session.warmup_adjustment,
                        "queue_items": len(session.queue),
                        "personal_charts": len(session.personal),
                        "core_expected_judged": session.core_expected_judged,
                        "reserve_expected_judged": session.reserve_expected_judged,
                        "output_dir": str(args.output_dir),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "tables":
        result = _refresh_tables(
            args.assistant_db, args.cache_dir, _table_specs(args.table)
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "features":
        written = _build_features(args.songinfo_db, args.assistant_db)
        print(json.dumps({"features": written}, sort_keys=True))
        return 0

    if args.command == "menu":
        conn = store.init(args.assistant_db)
        try:
            session = build_session(
                conn,
                menu_date=args.date,
                readiness=args.readiness,
                profile=_profile_context(args),
            )
        finally:
            conn.close()
        write_export(session, args.output_dir)
        _persist_session(args.assistant_db, session)
        print(
            json.dumps(
                {
                    "menu_date": session.menu_date,
                    "seed": session.seed,
                    "table_warnings": list(session.table_warnings),
                    "warmup_adjustment": session.warmup_adjustment,
                    "queue_items": len(session.queue),
                    "personal_charts": len(session.personal),
                    "core_expected_judged": session.core_expected_judged,
                    "reserve_expected_judged": session.reserve_expected_judged,
                    "output_dir": str(args.output_dir),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "review":
        print(json.dumps(_latest_review(args.assistant_db), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "experiment":
        conn = store.init(args.assistant_db)
        try:
            if args.experiment_command == "start":
                starts_at = int(time.time()) if args.starts_at is None else args.starts_at
                result = start_experiment(
                    conn,
                    name=args.name,
                    seed=args.seed,
                    starts_at=starts_at,
                    days=args.days,
                    min_samples_per_arm=args.min_samples_per_arm,
                )
            elif args.experiment_command == "assign":
                session_at = int(time.time()) if args.session_at is None else args.session_at
                if args.candidates_json is None:
                    candidate_sets = build_candidate_sets(
                        conn,
                        experiment_id=args.experiment_id,
                        session_key=args.session_key,
                    )
                else:
                    candidate_sets = json.loads(
                        args.candidates_json.read_text(encoding="utf-8")
                    )
                    if not isinstance(candidate_sets, dict):
                        raise ValueError("--candidates-json must contain a JSON object")
                result = assign_session(
                    conn,
                    experiment_id=args.experiment_id,
                    session_key=args.session_key,
                    session_at=session_at,
                    candidate_sets=candidate_sets,
                )
            elif args.experiment_command == "candidates":
                result = build_candidate_sets(
                    conn,
                    experiment_id=args.experiment_id,
                    session_key=args.session_key,
                )
            elif args.experiment_command == "resolve":
                result = resolve_targets(
                    conn, experiment_id=args.experiment_id, now=args.now
                )
            elif args.experiment_command == "targets":
                result = list_targets(
                    conn,
                    experiment_id=args.experiment_id,
                    status=None if args.status == "all" else args.status,
                )
            else:
                result = report_experiment(conn, experiment_id=args.experiment_id)
        finally:
            conn.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "progress-token-create":
        created = create_progress_token(args.output)
        print(json.dumps({"created": str(created)}, ensure_ascii=False))
        return 0

    if args.command == "progress-send":
        token = args.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("progress token file is empty")
        try:
            if args.daemon:
                run_sender(
                    args.score_db,
                    args.url,
                    token,
                    source_id=args.source_id,
                    poll_interval=args.poll_interval,
                    heartbeat=args.heartbeat,
                )
            else:
                result = send_progress(
                    args.score_db, args.url, token, source_id=args.source_id
                )
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        except KeyboardInterrupt:
            return 0
        return 0

    if args.command == "serve":
        token = None
        if args.progress_token_file is not None:
            token = args.progress_token_file.read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("progress token file is empty")
        try:
            serve(
                args.export_dir,
                args.score_db,
                host=args.host,
                port=args.port,
                progress_state=args.progress_state,
                progress_token=token,
                progress_source_id=args.progress_source_id,
                progress_stale_after=args.progress_stale_after,
            )
        except KeyboardInterrupt:
            return 0
        return 0

    assert args.command == "collect"
    with Poller(
        args.db_dir,
        args.assistant_db,
        poll_interval=args.poll_interval,
    ) as poller:
        if args.daemon:
            try:
                poller.run_daemon(_render_tick)
            except KeyboardInterrupt:
                return 0
        else:
            _render_tick(poller.tick(force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
