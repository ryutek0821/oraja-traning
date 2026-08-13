# API・認証・エラー契約

JSON Schema 2020-12がwire正本。未公開draft v1との互換は維持せず置換する。

## 入口・認証・retry

ownerは常に認証済みcredentialから完全なowner鎖を解決する。IRは`Device → Profile → Account`、Web/5DBはsessionまたはupload grantから`Profile → Account`を解決し、request body、path、R2 object key内のIDは認可根拠にしない。

| 入口 | 認証・owner解決 | 成功 | client retry |
|---|---|---|---|
| `POST /v1/ir/events` | Profile/Device限定256-bit bearer token（server保存はhashのみ） | 新規`202`、同一再送`200` | `408`、`425`、`429`、`5xx`だけ同じ`event_id`とcanonical payloadで再送 |
| 5DB upload | Web sessionまたはProfile固定のupload grant | manifest受理`202` | 一時失敗だけ同じmanifest digestで再開 |
| Web/API | Secure、HttpOnly、SameSite、`__Host-` session cookie。状態変更はCSRF検証 | endpointごとの`2xx` | safe methodまたは明示的にretryableな応答だけ |
| capability table URL | recommend/today別の256-bit secret。D1保存はhashのみ | immutable artifact`200` | `GET`は安全に再送可。失効・rotation後の旧URLは`404` |
| MCP | OAuth Authorization Code + PKCE。scopeとAccount/Profile owner鎖を照合 | scope内resource/tool応答 | access token失効時は再認証。write toolはidempotency契約がある場合だけ再送 |

## IR ingest

`POST /v1/ir/events` は`ir-submission.v1`を受ける。ownerは256-bit device token（hash保存）から解決し、body中のowner/provenance等はunknown fieldとして拒否する。

Profile DOがPlayEvent、Job、revision、outbox、Alarmを原子的に保存すると`202 Accepted`、同じevent ID・同じcanonical payload/owner/contract versionは既存Jobを`200`、同じIDの競合は`409`とする。成功時は`Location: /v1/jobs/{job_id}`と`Retry-After`を返す。Jobは`queued/running/succeeded/failed`をpollできる。

QueueはAlarmから冪等送信する。一時失敗は指数backoff+jitterで5回、その後DLQ。同じJob IDで運営再実行する。

## Problem Details

全JSON errorはRFC 9457 `application/problem+json`を使い、標準memberに`code`、`trace_id`、`retryable`を追加する。payload、token、email、秘密URLは含めない。

```json
{
  "type": "https://oraja-training.dev/problems/idempotency-conflict",
  "title": "Idempotency conflict",
  "status": 409,
  "code": "idempotency_conflict",
  "trace_id": "018f0f0f-0f11-7f0f-8f0f-0f0f0f0f0f0f",
  "retryable": false
}
```

公開後の破壊的変更は即時切替とし、旧clientへ`426`と`minimum_client_version`、`update_url`を返す。clientは再送を止める。

| HTTP | `code`例 | retry |
|---:|---|---|
| 400 / 415 / 422 | `invalid_contract`、`unsupported_media`、`unsupported_game_mode` | no |
| 401 / 403 / 404 | `unauthorized`、`insufficient_scope`、`not_found` | no。必要なら再認証 |
| 409 | `idempotency_conflict`、`profile_limit_reached` | no |
| 413 | `payload_too_large` | no |
| 426 | `client_upgrade_required` | no。client更新後に新契約で送信 |
| 408 / 425 / 429 | `request_timeout`、`too_early`、`rate_limited` | yes。`Retry-After`に従う |
| 500 / 502 / 503 / 504 | `temporary_unavailable` | yes。同一idempotency keyで再送 |

## Client spool

未送信は最大10,000件・90日。ACK済みは即削除する。上限到達時は新規spoolを止めて警告する。恒久拒否payloadは削除し、理由だけ30日保持する。course結果は通信・spoolせず成功扱いにし、機密情報を含まない診断だけ残す。

## 5DB

提出中はbeatoraja停止を必須とする。`score.db`、`scoredatalog.db`、`scorelog.db`、`songdata.db`、`songinfo.db`だけを許可し、WAL/SHMを拒否する。各2GiB、合計5GiBが上限。IRを履歴正本とし、厳密fingerprintで欠落だけbackfill、競合はIR優先で監査する。一式は1 Job・1 recommendation revision。

## Authentication

- Account user ID: lowercase正規化、3〜32文字。一意性は正規化後の値で判定する。
- Password: 12〜128文字。Argon2id hashだけを保存する。
- Recovery: 10個の単回復旧コードを発行し、server保存はhashのみ。使用済みcodeは再利用不可。
- Email/reset: emailは任意かつ確認済み状態を区別する。reset tokenはhash保存、単回使用、15分有効。
- IR: 256-bit token、hash保存、個別失効、期限なし、90日未使用で失効。
- Web: Secure/HttpOnly/SameSite/`__Host-` cookie、30日idle、重要操作は15分以内再認証。
- MCP: access 1時間、rotating refresh 30日。
- 表URL: recommend/today別256-bit secret、hash保存、失効後旧URLは即404。

別ownerで存在するIDは404とする。重要な規約/privacy再同意待ちはread/export/deleteだけを許可する。

## 監査イベント

監査行はtimestamp、actor/owner、request ID、対象ID、input digest、reason code、statusだけを持つ。payload本文、token、email、秘密URLは保存しない。

```text
account.created / account.login_failed / account.delete_requested / account.delete_cancelled
profile.created / profile.suspended / profile.deleted
device.created / device.revoked
consent.accepted / consent.reconsent_required
play.accepted / play.duplicate / play.rejected
upload.accepted / upload.rejected
job.queued / job.retry / job.succeeded / job.failed / job.replayed
revision.ready / revision.published / revision.superseded / revision.rollback_published
capability.created / capability.rotated / capability.revoked
oauth.authorized / oauth.denied / mcp.tool_called / mcp.denied
advisor.proposed / advisor.approved / advisor.rejected
deletion.started / deletion.completed / backup.expired
```

拒否イベントはreason codeとinput digestだけを残す。Jobの運営再実行、latest pointer更新、削除開始・完了、Profile鍵破棄、backup expiryは必ず監査する。
