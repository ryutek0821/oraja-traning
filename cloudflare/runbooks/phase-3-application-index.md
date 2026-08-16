# #27 Phase 3 IR・生成・Web・データライフサイクル索引

対象は #12〜#16。実装の正本とローカル検証入口を次に固定する。

| issue | 成果物 | ローカル検証 |
|---|---|---|
| #12 IR API | `worker/src/ir-api.ts`、`profile-do.ts`、`migrations/0004_ir_api.sql` | `test/ir-api.test.mjs` |
| #13 IR JAR | `ir/` のIRConnection用source、spool・再送契約 | Gradle build、Worker契約検査 |
| #14 Tables | `worker/src/tables.ts`、`migrations/0007_tables.sql` | `test/tables.test.mjs` |
| #15 Web | `web/public/`、認証・profile/device操作route | Worker typecheck、browser smoke |
| #16 Privacy | `worker/src/privacy.ts`、export・削除猶予・backup契約 | Worker typecheck、隔離環境drill |

Worker入口 `worker/src/index.ts` は認証、device、play、upload、table、privacyを接続する。`npm test`、`npm run typecheck`、`npm run check` をPhaseのローカルgateとする。

実Cloudflare binding、Email、R2、Queue/Workflow/Container、削除・復元、性能SLOはpreview/stagingで別途受入する。本索引はdeploy、service restart、実データ移行を行わない。
