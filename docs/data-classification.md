# データ分類・保持

| 分類 | 例 | owner | 保存・許可用途 | 禁止・保持 / 削除 |
|---|---|---|---|---|
| C0 資格情報・秘密 | password/recovery/device token hash、OAuth token、Profile鍵、capability secret hash | Account / Service | D1のhash、暗号化secret store。認証・失効・鍵管理だけに使用 | 平文保存、ログ、MCP返却を禁止。削除確定時に失効・鍵破棄 |
| P1 アカウント・プロフィール | user ID、任意email、timezone、同意、readiness | Account | D1 / Profile DO。本人のWeb操作と許可scope内MCP | 別Account参照と集合入力を禁止。Account/Profile削除まで |
| P2 生DB・提出物 | 5つのbeatoraja DB、upload manifest | Profile | Profile単位envelope encryptionのprivate R2。検証・欠落backfill・本人の再解析 | MCP、capability URL、集合学習への入力を禁止。Profile削除まで |
| P3 個人プレイ・派生 | PlayEvent、特徴量、個人model、推薦・AIジャーナル | Profile | Profile DO / private R2。個人推薦、期間要約、明示scopeの履歴 | Account越境とscope外返却を禁止。Profile削除まで |
| A1 匿名集合モデル | aggregate parameter、集計済み品質・privacy評価 | Service / 対象Profile群 | official aggregate store。privacy gate済み`live_ir`由来の派生recordだけで生成 | 生DB、5DB backfill、個人再識別可能なrowを禁止。既生成parameterは個人削除後も保持可 |
| O1 運用・監査 | request/event/job digest、reason、status、latency、IP/User-Agent | Service | D1 / security log。abuse調査、retry、削除証跡 | payload、token、email、秘密URLを禁止。IP/User-Agent 30日、監査行1年 |
| B1 Backup | D1/DO/R2の暗号化backup | Service custody / 元owner | 別private R2。復元訓練と障害復旧 | 最大30日。削除確定時にProfile鍵を破棄して即時読不能化 |
| E1 Export | 正規化履歴、設定、推薦・model履歴、AIジャーナル、同意、manifest/digest | Account / Profile | 本人が明示要求したauthenticated download | 元5DB、資格情報、他ownerデータを含めず、元データの保持期間を延長しない |

superseded成果物本体は90日、manifest/digestはProfile削除まで保持する。失敗Jobはpayloadを持たない診断だけ30日保持する。

集合入力は品質ゲート済みlive IRだけ。eligibilityは`pending/eligible/ineligible/revoked`、reason code、policy versionで管理し、client booleanを信用しない。公開gateは100 Profile以上、1 Profile寄与1%以下、membership inference AUCの95%上限0.55以下、TPR@1%FPR 5%以下、既知record完全抽出0件の全条件を満たす必要がある。
