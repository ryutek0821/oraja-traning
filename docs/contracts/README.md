# 機械可読契約 v1

JSON Schema は JSON Schema 2020-12 で記述し、共通定義は canonical URI `https://oraja-training.dev/contracts/common.schema.json` の `$defs` を `$ref` で参照する。schema の version は受理 semantics の version であり、既存ローカル `assistant.db` の schema version とは別物である。

| 契約 | 用途 | owner/trust | 冪等性 |
|---|---|---|---|
| [`domain-entities.v1`](domain-entities.v1.json) | Account/Profile/Device/PlayEvent/Job/RecommendationVersion の ID・owner・lifecycle | account/profile parent と trust domain | entityごとの key |
| [`ir-event.v1`](ir-event.v1.schema.json) | IRからの1プレイ allowlist event（client eligibility claimなし） | profile + device、server attested | `event_id` |
| [`aggregate-eligibility.v1`](aggregate-eligibility.v1.schema.json) | 公式集合学習の server-attested eligibility decision | official account/profile、同意・SP7・非course | `decision_id` + source digest |
| [`upload-manifest.v1`](upload-manifest.v1.schema.json) | 5DB静的 snapshot | account/profile、rawは常に aggregate不可 | `profile_id:manifest_sha256` |
| [`container-input-manifest.v1`](container-input-manifest.v1.schema.json) | Containerへ渡す暗号化 input | job/profile/trust partition | job input digest |
| [`container-output-manifest.v1`](container-output-manifest.v1.schema.json) | 正規化・派生 output | job/profile/trust partition | job idempotency key |
| [`artifact-manifest.v1`](artifact-manifest.v1.schema.json) | 2表・Web export・model artifact | immutable profile release | `profile:revision:manifest_sha256` |

## Schema と semantic validation の分担

JSON Schema は型、必須キー、allowlist、サイズ、SP7、self-hosted の `aggregate_eligible=false` など、単一 payload で検証できる条件を強制する。次の cross-record 条件は Worker/DO/Container の semantic validator と DB transaction で必ず検証する。

- `account_id → profile_id → device_id` の所有権
- 同じ `event_id` の payload digest と duplicate ACK
- upload の5ファイル名が各1個で、manifest と object digest/size が一致すること
- `trust_domain` と R2 prefix / job partition / aggregate query の一致
- `artifact.latest_pointer` の profile/version/digest が manifest と一致し、revision が current より大きいこと
- `previous_revision`、job input/output、source/output digest の連鎖
- `is_course=true` の model exclusion
- Container の `job_type`、`input_digest`、`idempotency_key` が同じ job preimage を指すこと
- latest pointer の `profile_id` と `trust_domain` が外側の artifact と同じ partition に属すること

cross-record の canonical digest、digest chain、latest pointer CAS、集合学習 gate の
機械可読な規則は [`semantic-rules.v1.json`](semantic-rules.v1.json) に固定する。
IR event の `aggregate_eligible` は常に `false` であり、`aggregate_eligible` は IR client payload の自己申告ではなく、
[`aggregate-eligibility.v1`](aggregate-eligibility.v1.schema.json) を official Worker
が発行した場合だけ有効とする。

## 正常例・拒否例

`examples/` には各契約の正常例と、allowlist・trust boundary・revision 条件を破る拒否例を置く。拒否例は「HTTP error body」ではなく、validator が拒否すべき入力 fixture である。

Container/artifact の `aggregate_eligible=true` は service が発行する derived manifest の
例であり、完全な受理には `source_*_manifest_sha256` と一致する
`aggregate-eligibility.v1` decision の lookup が必要である。JSON Schema はこの別レコード
参照を表せないため、Worker/DO の semantic validator が decision の issuer、同意、SP7、
非course、trust domain を再確認する。

| 契約 | 正常例 | 拒否例 |
|---|---|---|
| IR event | `examples/ir-event.valid.json` | `examples/ir-event.invalid-extra-field.json`、`ir-event.invalid-dp.json` |
| aggregate eligibility | `examples/aggregate-eligibility.valid.json` | `examples/aggregate-eligibility.invalid-self-hosted.json` |
| upload | `examples/upload-manifest.valid.json` | `examples/upload-manifest.invalid-sidecar.json` |
| Container input | `examples/container-input.valid.json` | `examples/container-input.invalid-self-hosted-aggregate.json` |
| Container output | `examples/container-output.valid.json` | `examples/container-output.invalid-self-hosted-aggregate.json` |
| artifact | `examples/artifact-manifest.valid.json` | `examples/artifact-manifest.invalid-revision.json` |

拒否理由は [API contract](../api-contract.md) の `invalid_contract`、`unsupported_game_mode`、`disallowed_field`、`idempotency_conflict` のいずれかへ写像し、raw payloadを監査ログへ保存しない。
