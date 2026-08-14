# #28 Phase 4 OAuth・MCP・AI共有記憶索引

対象は #17〜#19。実装境界と検証入口を次に固定する。

| issue | 成果物 | 主要境界 |
|---|---|---|
| #17 OAuth | `worker/src/oauth.ts`、`migrations/0008_oauth_advisor.sql`、`0009_oauth_token_lifecycle.sql`、`0011_oauth_security_boundaries.sql` | Authorization Code + PKCE、resource束縛、rotating refresh、grant失効、CIMD/DCR、日韓同意 |
| #18 MCP | `worker/src/mcp.ts` | `/mcp`、Resources/Tools、明示scope、生DB非公開 |
| #19 Advisor | `worker/src/advisor.ts` | pending既定、Web承認/却下後だけ共有journalへ反映 |

`worker/src/index.ts` がOAuth discovery/token、`/mcp`、advisor routeを接続する。`test/foundation.test.mjs` は全実装済みservice boundaryが入口から到達可能であることを検査する。

DCRは無制限公開しない。各環境で `OAUTH_DCR_INITIAL_ACCESS_TOKEN` をWorker secretとして設定し、登録要求は同値をBearer tokenで送る。CIMD client IDはHTTPS metadata URLそのものとし、redirectなし・JSON・32KiB以下・5秒timeout・1時間cacheで取得する。OAuth同意はGETで日韓画面を表示し、CSRF検証済みPOSTの明示許可後だけcodeを発行する。

ローカルgateは `npm test`、`npm run lint`、`npm run typecheck`、`npm run check`。複数Remote MCP client、実Origin、token audience、OAuth同意UI、失効・rotationはpreview/stagingの接続試験で受入する。本索引はdeployや外部client登録を行わない。
