# ドメインエンティティ契約

すべてのサービス側エンティティ ID は RFC 9562 の UUIDv7（小文字）とする。時系列順に並べられるが、認可は ID の時刻や prefix ではなく所有者行で判定する。

| Entity | ID / owner | 保存境界 | lifecycle | 不変条件 |
|---|---|---|---|---|
| `Account` | `account_id` / account本人 | D1 | `pending → active → pending_delete → deleted` | password は Argon2id hash、復旧コード/token は hash のみ。削除申請は7日取消可能 |
| `Profile` | `profile_id` / parent `account_id` | D1 + profile DO/R2 prefix | `active → suspended → pending_delete → deleted` | 既定 `PROFILE_LIMIT=1`。active/pending_delete を1 account 1件だけ許す |
| `Device` | `device_id` / `profile_id` | D1 | `active → revoked` または `expired` | IR token は発行時のみ平文表示、保存は hash。scope は `plays:write` に限定 |
| `PlayEvent` | `event_id` / `profile_id` | profile DO、必要な audit hash は D1 | `received → accepted → normalized → model_excluded` | immutable。`event_id` が冪等キー。同じ key の payload 差替えは409 |
| `Job` | `job_id` / `profile_id` | D1 + Workflow | `queued → running → succeeded` または `failed/cancelled` | delivery は at-least-once。`idempotency_key` と input digest が同一なら一効果 |
| `RecommendationVersion` | `recommendation_version_id` / `profile_id` | profile DO + private R2 | `building → ready → superseded` | artifact は immutable。`revision` は profile/partition ごとに単調増加 |

## Account と Profile の制約

現在の設定値は次の通りである。

```text
PROFILE_LIMIT = 1
```

`PROFILE_LIMIT` は将来変更できる設定値として扱うが、初期版では account の profile slot を transaction 内で予約してから Profile を作成する。`active` または `pending_delete` の profile 数が limit 以上なら作成を拒否する。削除済み slot は再利用できる。API の事前 count だけに頼らず、account row の compare-and-set と slot の一意制約を同じ transaction で実行する。

```sql
-- 概念契約。D1/DO adapter が同じ原子性を実現する。
UPDATE account_slots
   SET used = used + 1
 WHERE account_id = :account_id
   AND used < :profile_limit;
-- changed_rows = 1 の時だけ Profile INSERT を続行する。
```

### 所有権の判定

- `account_id` は credential の subject から得る。request body、URL、R2 key の文字列から所有者を推定しない。
- `profile_id` は `account_id` に紐づくかを D1/DO で確認する。越境アクセスは存在有無を漏らさない404とする。
- `device_id` は1 profileに属し、device tokenの hash照合後にだけ event owner として採用する。
- `trust_domain` は `official` または `self_hosted` の immutable な provenance である。self-hosted record は official aggregate partition へ移動・コピーできない。

## ライフサイクル詳細

| Entity | 受理 | 更新 | 削除/取消 |
|---|---|---|---|
| Account | terms/privacy同意と認証情報が揃った時 | status/settings/audit のみ | `pending_delete` から7日以内は取消可能。確定後、D1/DO/R2/credentialを削除し、encrypted backupは最大30日で失効 |
| Profile | account slot の予約とともに作成 | 設定、event stream、latest pointer | pending中は新規 ingest を拒否。取消で active、確定後 `deleteAll()` とR2 prefix削除 |
| Device | token hash と scope の登録 | last_seen/audit のみ | revoke は即時。全 event ingest を拒否し、既存 PlayEvent は監査対象として保持 |
| PlayEvent | schema、owner、scope、event idempotency を検証後に durable accept | payload は変更不可。normalization status の append-only 遷移のみ | profile削除時に削除対象。reject は PlayEvent に昇格せず audit に hash と reason だけ残す |
| Job | input digest と idempotency key を確定して queue | retry count/status/attempt metadata | cancel は未実行分だけ。成功済み output は削除せず immutable とする |
| RecommendationVersion | output/artifact manifest の digest 検証後 `ready` | `latest` pointer は別行で比較更新 | superseded は読み取り可能。削除は profile deletion policy に従う |
