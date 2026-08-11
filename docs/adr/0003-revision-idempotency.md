# ADR-0003: job、revision、冪等性、latest pointer

- Status: Accepted
- Date: 2026-08-11
- Scope: issue #2 / Phase 0

## Identifiers

| 操作 | `idempotency_key` | 重複判定 | payload差分 |
|---|---|---|---|
| IR `PlayEvent` | `event_id` | `(profile_id, event_id)` | 409 `idempotency_conflict` |
| 5DB upload | `profile_id:manifest_sha256` | 同じ profile と manifest digest | 409。別manifestは新しい upload |
| Container `Job` | `job:<profile_id>:<input_digest>`（`input_digest` は job type・contract version・入力を含む） | 同じ profile/partition/input | 既存成功 output を返す |
| artifact publish | `profile_id:revision:manifest_sha256` | revision と manifest digest | 409。revisionの再利用禁止 |

key を再送する caller は同じ body、同じ owner、同じ contract version を使う。成功 ACK 後の再送は副作用を追加せず、既存の canonical ID/status を返す。

## Canonical digest preimage

digest を比較するすべての境界は、RFC 8785 JSON Canonicalization Scheme (JCS) で
canonical 化した UTF-8 bytes に SHA-256 を適用する。digest は小文字 hex で表す。
実装は canonical 化の前に duplicate object name、`NaN`、`Infinity`、unpaired
surrogate を拒否する。JSON の whitespace、object key の順序、escape の表記差は
digest に影響させないが、文字列の意味を変える Unicode normalization は行わない。

- `payload_digest`: 受理した契約 object の JCS。HTTP header、request ID、受信時刻などの transport/server metadata は除外する。
- `manifest_sha256`: 保存する manifest object 自体の JCS。自分自身の digest を含めない。
- `input_digest`: `{contract_versions,input_manifest_sha256,profile_id,trust_domain,job_type}` の JCS。Container input/output manifest は `job_type` とこの digest を明示する。
- Job key は `job:<profile_id>:<input_digest>`、upload key は `<profile_id>:<manifest_sha256>` とする。

JSON Schema の検証だけでは digest の内容や owner を確定できないため、Worker/DO/Container の semantic validator が同じ canonicalizer を共有する。

## Monotonic revision rule

1. Profile DO が処理対象の `revision` を profile + trust domain stream 内で一度だけ予約する。
2. `RecommendationVersion`、output manifest、artifact manifest はその revision と input digest を immutable に記録する。
3. publisher は `latest_pointer` を `candidate_revision > current_revision` の compare-and-set でだけ更新する。
4. 古い job が後から成功しても、`candidate_revision <= current_revision` なら artifact は保管できるが latest にはならない。
5. `revision` は profile と trust domain の境界を越えて比較しない。削除後に同じ profile ID を再利用しない。

Candidate の cross-record 検証は次の順で行う。

1. artifact の `profile_id`、`recommendation_version_id`、`revision` が pointer の同名フィールドと一致し、`manifest_sha256` が artifact manifest の JCS digest と一致することを確認する。
2. 初回は `previous_revision=null`、それ以外は transaction 開始時の current revision と `previous_revision` が一致することを確認する。
3. `profile_id` と `trust_domain` を partition key にした compare-and-set を実行する。latest pointer 自体にも両方を記録し、candidate が current 以下なら immutable artifact だけを残し、pointer は変更しない。

output manifest → artifact manifest の digest、upload manifest → Container input の digest は、同じ chain の前段を参照していることを必須とする。どこか一つでも不一致なら publish effect を作らず、既存の immutable record は削除しない。

## ACK boundary

`202 Accepted` は Worker/DO が event または upload の idempotency record を durable に commit し、再試行可能な Queue/Workflow job を登録した後にだけ返す。ACK はモデル更新や latest pointer 更新の完了を意味しない。duplicate replay は `200` と既存 canonical ID を返し、同じ payload であることを digest 照合する。

## Retry and failure

- Queue delivery は at-least-once。worker/container は同じ key の record を再利用する。
- timeout、429、502、503、504 は `Retry-After` と backoff を使って同じ key で再送する。
- schema/owner/scope/unsupported-mode の 4xx は自動 retry しない。
- 途中で pointer 更新に失敗しても immutable artifact と job record を消さず、同じ publish key を再実行する。
