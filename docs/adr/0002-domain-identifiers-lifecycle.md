# ADR-0002: ドメイン ID、所有権、ライフサイクル

- Status: Accepted
- Date: 2026-08-11
- Scope: issue #2 / Phase 0

## Decision

- `Account`、`Profile`、`Device`、`PlayEvent`、`Job`、`RecommendationVersion` はすべて UUIDv7 を使う。
- service-side ID は URL の意味付けや認可に使わず、parent owner row を照合する。
- Account は Profile の親、Profile は Device/PlayEvent/Job/RecommendationVersion の親である。
- 初期設定 `PROFILE_LIMIT=1` はドメイン制約として扱い、active/pending_delete profile の予約を account slot transaction で直列化する。
- password、recovery code、device token、OAuth token は平文を保存しない。token は hash、必要な暗号化 secret は別 key hierarchy で保護する。
- deletion は7日取消 window とし、確定後の backup は最大30日で失効する。匿名集合モデルの既生成 parameter は個人データへ逆参照できない限り保持する。

## Lifecycle rules

受理・更新・削除は [domain-entities.md](../contracts/domain-entities.md) の表を正とする。削除中の entity は新しい play/upload/job を受理せず、取消だけが許可される。PlayEvent と artifact の payload は append-only/immutable で、状態や pointer を別レコードにする。

## Consequences

- 現行 local `assistant.db` の integer/autoincrement ID は、この issue で強制変換しない。Phase 1 の adapter が local ID と service UUID の対応を持つ。
- 既存ローカル利用の個人固定値を変更する実装は後続 issue の対象であり、この ADR の採用だけでは挙動が変わらない。
