# Phase 1 回帰基準とコア分離 — 進捗索引

監査日: 2026-08-11

この文書は、Phase issue [#25](https://github.com/ryutek0821/oraja-traning/issues/25) の
作業ツリー上の成果物と検証可能範囲を索引化する。対象は子issue [#3](https://github.com/ryutek0821/oraja-traning/issues/3)
（合成fixture / golden）と [#4](https://github.com/ryutek0821/oraja-traning/issues/4)
（Python domain core / SQLite adapter）に限る。

GitHub上のissue本文は `gh issue view` で確認した。#25、#3、#4はいずれもOPENで、
本文のチェックボックスも未同期である。この監査ではissue状態、commit、push、PR、deploy、
外部リソースを変更しない。

## 依存関係

```text
#1
└── #25 Phase 1
    ├── #3 synthetic fixture / golden   (依存: #2)
    └── #4 domain core / adapter        (依存: #2, #3)
```

## #3 成果物索引

| 役割 | 成果物 | 確認内容 |
| --- | --- | --- |
| 5DB builder | [`tests/fixtures/synthetic_beatoraja.py`](../tests/fixtures/synthetic_beatoraja.py) | `score.db`、`scoredatalog.db`、`scorelog.db`、`songdata.db`、`songinfo.db`を一時ディレクトリへ生成。fixture v2、必須ケース、SP7 catalog、異常variantを定義 |
| fixture契約 | [`tests/fixtures/README.md`](../tests/fixtures/README.md) | baselineとWAL/SHM/journal・破損SQLite・途中更新・カウンター巻戻りvariantの分離、公開安全条件を記載 |
| 56ケース基準 | [`tests/golden/collection_manifest.json`](../tests/golden/collection_manifest.json) | 42 test functions、parametrize 16 cases、合計56 collected casesを固定 |
| backfill/snapshot golden | [`tests/golden/synthetic_backfill.json`](../tests/golden/synthetic_backfill.json) | 正規化前段の件数、IR NoPlay除外、gauge、snapshot差分、累積値を固定 |
| core/menu golden | [`tests/golden/synthetic_outputs.json`](../tests/golden/synthetic_outputs.json) | 正規化play列、推薦候補、Daily Menu 100,000 + RESERVE 10,000、model stateを固定 |
| 更新手順 | [`tests/golden/update_synthetic.py`](../tests/golden/update_synthetic.py)、[`tests/golden/README.md`](../tests/golden/README.md) | goldenの生成・check・レビュー条件を定義。数値だけの手編集は禁止 |
| 回帰テスト | [`tests/test_synthetic_fixture.py`](../tests/test_synthetic_fixture.py)、[`tests/test_golden.py`](../tests/test_golden.py) | manifest、catalog ownership/hash edge、sidecar、破損/途中更新/巻戻り、privacy、golden budgetを検証 |
| pytest接続 | [`tests/conftest.py`](../tests/conftest.py) | 実DBではなく合成builderをsession fixtureとして利用し、DBとsidecarの不変性を検査 |

### #3 受入条件の監査

| 受入条件 | 現在の判定 | 証跡 |
| --- | --- | --- |
| 既存56ケースを維持 | 確認済み | `test_collection_manifest_freezes_the_original_56_cases`、`collection_manifest.json` |
| 実DBなしで再現 | 確認済み | `tests/conftest.py` のsynthetic fixture、fixture/golden/backfill/snapshotテスト |
| 推薦候補と100,000 + 10,000のgolden | 部分確認 | `test_golden_covers_normalized_plays_and_daily_budget` は通過。ただし下記のcommitted golden一致は未通過 |
| 秘密情報・個人識別子・BMS本体なし | 確認済み | `test_fixture_and_golden_assets_contain_no_machine_or_personal_paths`、manifest privacy |
| 異常系とSP7/hash境界を再現 | 確認済み | sidecar 3種、破損SQLite、partial update、counter rollback、SP7 ownership、MD5衝突、SHA-256欠落のテスト |

## #4 成果物索引

| 役割 | 成果物 | 確認内容 |
| --- | --- | --- |
| domain values | [`src/oraja_training/domain/types.py`](../src/oraja_training/domain/types.py) | `ProfileContext`、`Play`、`Chart`、`Observation`、`ModelSnapshot`、`RecommendationInput/Output`をimmutable valueとして定義 |
| ports / errors | [`src/oraja_training/domain/ports.py`](../src/oraja_training/domain/ports.py)、[`src/oraja_training/domain/errors.py`](../src/oraja_training/domain/errors.py) | Feature/Model/Recommendation Repository、UnitOfWork protocolと共通例外階層を定義 |
| pure core | [`src/oraja_training/collect/normalize.py`](../src/oraja_training/collect/normalize.py)、[`src/oraja_training/features/build.py`](../src/oraja_training/features/build.py)、[`src/oraja_training/model/core.py`](../src/oraja_training/model/core.py)、[`src/oraja_training/plan/menu.py`](../src/oraja_training/plan/menu.py) | Mapping/domain値から正規化・feature・model・推薦を計算し、core経路でSQLiteを開かない |
| local adapters | [`src/oraja_training/db/feature_adapter.py`](../src/oraja_training/db/feature_adapter.py)、[`src/oraja_training/db/model_adapter.py`](../src/oraja_training/db/model_adapter.py)、[`src/oraja_training/db/recommendation_adapter.py`](../src/oraja_training/db/recommendation_adapter.py) | SQLite schemaとの読み書きをadapterへ隔離 |
| compatibility | [`src/oraja_training/cli.py`](../src/oraja_training/cli.py)、旧API shim | 既存CLIとlocal SQLite呼び出しをcore/adapterへ委譲 |
| 公開境界 | [`docs/core-api.md`](core-api.md) | core entry point、repository port、profile/seed規則、adapter実装契約を文書化 |
| 回帰テスト | [`tests/test_core_separation.py`](../tests/test_core_separation.py)、[`tests/test_domain_core.py`](../tests/test_domain_core.py) | sqlite import境界、domain変換、profile表示名とseed、repository transaction、predictionのstorage independenceを検証 |

### #4 受入条件の監査

| 受入条件 | 現在の判定 | 証跡 |
| --- | --- | --- |
| domain coreから`sqlite3` importを除去 | 確認済み | `test_core_modules_do_not_import_sqlite` |
| local SQLite adapterでfixture/goldenを通す | 部分確認 | fixture、backfill、snapshot、core separationは通過。Daily Menuのcommitted golden一致だけ未通過 |
| profile ID/display nameを変えても個人値がseedへ混入しない | 確認済み | `test_profile_display_name_does_not_change_seed`、`ProfileContext.deterministic_namespace` |
| 同一入力・versionで決定的な成果物 | 部分確認 | [`tests/test_menu.py`](../tests/test_menu.py) のdeterminismテストは通過。固定goldenとの一致は下記差分解消後に再確認 |

## 検証記録

作業ツリー上で次を実行した。

| コマンド | 結果 |
| --- | --- |
| `env UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run pytest -q tests/test_synthetic_fixture.py tests/test_backfill.py tests/test_snapshot.py tests/test_core_separation.py tests/test_domain_core.py` | 成功 |
| `env UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run pytest -q` | 1 failure。`tests/test_golden.py::test_synthetic_outputs_match_committed_golden`のみ失敗し、他は通過 |
| `env UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run python tests/golden/update_synthetic.py --check` | 失敗。`synthetic_outputs.json is stale` |
| `git diff --check` | 成功 |

### 未完了の引き継ぎ

現行の共有変更では、生成されたDaily Menuのseedが `8050c556ade31935`、保存済みgoldenのseedが
`ee2aa62706087a7a` となっており、`daily_menu`全体が一致しない。これは#25の文書監査で
goldenを手編集・更新する範囲ではないため、実装側の意図確認なしに変更しない。

再開時は次の順で確認する。

1. menu/core側の変更が意図した仕様変更か、#3のbaseline回帰かを担当者が判断する。
2. 仕様変更を採用する場合だけ、[`tests/golden/README.md`](../tests/golden/README.md) の手順に従って `update_synthetic.py --write` を実行し、diffをレビューする。
3. 仕様変更を採用しない場合は、goldenではなくmenu/core側の変更を修正する。
4. `--check`、Phase対象pytest、全体pytest、`git diff --check`を再実行する。

この判断とgolden更新は未実施であり、本監査の完了範囲外である。デプロイ、サービス再起動、
D1/R2変更、GitHub issue操作は発生していない。
