# ADR-0003: job、revision、冪等性、latest pointer

- Status: Accepted
- Date: 2026-08-11
- Scope: issue #2 / Phase 0

## Identifiers

| 操作 | `idempotency_key` | 重複判定 | payload差分 |
|---|---|---|---|
| IR `PlayEvent` | `event_id` | `(profile_id, event_id)` | 409 `idempotency_conflict` |
| 5DB upload | `profile_id:manifest_sha256` | 同じ profile と manifest digest | 409。別manifestは新しい upload |
| Container `Job` | `job_type:profile_id:input_digest:contract_versions` の canonical SHA-256 | 同じ profile/partition/input | 既存成功 output を返す |
| artifact publish | `profile_id:revision:manifest_sha256` | revision と manifest digest | 409。revisionの再利用禁止 |

key を再送する caller は同じ body、同じ owner、同じ contract version を使う。成功 ACK 後の再送は副作用を追加せず、既存の canonical ID/status を返す。

## Monotonic revision rule

1. Profile DO が処理対象の `revision` を profile + trust domain stream 内で一度だけ予約する。
2. `RecommendationVersion`、output manifest、artifact manifest はその revision と input digest を immutable に記録する。
3. publisher は `latest_pointer` を `candidate_revision > current_revision` の compare-and-set でだけ更新する。
4. 古い job が後から成功しても、`candidate_revision <= current_revision` なら artifact は保管できるが latest にはならない。
5. `revision` は profile と trust domain の境界を越えて比較しない。削除後に同じ profile ID を再利用しない。

## ACK boundary

`202 Accepted` は Worker/DO が event または upload の idempotency record を durable に commit し、再試行可能な Queue/Workflow job を登録した後にだけ返す。ACK はモデル更新や latest pointer 更新の完了を意味しない。duplicate replay は `200` と既存 canonical ID を返し、同じ payload であることを digest 照合する。

## Retry and failure

- Queue delivery は at-least-once。worker/container は同じ key の record を再利用する。
- timeout、429、502、503、504 は `Retry-After` と backoff を使って同じ key で再送する。
- schema/owner/scope/unsupported-mode の 4xx は自動 retry しない。
- 途中で pointer 更新に失敗しても immutable artifact と job record を消さず、同じ publish key を再実行する。
