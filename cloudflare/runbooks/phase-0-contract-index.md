# Phase 0 (#24) 契約成果物・受入索引

監査日: 2026-08-11

対象: [#24 Phase 0 — 契約確定](https://github.com/ryutek0821/oraja-traning/issues/24)
子issue: [#2 サービス境界・データ分類・イベント契約](https://github.com/ryutek0821/oraja-traning/issues/2)

この文書は、#24 の進捗を #2 の本文・成果物・検証へ結び付ける索引である。#24 の本文を
Phase の受入条件、#2 の本文を契約の正とし、ここでは実装issueのコードやGitHub issueを
変更しない。文書・schema・fixtureが存在することは、実行経路の受入やデプロイ済みを意味しない。

## Phase 0 の監査結果

GitHub の読み取り確認では #24 の native sub-issue として #2 が登録されており、#2 は
OPEN のままである。したがって、現在の状態は「#2 の設計成果物と検証導線を索引化済み、
#2 の完了・close は未実施」とする。

| Phase 条件 | 現在の証拠 | 判定 |
|---|---|---|
| #24 → #2 を辿れる | GitHub native sub-issue #2、上記リンク | 確認済み |
| #2 の完了状態を進捗へ反映 | #2 が OPEN、成果物と自動検証の状態を下表に記録 | 進行中 |
| #2 本文を依存関係の正とする | 本索引は #2 の Todo/完了条件を改変せず参照 | 確認済み |

## #2 Todo と契約成果物

| #2 の成果物 | 正となる成果物 | 検証導線 |
|---|---|---|
| 公式 Cloudflare / self-hosted の信頼境界をADR化 | [architecture.md](../../docs/architecture.md)、[ADR-0002](../../docs/adr/0002-domain-identifiers-lifecycle.md)、[data-classification.md](../../docs/data-classification.md) | architecture のデータフロー・分離不変条件 |
| Account / Profile / Device / PlayEvent / Job / RecommendationVersion | [domain-entities.md](../../docs/contracts/domain-entities.md)、[domain-entities.v1.json](../../docs/contracts/domain-entities.v1.json) | `test_domain_entity_catalog_fixes_profile_limit_and_ownership` |
| 1 account 1 profile の制約 | `PROFILE_LIMIT=1` と lifecycle/slot transaction の定義 | 同上、domain entity catalog |
| 個人・資格情報・生DB・派生・匿名集合・監査の分類 | [data-classification.md](../../docs/data-classification.md) | official/self_hosted aggregate gate の記載 |
| IR event の versioned JSON Schema | [ir-event.v1](../../docs/contracts/ir-event.v1.schema.json)、[正常例](../../docs/contracts/examples/ir-event.valid.json)、[拒否例](../../docs/contracts/examples/ir-event.invalid-extra-field.json) | `test_valid_contract_examples` / `test_rejected_contract_examples`、DP/course/aggregate 各テスト |
| job/revision/idempotency/latest の単調規則 | [ADR-0003](../../docs/adr/0003-revision-idempotency.md)、[semantic-rules.v1](../../docs/contracts/semantic-rules.v1.json) | digest、job key、latest pointer の semantic tests |
| 5DB / Container / artifact manifest | [contracts README](../../docs/contracts/README.md)、[upload](../../docs/contracts/upload-manifest.v1.schema.json)、[Container input](../../docs/contracts/container-input-manifest.v1.schema.json)、[Container output](../../docs/contracts/container-output-manifest.v1.schema.json)、[artifact](../../docs/contracts/artifact-manifest.v1.schema.json) | 各正常例・拒否例、5DB exact-name、path traversal、digest-chain tests |
| API認証、エラー、retry、監査イベント | [api-contract.md](../../docs/api-contract.md) | 認証入口、ACK、error code、監査event、範囲外拒否の文書確認 |
| DP/PMS等の初期範囲外拒否 | [architecture.md](../../docs/architecture.md)、[api-contract.md](../../docs/api-contract.md)、[ir-event invalid DP](../../docs/contracts/examples/ir-event.invalid-dp.json) | `test_ir_event_rejects_dp_game_mode_fixture` と拒否例検証 |
| コンポーネント依存・migration/rollback | [architecture.md](../../docs/architecture.md)、[migration-rollback.md](migration-rollback.md) | architecture の一枚図、rollback手順、下記の契約テスト |

## #2 完了条件の受入マトリクス

| #2 完了条件 | 受入証拠 | 現時点の範囲 |
|---|---|---|
| 全コンポーネントの入力・出力・所有者・信頼境界を1つの図から追跡できる | [architecture.md のデータフロー](../../docs/architecture.md)、コンポーネント契約表 | 設計文書として確認済み。実行時強制は後続issue |
| event/upload/artifact 契約に正常例・拒否例がある | [contracts README](../../docs/contracts/README.md) の例一覧、各 v1 schema | 例とschema検証を確認済み |
| self-hosted データが公式集合学習へ混入しない条件がテスト可能 | [aggregate-eligibility.v1](../../docs/contracts/aggregate-eligibility.v1.schema.json)、[semantic-rules.v1](../../docs/contracts/semantic-rules.v1.json)、[data-classification.md](../../docs/data-classification.md) | schema/semantic contract の境界を確認済み。実サービスの越境試験は後続issue |
| #1 の確定仕様と矛盾しない | [SPEC.md](../../SPEC.md)、[#1](https://github.com/ryutek0821/oraja-traning/issues/1)、architecture の監査表 | 設計文書間の整合を確認済み。#1 の実装・公開gateは未完了 |

## 検証コマンドとリンク

Phase 0 の再現可能な最小検証は次の通り。外部サービス、実データ、deploy、migration apply
を必要としない。

```sh
uv run pytest -q tests/test_contracts.py
git diff --check
```

主な自動検証箇所:

- [tests/test_contracts.py](../../tests/test_contracts.py): v1 schemaの正常/拒否例、common `$ref`、UUID/owner/profile limit、5DB allowlist、DP/course、aggregate eligibility、digest、idempotency、latest pointer。
- [docs/contracts/examples](../../docs/contracts/examples): event/upload/Container/artifact/aggregate eligibility の正常・拒否fixture。
- [docs/contracts/semantic-rules.v1.json](../../docs/contracts/semantic-rules.v1.json): JCS/SHA-256、owner chain、digest chain、latest CAS、aggregate gate の機械可読規則。

`npm run check`、実Cloudflare resource、IR実機、Worker/DO/Container接続、OAuth/MCP、
実データ移行、production deployは #2 の文書契約だけでは受入できず、依存する後続issueの
検証範囲とする。現在の共有作業ツリーには他issueの未コミット変更があるため、Phase 0の
検証結果は上記の専用契約テストと差分検査に限定して記録する。

## 未完了・依存

- #2 は OPEN。issue close、commit、push、PR、deployはこの監査では行わない。
- schema/fixtureの存在は runtime validator、D1/DO/R2 partition、Queue/Workflow retry、
  実際のaggregate queryの受入を代替しない。
- 後続Phaseで実装した境界は、#2 の schema version・semantic rules・owner/trust partition
  を変更する場合にこの索引とADRを更新してから受入する。
