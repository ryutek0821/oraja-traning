# 認証運用・検証 runbook

Issue #7 の Worker 認証契約を記録する。認証情報、回復コード、メール token は
レスポンス以外へ平文で保存しない。回復コードは登録成功時に一度だけ表示し、
紛失時は別の回復コードまたは確認済みメールの reset を使う。

## ポリシー

- ユーザー ID は NFKC、trim、小文字化後の ASCII `[a-z0-9][a-z0-9_-]{2,31}`。
- パスワードは 12〜128 code points。Argon2id の parameters は
  `AUTH_POLICY.passwordParamsVersion` とともに保存し、version 不一致または
  `password_rehash_required=1` の成功 login で再 hash する。
- 登録では利用規約とプライバシー規約の公開済み version を両方指定し、同意行を
  profile に紐付けて同じ D1 batch に保存する。version は `terms_versions` に先に
  seed する。
- 回復コードは 32-byte random の URL-safe 値を 10 個生成し、個別 salt と hash
  だけを D1 に保存する。使用時は compare-and-set で単回消費し、全 session を失効する。
- Session は 30 日 TTL、`__Host-` + `Secure` + `HttpOnly` + `SameSite=Lax`。
  login、回復、reset 後は新しい token を発行し、rotation 前の token を失効する。
  state-changing API は session と別 cookie の CSRF token を要求する。
- login/register/reset/recovery は IP、アカウント、またはメール識別子の hash を
  rate-limit key として使う。失敗／reset request は 15 分窓で指数 backoff し、
  raw IP、user ID、メールアドレスは `auth_rate_limits` に保存しない。
- メールアドレスは任意。確認 token は 30 分、password reset token は 15 分で失効し、
  目的 (`verify` / `password_reset`) を DB query と update の両方で固定する。reset は
  `verified_at` があるメールだけを対象にし、新しい request は同じメールの古い reset
  token を失効する。
- メール変更は active session に紐付けて新しい確認 token を発行する。確認成功時に
  旧メールを失効し、`email_change_requested` と `email_changed` を監査する。route 側は
  `requestEmailChange` の前に通常の CSRF 検証を必ず行う。

## 外部 secret と Email binding

本番では Cloudflare Worker secret として `EMAIL_ENCRYPTION_KEY`（16 bytes 相当以上の
高 entropy 値）を設定する。これは Wrangler の `vars`、Git、監査ログへ置かない。
IP・ユーザーID・メールアドレスの検索／レート制限キーには、別の16 bytes以上の
`AUTH_HASH_PEPPER`を設定し、HMAC-SHA-256で不可逆化する。
任意メールを利用する環境では `AUTH_EMAIL` binding と `AUTH_EMAIL_FROM` も設定する。
`PUBLIC_ORIGIN` は HTTPS の固定 origin とし、メール本文には opaque link 以外の
アカウント ID、パスワード、回復コード、プレイ情報を含めない。

外部メール送信を伴うテストでは `AuthEmailSender` の mock を注入し、`send` に渡された
token をテスト内だけで受け取る。実際の SMTP／Cloudflare Email binding、実 secret、
本番宛先は使用しない。暗号化の round-trip は同じ mock secret、wrong secret、改ざん
ciphertext、短い IV の各ケースを検証する。

## 検証手順

```sh
cd cloudflare
npm run check
npm run typecheck
cd ..
uv run pytest -q tests/test_auth_contract.py tests/test_d1_migration.py
```

Preview での確認は、migration plan と dry-run のみを先に実行する。
実 D1 migration、Email binding 設定、secret provisioning、Worker のデプロイは承認済みの
release 作業でのみ行い、この issue のローカル検証には含めない。

最低限の negative test は次を含める。

1. ID の大文字／NFKC 衝突、3 未満・32 超の境界、password の 11／129 境界。
2. 同意なし、存在しない規約 version、未確認メールでの reset。
3. 期限切れ、再利用、purpose 違いの email token。
4. 同じ回復コードの二重使用、古い session cookie、CSRF token 不一致。
5. 失敗を連続させた login/register/reset の `429` と `Retry-After`。
6. DB／audit／メール本文に password、回復コード、token、raw email がないこと。

## 未接続の外部作業

`cloudflare/worker/src/index.ts` の route はここで定義した auth service を呼び出す。
Email binding、secret、規約 version の本番 seed、D1 migration 適用、Cloudflare 公開は
運用承認と環境資格情報が必要なため、runbook の手順だけを整備し、ローカル worker の
実装・テストでは実行しない。
