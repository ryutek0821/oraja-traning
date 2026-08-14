# #28 Phase 4 OAuth・MCP・AI共有記憶索引

対象は #17〜#19。実装境界と検証入口を次に固定する。

| issue | 成果物 | 主要境界 |
|---|---|---|
| #17 OAuth | `worker/src/oauth.ts`、`migrations/0008_oauth_advisor.sql` | Authorization Code + PKCE、discovery、client登録、scope付きtoken |
| #18 MCP | `worker/src/mcp.ts` | `/mcp`、Resources/Tools、明示scope、生DB非公開 |
| #19 Advisor | `worker/src/advisor.ts` | pending既定、Web承認/却下後だけ共有journalへ反映 |

`worker/src/index.ts` がOAuth discovery/token、`/mcp`、advisor routeを接続する。`test/foundation.test.mjs` は全実装済みservice boundaryが入口から到達可能であることを検査する。

ローカルgateは `npm test`、`npm run lint`、`npm run typecheck`、`npm run check`。複数Remote MCP client、実Origin、token audience、OAuth同意UI、失効・rotationはpreview/stagingの接続試験で受入する。本索引はdeployや外部client登録を行わない。
