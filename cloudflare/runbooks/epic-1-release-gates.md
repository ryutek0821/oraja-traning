# #1 外部公開・デプロイ・launch runbook

この文書は、親Epic #1 の監査時点（2026-08-11）での**承認用手順**である。
ここに書かれた deploy、migration、実データ移行、GitHub の改名/public化、一般登録の
操作は、このissueの作業では実行しない。実行者は対象issueと環境の明示承認を取得し、
各チェックの証跡を deployment record に残す。

## 0. 実行権限と停止条件

- #2〜#20 の受入、#21 の招待β30日 go 判定、#22 のOSS公開gateを先に確認する。
- production、GitHub改名/public化、一般登録開始、Release公開は #23 の個別承認なしに
  実行しない。
- 未コミット変更、未承認migration、未確認backup、秘密値のログ出力が一つでもあれば停止する。
- 実データを preview に投入しない。staging/production は profile owner、保持期間、
  削除窓口、監視担当が記録されている場合だけ進める。
- 重大な越境、欠落、秘密漏えい、復元不能、SLO逸脱を検知した場合は新規登録・IR・uploadを
  止め、既存の表readを維持できる範囲で rollback に移る。

## 1. 承認前の読み取り専用確認

リポジトリと成果物の同一性を確認する。以下は確認コマンドの例であり、監査中に
production操作を起動するものではない。

```sh
git status --short --branch
git rev-parse HEAD
git diff --check
pytest -q
cd cloudflare
npm ci
npm run check
npm run typecheck
npm run deploy:preview:dry
npm run deploy:staging:dry
```

次も記録する。

- source commit/tag、Python/Worker/Container/IR のartifact checksum、SBOM
- 対象環境、Worker version、D1 migration一覧、binding名、Queue/Workflow名
- 直近のD1/DO/R2暗号化backup IDと、隔離環境でのrestore drill結果
- secretは**値ではなく**secret名、version、rotation担当だけ
- #20 のSLO/error budget、security scan、越境negative suiteの結果

`cloudflare/scripts/check-config.mjs` が検査する環境名・binding・R2/Queue prefix・origin
allowlistを変更する場合は、変更理由と承認者を記録する。`example.invalid` のoriginや
local build versionを実環境の値として使わない。

## 2. 環境ごとの適用順

### Preview（検証専用）

1. preview専用のD1/DO/R2/Queue/Workflow/Container resourceを確認する。
2. 番号順のD1 migrationを適用する。適用済みSQLを編集しない。
3. Worker、DO、Container、Workflowを同一releaseでdeployする。
4. `/healthz`、`/version`、authのnegative test、契約schema、binding隔離を確認する。
5. 失敗時は preview resource を削除せず、まず `preview:cleanup:plan` でレビュー用計画を出す。
   削除は別の明示承認が必要である。

### Staging（β準備）

1. previewの証跡、#20 の脅威モデル/SLO、#21 のβ計画を承認する。
2. staging backupを取得してから、forward migrationを適用する。
3. Worker/DO/Container/Workflowをrolling切替し、versionとread/write smokeを確認する。
4. 招待制・profile上限1・rate limit・削除窓口を有効化する。
5. 合成fixtureで初回5DB、変更なし月次、重複/逆順Queue、export/delete/restore、OAuth/MCPを
   実行し、実データを投入する前にgo/no-goを記録する。

### 招待β

1. #21 の暗号化backup/rollback pointを取得する。
2. 移行対象のlocal `assistant.db` と5DBの照合reportを作り、欠落・変換不能を承認する。
3. 実機IR、offline/restart/retry、5DB upload、2表生成、MCP、AI journal、削除/復元を
   日次記録する。
4. 30日連続、重大な欠落/越境0件、重複play 0件、SLO達成、復元成功、日韓手順の第三者再現を
   確認して #21 のgo判定を残す。

### Production / launch（#23専管）

次の順序をすべて満たした後に限る。

1. #21/#22 のgo判定、未解決重大issue 0件、最終backup/restore確認を再確認する。
2. release tag、source/JAR/Container checksum、SBOM、privacy/terms/security URLを固定する。
3. production migrationを承認済み順序で適用し、Worker/DO/Container/Workflowを切り替える。
4. `/healthz`、`/version`、匿名source/license、登録→IR→表取得の最小導線を確認する。
5. #23 の承認が**別々に**そろった場合のみ、GitHub改名、visibility変更、Release公開、
   invite-only解除、一般登録開始を行う。
6. launch後24時間/7日間、Queue lag、error、SLO、security alert、登録/IR/upload件数を
   個人データを含めず監視する。

## 3. Production deploy の保護

production deployは `cloudflare/scripts/guarded-deploy.mjs production` を入口とし、
`ORaja_PRODUCTION_DEPLOY_APPROVED=true`、`CLOUDFLARE_API_TOKEN`、
`CLOUDFLARE_ACCOUNT_ID` を承認済み環境からだけ供給する。値をshell、source、監査記録、
ログへ貼り付けない。production secretはpreview/stagingと別値にする。

このguardはdeploy許可だけを確認し、migrationの互換性、backup、go/no-goを代替しない。
それらはこのrunbookの前段で人間が確認する。

## 4. Rollback / incident

1. incident ID、発生時刻、release、migration、job/event/revisionを記録する。payload、token、
   email、capability secret、local pathは記録しない。
2. 新規登録・IR・upload・生成jobを止め、read-onlyのhealth/versionと既存immutable表readを
   維持する。
3. `cloudflare/runbooks/migration-rollback.md` に従い、承認済みbackupへ復元し、直前の承認済み
   Worker revisionへ切り替える。
4. 適用済みmigrationを編集・削除しない。復元後に必要ならforward repair migrationを作る。
5. D1/DO/R2の整合性、latest pointerの単調性、token/capability失効、削除状態を隔離環境で
   検証してから段階的にwriteを戻す。
6. 秘密漏えいまたは越境が疑われる場合は、該当secret/token/capability/OAuth grantを失効・
   rotateし、#20 のincident/data-breach手順へ引き継ぐ。

GitHubのvisibility変更、Release公開、一般登録の解除はサービスrollbackとは別の外部操作で
あり、#23 の承認なしに自動で戻さない。

## 5. Deployment record

```text
record_id:
issue_approval:
environment:
source_commit_or_tag:
worker_version:
migrations:
bindings_verified:
backup_ids:
restore_drill:
artifact_checksums:
sbom:
smoke_and_negative_tests:
slo_snapshot:
operator:
approver:
started_at:
completed_at:
rollback_point:
notes_without_protected_data:
```

監査時点では、上記のproduction値・backup ID・実データ移行結果・外部承認は存在しない。
