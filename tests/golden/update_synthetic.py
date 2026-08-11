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
from oraja_training.domain.types import ModelSnapshot, ProfileContext, RecommendationInput
from oraja_training.plan.menu import build_session_from_input
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


def _model(catalog: dict) -> ModelSnapshot:
    value = catalog["model"]
    return ModelSnapshot(
        target=value["target"],
        version=int(value["version"]),
        trained_at=int(value["trained_at"]),
        n_train=int(value["n_train"]),
        weights=tuple(float(item) for item in value["weights"]),
        means=tuple(float(item) for item in value["means"]),
        scales=tuple(float(item) for item in value["scales"]),
        feature_names=tuple(value["feature_names"]),
        metrics={key: float(item) for key, item in value["metrics"].items()},
        description=value["description"],
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


def _recommendation(fixture: Path) -> dict:
    catalog = load_catalog(fixture)
    profile = ProfileContext(
        profile_id="synthetic-profile",
        display_name="Synthetic",
        timezone="UTC",
        seed_namespace="synthetic-golden",
    )
    source = RecommendationInput(
        profile=profile,
        import_id=1,
        baseline_judged=140,
        candidates=tuple(catalog["owned"]),
        model=_model(catalog),
    )
    session = build_session_from_input(
        source,
        menu_date=FIXED_MENU_DATE,
        clock=lambda: FIXED_CLOCK,
    )
    category_counts: dict[str, int] = {}
    category_expected: dict[str, int] = {}
    for item in session.queue:
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
        category_expected[item.category] = (
            category_expected.get(item.category, 0) + item.expected_judged
        )
    return {
        "model_state": {
            "target": source.model.target,
            "version": source.model.version,
            "status": session.model_status,
            "n_train": source.model.n_train,
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
    payload.update(_recommendation(fixture))
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
