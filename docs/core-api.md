# Python core / adapter boundary

Issue #4 keeps feature extraction, model fitting, and recommendation planning
independent of SQLite. The public boundary is deliberately small:

```text
adapter-owned records
  -> oraja_training.domain values
  -> pure feature/model/menu functions
  -> RecommendationOutput
  -> adapter-owned persistence or artifact writer
```

## Domain values

`oraja_training.domain` exports the immutable values used across the boundary:

- `ProfileContext(profile_id, display_name, timezone, seed_namespace)` carries
  identity and the namespace used for deterministic planning. The display name
  is presentation-only.
- `Play` is the normalized score event. `normalize_play(mapping, ...)` returns
  it; `derive_play(mapping, ...)` is the legacy plain-mapping shim for the
  local store.
- `Chart` describes chart metadata without a database row or connection.
- `Observation` is a model observation, and `ModelSnapshot` is validated model
  parameters and metrics.
- `RecommendationInput` contains the latest import, candidate mappings, and an
  optional `ModelSnapshot`. `RecommendationOutput` contains the deterministic
  result plus persistence metadata.

Values crossing the boundary are validated. Invalid values raise
`DomainValidationError`, a `CoreContractError` under the common `DomainError`
base. Persistence failures should be translated by an adapter to
`RepositoryError`; planner-specific insufficiency is `MenuBuildError`.

## Ports

`FeatureRepository` provides `iter_songinfo()` and `save_features(features)`.
`ModelRepository` provides `iter_observations()`, `latest_model()`, and
`save_model(model)`. `RecommendationRepository` provides
`load_input(profile)` and `save_output(output)`. A write operation may receive
the `UnitOfWork` port (`commit()` / `rollback()`) so a repository and its
transaction share one boundary.

The local implementations are `SQLiteFeatureRepository`,
`SQLiteModelRepository`, `SQLiteRecommendationRepository`, and
`SQLiteUnitOfWork` in `oraja_training.db`. They are the only adapters that
know the assistant SQLite schema. Schema creation and migration remain in
`db.store`.

## Core entry points

These functions do not import `sqlite3` or open files:

- `features.extract_features(row)` and `features.build_feature_rows(rows)`
- `model.fit_observations(observations)`, `model.fit_repository(repository)`,
  and `model.predict_snapshot(snapshot, level, density, scratch)`
- `plan.build_session_from_input(input)` and
  `plan.build_session_from_repository(repository, profile=...)`
- `plan.recommendation_output(session)`

The existing `features.build_all(source, destination)`,
`model.fit_latest(connection)`, `model.predict_latest(connection, ...)`, and
`plan.build_session(connection, ...)` calls remain compatibility shims. They
delegate to the local adapters so the current CLI and generated JSON surfaces
continue to work.

## Profile and determinism rules

The default local context is `local-profile` / `Personal` for CLI compatibility.
Profile ID (or an explicit `seed_namespace`) is the only profile value used in
the menu seed. Display name and timezone do not affect it, and no user name or
absolute player path is embedded in a seed, table key, or domain value. A
service/container adapter must authenticate ownership, construct its own
`ProfileContext`, and only then call `RecommendationRepository.load_input`.

For a new adapter, the minimum integration is:

```python
profile = ProfileContext(profile_id, display_name, timezone)
result = build_session_from_repository(repo, profile=profile, menu_date=menu_date)
repo.save_output(recommendation_output(result))
```

The adapter may serialize the output differently; it must preserve the seed,
profile ID, import ID, model version, and queue contents needed to reproduce or
audit the result.
