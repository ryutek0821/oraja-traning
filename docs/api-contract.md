# API・認証・エラー・監査契約

これはサービス層の wire contract であり、現行の `serve/app.py` の localhost API をこの issue で変更するものではない。

## 認証方式

| 入口 | 認証 | 必須 scope | owner の決定 |
|---|---|---|---|
| IR `POST /v1/plays` | profile-scoped bearer device token（hashのみ保存） | `plays:write` | tokenの `device_id → profile_id → account_id` |
| Web/API | Secure + HttpOnly session cookie、CSRF token | endpointごとの session permission | session subject |
| 5DB upload | Web session または upload grant | `uploads:write` | grant発行時に固定した profile |
| capability table URL | 長期 random secret URL、任意失効 | read-only、URL rotationで旧URL即404 | secret recordの profile |
| Remote MCP `/mcp` | OAuth 2.1 Authorization Code + PKCE | `profile:read` 等の明示 scope | OAuth subject + grant |

request body、path segment、R2 object keyに含まれる `account_id/profile_id` は認可情報ではない。別ownerで存在するIDは、情報漏えいを避けるため原則404を返す。

## 初期エンドポイント契約

| Method | Path | request | 成功応答 | retry |
|---|---|---|---|---|
| POST | `/v1/plays` | `ir-event.v1` | 新規 `202`、同一再送 `200`。ACK後に async job | 408/425/429/5xxのみ同じ event_id で |
| POST | `/v1/uploads` | `upload-manifest.v1` + pre-signed part | 新規 `202` と `job_id` | 一時失敗のみ同じ manifest digest で |
| GET | `/t/{secret}/recommend/header.json` | capability secret | immutable artifact `200` | GETは安全に再試行可 |
| GET | `/t/{secret}/today/header.json` | capability secret | immutable artifact `200` | GETは安全に再試行可 |
| GET | `/mcp` | OAuth access token | MCP discovery/stream | token失効時は再認証 |

### ACK の意味

`202` は durable idempotency record と Queue/Workflow の投入までを意味する。生成版の ready、model update、latest pointer の切替は含まない。レスポンス例:

```json
{
  "status": "accepted",
  "event_id": "018f0f0f-0f0f-7f0f-8f0f-0f0f0f0f0f0f",
  "idempotency_key": "018f0f0f-0f0f-7f0f-8f0f-0f0f0f0f0f0f",
  "job_id": "018f0f0f-0f10-7f0f-8f0f-0f0f0f0f0f0f",
  "retryable": false
}
```

同じ key と同じ canonical payload の再送は `200 {"status":"duplicate", ...}` とする。key が同じで payload、owner、scope、contract version のいずれかが違う場合は `409 idempotency_conflict` とし、新しい effect を作らない。

## エラー形式

すべての JSON API error は次の shape を使う。`details` に raw DB 行、token、email、秘密 URLを入れない。

```json
{
  "error": {
    "code": "unsupported_game_mode",
    "message": "この契約は SP7 のみ受け付けます",
    "request_id": "018f0f0f-0f11-7f0f-8f0f-0f0f0f0f0f0f",
    "retryable": false,
    "retry_after_seconds": null
  }
}
```

| HTTP | code例 | retry | 用途 |
|---:|---|---|---|
| 400 | `invalid_json`, `invalid_contract` | no | JSON/schema不正 |
| 401 | `unauthorized` | no（再認証） | token/session不正 |
| 403 | `insufficient_scope`, `profile_suspended` | no | 認可不足 |
| 404 | `not_found` | no | 所有者越境を隠す |
| 409 | `idempotency_conflict`, `profile_limit_reached` | no | state/payload conflict |
| 413 | `payload_too_large` | no | file/request size超過 |
| 415/422 | `unsupported_media`, `unsupported_game_mode`, `disallowed_field` | no | 初期範囲外・allowlist外 |
| 429 | `rate_limited` | yes | `Retry-After` 秒を付ける |
| 500/502/503/504 | `temporary_unavailable` | yes | server/queue/container一時障害 |

## 監査イベント

監査行は payload 本文でなく hash、owner、request ID、reason、status のみを持つ。

```text
account.created / account.login_failed / account.delete_requested / account.delete_cancelled
profile.created / profile.suspended / profile.deleted
device.created / device.revoked
play.accepted / play.duplicate / play.rejected
upload.accepted / upload.rejected
job.queued / job.retry / job.succeeded / job.failed
revision.ready / revision.published / revision.superseded
capability.created / capability.rotated / capability.revoked
mcp.authorized / mcp.denied / advisor.proposed / advisor.approved
```

`play.rejected` と `upload.rejected` には `reason_code` と input digest を残し、拒否した raw body を保存しない。削除処理の開始・完了・backup expiry も監査する。

## 初期範囲外の拒否

- `game_mode != SP7`、DP/PMS、未知の `rule` は入口と Container で拒否する。
- BMS本体、replay `keyinput`、任意 `values`、rival情報、IR ranking、course ranking は IR event/MCP resource の入力・出力に含めない。
- `is_course=true` は個人履歴へ受理できるが、通常曲モデル・公式集合モデルへの入力から除外する。
- MCP は raw 5DB/R2 object を返さない。詳細履歴は `plays:read` を持つ明示 tool call と paging が必要。
