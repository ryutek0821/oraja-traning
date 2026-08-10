"""Command-line interface for initial import and live collection."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Sequence

from oraja_training.collect import backfill, snapshot
from oraja_training.collect.poller import Poller, TickResult
from oraja_training.db import readers, store
from oraja_training.features import build_all
from oraja_training.model import fit_latest
from oraja_training.plan import build_session, write_export
from oraja_training.serve import serve
from oraja_training.tables import fetch_table, resolve


DEFAULT_TABLES = (
    ("satellite", "https://stellabms.xyz/sl/table.html"),
    ("genocide", "https://nekokan.dyndns.info/~lobsak/genocide/"),
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

    tables = subcommands.add_parser("tables", help="difficulty table operations")
    table_commands = tables.add_subparsers(dest="tables_command", required=True)
    refresh = table_commands.add_parser("refresh", help="fetch and match source tables")
    refresh.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))
    refresh.add_argument("--cache-dir", type=Path, default=Path(".cache/tables"))
    refresh.add_argument(
        "--table", action="append", default=[], metavar="ID=URL",
        help="source table page/header; defaults to Satellite and GENOCIDE",
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

    review = subcommands.add_parser("review", help="show latest daily training summary")
    review.add_argument("--assistant-db", type=Path, default=Path("assistant.db"))

    server = subcommands.add_parser("serve", help="serve tables and training cockpit")
    server.add_argument("--export-dir", type=Path, default=Path("export/current"))
    server.add_argument("--score-db", type=Path)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    return parser


def _render_tick(result: TickResult) -> None:
    if result.scanned or result.new_plays or result.lost_events:
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True), flush=True)


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
                          table_id, page_url, header_url, data_url, fetched_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(table_id) DO UPDATE SET
                          page_url=excluded.page_url, header_url=excluded.header_url,
                          data_url=excluded.data_url, fetched_at=excluded.fetched_at,
                          last_error=NULL
                        """,
                        (table_id, url, table.header_url, table.data_url, table.fetched_at),
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
    payload = json.dumps(asdict(session), ensure_ascii=False, separators=(",", ":"))
    conn = store.init(assistant_db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO sessions(created_at, arm, slots_json) VALUES (?, ?, ?)",
                (
                    int(getattr(session, "generated_at")),
                    "model" if int(getattr(session, "model_version")) else "heuristic",
                    payload,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO recommendation_versions(
                  generated_at, menu_date, import_id, model_version,
                  seed, readiness, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(getattr(session, "generated_at")),
                    str(getattr(session, "menu_date")),
                    int(getattr(session, "import_id")),
                    int(getattr(session, "model_version")),
                    str(getattr(session, "seed")),
                    str(getattr(session, "readiness")),
                    payload,
                ),
            )
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
                conn, menu_date=menu_date, readiness=args.readiness
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
            session = build_session(conn, menu_date=args.date, readiness=args.readiness)
        finally:
            conn.close()
        write_export(session, args.output_dir)
        _persist_session(args.assistant_db, session)
        print(
            json.dumps(
                {
                    "menu_date": session.menu_date,
                    "seed": session.seed,
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

    if args.command == "serve":
        try:
            serve(
                args.export_dir,
                args.score_db,
                host=args.host,
                port=args.port,
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
