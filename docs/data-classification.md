# データ分類と保持

分類は機密性と許可された処理の両方を表す。保存場所の prefix と `trust_domain` は分類の一部であり、同じ JSON shape でも境界をまたいで再利用しない。

| 分類 | 例 | owner | 保存 | 許可 | 禁止 / 保持 |
|---|---|---|---|---|---|
| C0 資格情報・秘密 | password hash、recovery hash、device token hash、OAuth token | Account / service | D1（hash）または暗号化 secret store | 認証、失効、監査 | 平文保存・ログ・MCP返却。削除確定で即時失効 |
| P1 アカウント・プロフィール | user ID、任意 email、timezone、同意、readiness | Account | D1、profile DO | 本人のWeb/MCP scope内 read、設定 | 別account read、aggregate input。削除確定で削除 |
| P2 生DB・提出物 | 5つの beatoraja DB、path/titleを含み得る snapshot | Profile | profile専用 key の envelope-encrypted private R2 | Containerの検証・正規化、本人の再解析 | MCP、公開URL、集合モデル、self-hosted→official移送。profile削除で削除、backup最大30日 |
| P3 個人プレイ・派生 | `PlayEvent`、normalized play、features、model state、推薦理由 | Profile | profile DO / private R2 | 本人の推薦・期間要約、明示 scope の履歴 | account越境、raw exportの代用、同意なし集合利用。削除 policyに従う |
| A1 匿名集合モデル | aggregate parameter、集計済み評価指標 | service / no individual owner | official aggregate store | `official`、同意、eligible、SP7 の derived inputのみ | self-hosted混入、個人再識別可能な row、raw event保存。既生成 parameter は個人削除後も保持可 |
| O1 運用・監査 | request ID、event/job hash、reason、status、latency | service | D1/ログ | abuse調査、retry、削除証跡 | payload本文、token、秘密。最小限の保持期間を設定 |

暗号化 backup は元データの分類・owner・`trust_domain` を継承し、別 bucket に置く。backup は本番の read path や MCP の capability URL から参照せず、削除確定後も復元可能性を最大30日以内に限定する。復元時も owner、profile prefix、集合学習 gate を再検証する。

## 公式集合学習のゲート

集合学習に入る record は次の条件をすべて満たす必要がある。条件は query の暗黙の前提にせず、normalization output と aggregate input の両方で検証する。

```text
trust_domain == "official"
AND aggregate_eligible == true
AND account_consent.aggregate_training == true
AND game_mode == "SP7"
AND is_course == false
AND source_kind in ("official_ir", "official_normalized")
```

IR client event は `aggregate_eligible=false` だけを送信でき、Worker が発行した `aggregate-eligibility.v1` decision がない record は集合入力に変換しない。`self_hosted` は decision 自体を発行できず、`aggregate_eligible=false` を schema で強制するため、official aggregate partition の入力型に変換できない。既存のローカル `assistant.db` も self-hosted として扱い、#1 の「公式 Cloudflare 環境で受理したデータだけ」を満たさない限り集合へ送らない。
