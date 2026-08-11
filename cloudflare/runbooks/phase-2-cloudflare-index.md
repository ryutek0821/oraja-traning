# #26 Phase 2 Cloudflare データ基盤・監査索引

監査日: 2026-08-11。これは #26 のローカル準備記録であり、子issueの完了・GitHub
issueの更新・Cloudflareのデプロイを意味しない。GitHub上の子issue状態と、各本文の
依存・完了条件を正とする。

## 進捗監査

| issue | 依存 | 作業ツリーで確認した成果物 | 監査判定 / 未完了条件 |
|---|---|---|---|
| [#5](https://github.com/ryutek0821/oraja-traning/issues/5) | #2 | `wrangler.jsonc`、CI/deploy workflow、config/migration scripts、deployment runbook | 基盤設定とhealth/version骨格はある。preview応答、binding隔離、空DB再構築、CI green、deploy証跡は未確認。 |
| [#6](https://github.com/ryutek0821/oraja-traning/issues/6) | #2、#5 | `0001_control_plane.sql`、`0002_auth.sql`、control-plane adapter、D1 migration tests | schema草案はある。`0007_tables.sql` と `0007_upload_protocol.sql` のprefix重複でmigration検査が停止し、空DB/前version適用と越境受入は未完了。 |
| [#7](https://github.com/ryutek0821/oraja-traning/issues/7) | #2、#5、#6 | `auth.ts`、`email.ts`、auth routes、認証runbook、contract tests | 認証骨格とlocal contract testはある。メール変更route wiring、Email binding/secret、terms seed、D1適用、preview受入は未完了。 |
| [#8](https://github.com/ryutek0821/oraja-traning/issues/8) | #2、#5、#6 | `profile-do.ts` のready/play-event受理 | 履歴・revisionの一部骨格に留まり、DO schema migration、export/restore、`deleteAll()`、tombstone、PITR、越境試験は未完了。 |
| [#9](https://github.com/ryutek0821/oraja-traning/issues/9) | #2、#5、#6、#7 | `upload-protocol.ts`、`upload-store.ts`、upload migration、manifest契約 | allowlist/multipart/envelope/dedupの実装草案はある。index route接続、mock R2受入、実R2/KEK、migration適用、upload smokeは未完了。 |
| [#10](https://github.com/ryutek0821/oraja-traning/issues/10) | #4、#5、#9 | Python container adapter、manifest検査、Container image、tests | local adapterとhealth imageはある。R2復号→core→artifact保存、Worker job wiring、同一golden、SBOM/vulnerability scan、preview Container受入は未完了。 |
| [#11](https://github.com/ryutek0821/oraja-traning/issues/11) | #6、#8、#9、#10 | `0005_workflow.sql`、job ledger、workflow state/pipeline | ledger/retry/CASの骨格はある。Queue/scheduled/status/cancelのWorker接続、実Container pipeline、100回再送・逆順・障害注入・p95試験は未完了。 |

2026-08-11 の `gh issue view` では #5〜#11 および #26 はすべて OPEN だった。
したがって、作業ツリーにファイルが存在することやlocal testが通ることだけでは、子issue
またはPhaseの完了とは判定しない。

## 外部deploy gate

次のrunbookを索引とする。いずれも手順・停止条件の定義であり、この監査では実行しない。

- [deployment.md](deployment.md): preview/staging/productionの手動deployとreviewer gate。
- [migration-rollback.md](migration-rollback.md): forward migration、backup、rollback。
- [preview-cleanup.md](preview-cleanup.md): preview resource cleanupの承認手順。
- [epic-1-release-gates.md](epic-1-release-gates.md): #1全体の受入、β、公開、production gate。
- [phase-0-contract-index.md](phase-0-contract-index.md): #2契約成果物とPhase 0の境界。

#5〜#11のpreview/staging受入には、環境別resource/binding、D1 migration、secret、
R2/Queue/Workflow/Container smokeの証跡が必要である。productionは、承認済みbackup、
restore drill、GitHub Environment reviewer、明示的なdeploy承認、deployment recordが
揃うまで停止する。実migration、実データ投入、secret設定、Cloudflare deployは未実施。

## ローカル検証記録

- `git diff --check`: 成功。
- `cd cloudflare && npm run lint`: 成功。
- `cd cloudflare && npm run typecheck`: 成功。
- `uv run pytest -q`: 1件失敗。`tests/test_golden.py` の `daily_menu` golden差分。
- `cd cloudflare && npm test`: 7 passed / 2 failed。migration prefix `0007` 重複。
- `cd cloudflare && npm run check`: configは通過、同じmigration prefix重複で停止。

## 収束状態

今回の #26 作業では、既存workerの変更を戻さず、commit/push/PR/issue close、外部操作、
deployを行わない。上記の未完了項目は各子issueの実装・受入作業へ引き継ぐ。
