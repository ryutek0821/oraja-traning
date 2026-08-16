from __future__ import annotations

from oraja_training.domain import ModelSnapshot, ProfileContext
from oraja_training.model.core import predict_snapshot


def test_profile_context_owns_deterministic_namespace() -> None:
    first = ProfileContext("profile-a", "Alice", "Asia/Tokyo")
    second = ProfileContext("profile-b", "Bob", "Asia/Tokyo")
    assert first.deterministic_namespace == "profile-a"
    assert second.deterministic_namespace == "profile-b"
    assert first.deterministic_namespace != second.deterministic_namespace


def test_prediction_is_storage_independent() -> None:
    snapshot = ModelSnapshot(
        target="observed_completion",
        version=1,
        trained_at=1,
        n_train=10,
        weights=(0.0, 1.0, 0.0, 0.0),
        means=(0.0, 0.0, 0.0),
        scales=(1.0, 1.0, 1.0),
        feature_names=(
            "table_completion_margin", "density_p99", "scratch_rate"
        ),
    )
    assert predict_snapshot(snapshot, 1.0, 0.0, 0.0) > 0.5
