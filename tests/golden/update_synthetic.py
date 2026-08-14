"""Generate or verify the deterministic synthetic output golden.

Usage from the repository root::

    uv run python tests/golden/update_synthetic.py --check
    uv run python tests/golden/update_synthetic.py --write

The write mode only replaces ``synthetic_outputs.json``.  Review that diff,
then run the fixture/golden tests before accepting a baseline change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from oraja_training.collect.backfill import run as backfill
from oraja_training.plan.menu import build_session
from tests.fixtures.synthetic_beatoraja import build_synthetic_fixture, load_catalog


GOLDEN_PATH = Path(__file__).with_name("synthetic_outputs.json")
FIXED_CLOCK = 1_786_291_200
FIXED_MENU_DATE = "2026-08-10"
NORMALIZED_COLUMNS = (
    "sha256", "mode", "played_at", "playcount", "source_generation", "source",
    "clear", "ex", "minbp", "notes", "judged", "empty_poor", "survival",
    "completed", "bp_rate", "credited_gauge_kind", "selected_gauge_kind",
    "option", "seed", "random", "trophy", "is_course",
    "exceeded_aggregate_score", "lost_events", "payload_hash", "ingested_at",
)


def _normalized_plays(fixture: Path, root: Path) -> dict:
    assistant = root / "assistant.db"
    result = backfill(fixture, assistant, clock=lambda: 1_700_000_000)
    conn = sqlite3.connect(assistant)
    try:
        rows = [
            dict(zip(NORMALIZED_COLUMNS, row))
            for row in conn.execute(
                "SELECT " + ", ".join(NORMALIZED_COLUMNS) +
                " FROM plays ORDER BY source, sha256, mode"
            )
        ]
    finally:
        conn.close()
    return {
        "result": {
            "charts": result.charts,
            "legacy_plays": result.legacy_plays,
            "ir_imports": result.ir_imports,
            "song_rows_seen": result.song_rows_seen,
            "skipped_ir_noplay": result.skipped_ir_noplay,
        },
        "columns": list(NORMALIZED_COLUMNS),
        "rows": rows,
    }


def _recommendation(fixture: Path, root: Path) -> dict:
    catalog = load_catalog(fixture)
    conn = sqlite3.connect(root / "assistant.db")
    model = catalog["model"]
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO daily_imports(
                  imported_at, effective_date, score_sha256,
                  scoredatalog_sha256, baseline_judged, cumulative_playcount
                ) VALUES (?, ?, 'synthetic-score', 'synthetic-log', 140, 4)
                """,
                (FIXED_CLOCK, FIXED_MENU_DATE),
            )
            import_id = int(cursor.lastrowid)
            for row in catalog["owned"]:
                conn.execute(
                    "INSERT OR REPLACE INTO charts VALUES (?, ?, ?, ?, ?, 7, ?, ?)",
                    (
                        row["sha256"], row["md5"], row["title"], row["artist"],
                        row["notes"], f"synthetic/{row['sha256']}.bms", FIXED_CLOCK,
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO table_entries VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["table_id"], row["level"], row["sha256"],
                        row["md5"], row["title"], FIXED_CLOCK,
                    ),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO chart_features VALUES (
                      ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        row["sha256"], row["density"], row["density"],
                        row["density"], row["density"], row["density"],
                        row["model_scratch"], row["scratch"], row["scratch"],
                        row["ln"], 0.0, row["soflan"], 0, 120.0, row["notes"],
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO score_state VALUES (?, 0, ?, 0, 0, 0, ?, ?, ?, ?, ?)",
                    (
                        row["sha256"], row["clear"], row["notes"],
                        row["playcount"], row["last_played"], import_id,
                        f"synthetic-{row['sha256']}",
                    ),
                )
            params = {
                "weights": model["weights"],
                "means": model["means"],
                "scales": model["scales"],
            }
            conn.execute(
                "INSERT INTO model_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model["target"], model["version"], model["trained_at"],
                    json.dumps(params, sort_keys=True), model["n_train"],
                    model["metrics"]["logloss"], model["metrics"]["brier"],
                    model["metrics"]["baseline_logloss"],
                ),
            )
        session = build_session(
            conn, menu_date=FIXED_MENU_DATE, clock=lambda: FIXED_CLOCK
        )
    finally:
        conn.close()
    category_counts: dict[str, int] = {}
    category_expected: dict[str, int] = {}
    for item in session.queue:
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
        category_expected[item.category] = (
            category_expected.get(item.category, 0) + item.expected_judged
        )
    return {
        "model_state": {
            "target": model["target"],
            "version": model["version"],
            "status": session.model_status,
            "n_train": model["n_train"],
        },
        "recommend_candidates": {
            "count": len(session.personal),
            "sha256": [candidate.sha256 for candidate in session.personal],
            "top": [
                {
                    "sha256": candidate.sha256,
                    "title": candidate.title,
                    "p_complete": candidate.p_complete,
                    "tier": candidate.tier,
                    "target": candidate.target,
                    "primary_axis": candidate.primary_axis,
                }
                for candidate in session.personal[:10]
            ],
        },
        "daily_menu": {
            "menu_date": session.menu_date,
            "generated_at": session.generated_at,
            "seed": session.seed,
            "target_judged": session.target_judged,
            "reserve_target": session.reserve_target,
            "core_expected_judged": session.core_expected_judged,
            "reserve_expected_judged": session.reserve_expected_judged,
            "queue_count": len(session.queue),
            "category_counts": category_counts,
            "category_expected_judged": category_expected,
            "queue_sha256": [item.sha256 for item in session.queue],
            "queue_expected_judged": [item.expected_judged for item in session.queue],
            "weakness_axes": list(session.weakness_axes),
        },
    }


def build_golden_payload(root: Path) -> dict:
    fixture = build_synthetic_fixture(root / "fixture")
    payload = {
        "golden_version": 1,
        "fixture_version": 2,
        "normalized_plays": _normalized_plays(fixture, root),
    }
    payload.update(_recommendation(fixture, root))
    return payload


def _render(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the committed golden differs")
    parser.add_argument("--write", action="store_true", help="replace synthetic_outputs.json")
    args = parser.parse_args()
    if args.check == args.write:
        parser.error("choose exactly one of --check or --write")

    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="oraja-synthetic-golden-") as directory:
        rendered = _render(build_golden_payload(Path(directory)))
    if args.write:
        GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        return 0
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    if rendered != expected:
        print("synthetic_outputs.json is stale; run with --write and review the diff")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
