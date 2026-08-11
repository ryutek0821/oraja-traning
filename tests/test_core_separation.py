from __future__ import annotations

from pathlib import Path

from oraja_training.collect.normalize import derive_play, normalize_play
from oraja_training.db import store
from oraja_training.db.recommendation_adapter import SQLiteRecommendationRepository
from oraja_training.domain import (
    ModelSnapshot,
    Play,
    ProfileContext,
    RecommendationInput,
    RecommendationOutput,
)
from oraja_training.features import build_feature_rows
from oraja_training.model import FitResult
from oraja_training.model.core import fit_repository
from oraja_training.plan import build_session_from_input, recommendation_output


ROOT = Path(__file__).parents[1]


def test_core_modules_do_not_import_sqlite() -> None:
    paths = (
        ROOT / "src/oraja_training/domain",
        ROOT / "src/oraja_training/collect/normalize.py",
        ROOT / "src/oraja_training/features/build.py",
        ROOT / "src/oraja_training/model/core.py",
        ROOT / "src/oraja_training/model/fit.py",
        ROOT / "src/oraja_training/plan/menu.py",
    )
    sources = []
    for path in paths:
        sources.extend(
            item.read_text()
            for item in (path.glob("*.py") if path.is_dir() else (path,))
        )
    assert all("import sqlite3" not in source for source in sources)


def test_normalize_returns_domain_play_and_legacy_record() -> None:
    row = {
        "sha256": "a" * 64,
        "mode": 0,
        "clear": 6,
        "epg": 10,
        "lpg": 10,
        "egr": 0,
        "lgr": 0,
        "egd": 0,
        "lgd": 0,
        "ebd": 0,
        "lbd": 0,
        "epr": 0,
        "lpr": 0,
        "ems": 0,
        "lms": 0,
        "notes": 20,
        "minbp": 1,
        "playcount": 1,
        "date": 123,
    }
    play = normalize_play(row, source="collector", source_generation=0)
    assert isinstance(play, Play)
    assert play.completed == 1
    assert derive_play(row, source="collector", source_generation=0)["is_course"] == 0


def test_feature_rows_are_calculated_before_persistence() -> None:
    rows = build_feature_rows(
        [
            {
                "sha256": "b" * 64,
                "distribution": "#00000000000500",
                "speedchange": "120,0,120,1000",
                "lanenotes": "5,0,0",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0].sha256 == "b" * 64


def _candidates(count: int = 24) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "sha256": f"{index:064x}",
            "md5": f"{index:032x}",
            "title": f"Chart {index}",
            "artist": "Artist",
            "notes": 1000 + index,
            "table_id": "satellite",
            "level": str(5 + index % 8),
            "clear": index % 7,
            "playcount": index % 4,
            "last_played": 0,
            "density": 0.1 + index / 100,
            "scratch": 0.2,
            "model_scratch": 0.2,
            "ln": 0.1,
            "soflan": 0.1,
        }
        for index in range(count)
    )


def test_profile_display_name_does_not_change_seed() -> None:
    first = RecommendationInput(
        ProfileContext("profile-a", "Alice", "UTC"), 1, 10, _candidates()
    )
    second = RecommendationInput(
        ProfileContext("profile-a", "Bob", "Asia/Tokyo"), 1, 10, _candidates()
    )
    first_session = build_session_from_input(first, menu_date="2026-08-11", target_judged=100, reserve_judged=10)
    second_session = build_session_from_input(second, menu_date="2026-08-11", target_judged=100, reserve_judged=10)
    assert first_session.seed == second_session.seed
    output = recommendation_output(first_session)
    assert output.profile.profile_id == "profile-a"
    assert output.queue


def test_model_repository_port_commits_only_a_passed_snapshot(monkeypatch) -> None:
    snapshot = ModelSnapshot(
        target="observed_completion",
        version=1,
        trained_at=1,
        n_train=10,
        weights=(0.0, 1.0, 0.0, 0.0),
        means=(0.0, 0.0, 0.0),
        scales=(1.0, 1.0, 1.0),
        feature_names=("level", "density_p99", "scratch_rate"),
    )

    class Repository:
        def iter_observations(self):
            return ()

        def latest_model(self):
            return None

        def save_model(self, model):
            self.saved = model

    class UnitOfWork:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    repository = Repository()
    unit_of_work = UnitOfWork()
    monkeypatch.setattr(
        "oraja_training.model.core.fit_observations",
        lambda *args, **kwargs: FitResult("passed", 10, 2, model=snapshot),
    )
    result = fit_repository(repository, unit_of_work=unit_of_work)
    assert result.status == "passed"
    assert repository.saved is snapshot
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0


def test_sqlite_recommendation_repository_preserves_output_metadata(tmp_path) -> None:
    conn = store.init(tmp_path / "assistant.db")
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO daily_imports(
                  imported_at, effective_date, score_sha256, scoredatalog_sha256,
                  baseline_judged, cumulative_playcount
                ) VALUES (1, '2026-08-11', 'score', 'catalog', 10, 1)
                """
            )
        output = RecommendationOutput(
            profile=ProfileContext("profile-a", "Alice", "UTC"),
            menu_date="2026-08-12",
            seed="0123456789abcdef",
            queue=({"sha256": "a" * 64},),
            personal=(),
            import_id=int(cursor.lastrowid),
            baseline_judged=10,
            generated_at=123,
        )
        SQLiteRecommendationRepository(conn).save_output(output)
        stored = conn.execute("SELECT manifest_json FROM recommendation_versions").fetchone()
        assert stored is not None
        assert '"profile_id":"profile-a"' in stored[0]
    finally:
        conn.close()
