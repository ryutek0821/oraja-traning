# #29 Phase 5 進捗・launch前チェック索引

監査日: 2026-08-11
対象: GitHub #29 と子issue #20〜#23
判定: Issue本文の完了条件、既存runbook、作業ツリーの証跡を読み取り専用で突合した。設計・設定・dry-runは、受入済みまたはデプロイ済みの証拠として扱わない。

この索引は現在のゲート状態を保存するための文書であり、公開・deploy・実データ移行・GitHub改名/public化・issue操作の承認記録ではない。各子issue本文と、外部操作の手順は次を正とする。

- [#20](https://github.com/ryutek0821/oraja-traning/issues/20)〜[#23](https://github.com/ryutek0821/oraja-traning/issues/23)
- [外部公開・deploy runbook](epic-1-release-gates.md)
- [deployment runbook](deployment.md)
- [PLAN.md の親Epic監査](../../PLAN.md)

## 1. Phase 5 の現在状態

GitHub Native Sub-issues は読み取り専用APIで確認し、#29 は #20〜#23 を子として参照している。4件とも OPEN であり、#29の完了条件である子issueの完了状態は未成立である。

| Issue | 現在状態 | 監査時点の証跡 | 完了判定 |
|---|---|---|---|
| #20 運用・SLO・脅威モデル | OPEN | 契約、データ分類、個別routeのrate limit、公開前runbookは存在する | 横断SLO、alert、redaction、越境negative suite、chaos、restore/delete drillの受入証跡なし |
| #21 招待β・移行・復元 | OPEN | βの実施順序と記録項目はrunbookにある | 招待運用、実データ移行、30日記録、実機IR/MCP接続、復元訓練、日韓第三者再現の証跡なし |
| #22 OSS・公開物 | OPEN | 合成fixture、契約文書、SBOM/build設定、IR moduleの一部境界は存在する | rootのLICENSE/SECURITY.md/CONTRIBUTING.mdは監査時点で不在。license/PII scan、日韓手順、source/JAR/Container再現checksumの受入証跡なし |
| #23 launch・一般登録 | OPEN | launch順序、production guard、rollback項目はrunbook/workflowにある | #21/#22 go判定、最終backup、release artifact固定、production適用、改名/public化、一般登録、post-launch監視は未実施 |

補足: #20〜#23以外の下位成果物が作業ツリーに存在しても、親runbookが要求するIssue受入・実環境検証・承認を代替しない。共有作業ツリーには未コミット変更があるため、launch runbookの「未コミット変更があれば停止する」条件にも現時点で該当する。

## 2. 完了条件の監査

### #20 — 運用・監視・脅威モデル

| 完了条件 | 必要な証拠 | 現在 |
|---|---|---|
| IR ACK p95 ≤ 2秒、accepted→latest p95 ≤ 60秒、table p95 ≤ 500ms | staging/productionの期間付きdashboard snapshotとerror budget | 未確認 |
| offline再送・Queue再配送・逆順完了で重複play 0件 | 実行ID、event/revision、結果の自動試験記録 | 未確認 |
| account/profileを跨ぐ到達不可 | 全Resource/API/object/MCPを含むnegative suiteの結果 | 未確認 |
| 保護データ・秘密値をtelemetryへ出さない | log/trace/metric/alertのredaction allowlist検査 | 未確認 |
| D1/DO/R2の復元と削除drill | 隔離環境のrestore/delete実行記録と照合結果 | 未確認 |
| alertからjob/event/profileを安全に追跡 | incident runbook、担当、alert payload検査 | 未確認 |

個別の認証・IR rate limitや契約テストは #20 の横断運用受入とは別である。#20のgo判定には、上表を同一運用記録へ集約する必要がある。

### #21 — 招待β・実データ・復元

次のすべてが必要であるが、監査時点で証跡はない。

- invite/allowlist、同意、privacy、問い合わせ/incident窓口を有効化した staging/production 相当環境
- local assistant.db/5DB のdry-run照合、暗号化backup、rollback point、移行承認
- 公式beatoraja最新版でのSP 7鍵 IR契約matrix、offline/restart/duplicate/既存IR併用の複数日記録
- 初回5DB、変更なし月次、変更あり月次、中断再開、生成、削除/復元の実運用記録
- 複数Remote MCP client、OAuth scope/paging、AI pending→Web承認→journalの接続記録
- 別account越境negative suite、性能SLO、30日の日次incident/欠落/error budget記録
- 隔離環境restore、削除7日取消・完全削除、SP 7鍵自己実験、日韓第三者再現、go/no-go記録

### #22 — OSS・公開準備

| 完了条件 | 必要な証拠 | 現在 |
|---|---|---|
| 全配布componentのAGPL-3.0とsource入手先 | root/package/module metadata、NOTICE、依存license監査 | 未受入 |
| secret/PII scan clean | tracked/public treeに対する再現可能なchecker結果 | 未受入 |
| 日韓文書の機能・privacy・保持/削除整合 | 日本語/韓国語README、quickstart、self-host/IR/upgrade/backupの第三者レビュー | 未受入 |
| 文書だけでself-hostと公式IR導入を再現 | clean環境での手順実行記録 | 未受入 |
| source/JAR/Containerの再現buildとchecksum照合 | build log、SBOM、provenance、checksum、Release候補 | 未受入 |

合成fixtureやbuild設定が存在することは、公開treeのlicense/PII監査とRelease artifactの再現性を意味しない。監査時点では root の LICENSE、SECURITY.md、CONTRIBUTING.md、CODE_OF_CONDUCT.md は不在である。

### #23 — launch・一般登録

次の順序を崩さない。

1. #20の運用受入、#21の30日β go、#22のOSS go、未解決重大issue 0件を記録する。
2. D1/DO/R2の最終暗号化backupとrestore確認、migration互換性、rollback pointを承認する。
3. source commit/tag、Python/Worker/Container/JAR、checksum、SBOM、privacy/terms/security URLを固定する。
4. production resource、secret、custom domain/DNS、GitHub production Environment reviewerの承認を記録する。
5. production migration/deployの個別承認後にhealth/version、登録→IR→表取得のsmokeを確認する。
6. さらに個別承認を得た場合だけ、repository改名、visibility変更、Release公開、invite-only解除、一般登録開始を行う。
7. launch後24時間/7日間のSLO、Queue、error、security alert監視担当と停止/rollback権限を割り当てる。

## 3. 公開前ゲート台帳

| Gate | 必須状態 | 監査時点 |
|---|---|---|
| #20運用go | SLO、脅威モデル、redaction、越境negative、chaos、復元/削除、incidentの受入記録 | 未充足 |
| #21βgo | 30日連続、重大欠落/越境0、重複play 0、SLO、復元、SP 7鍵、日韓第三者再現 | 未充足 |
| #22OSSgo | AGPL/license、PII/secret clean、日韓文書、再現build、checksum/SBOM | 未充足 |
| 最終データ保護 | encrypted backup ID、隔離restore、削除/rollback確認 | 未充足 |
| production準備 | resource inventory、domain/DNS、secrets、protected Environment reviewer、migration順序 | 未承認・未実施 |
| #23個別実行承認 | deploy、改名、public化、Release、招待解除、一般登録を個別に承認 | 未取得 |
| launch後監視 | 24時間/7日間の担当、SLO/error/security alert、停止条件 | 未割当 |

外部承認が揃うまで、production deploy、migration apply、実データ移行、GitHub改名/public化、Release公開、登録解除/一般登録開始を行わない。guard付きworkflowの存在だけでは、これらの承認や受入を代替しない。

## 4. #29の完了判定

- [x] #29のNative Sub-issuesが #20〜#23 を参照することを読み取り専用で確認
- [x] #20〜#23の現在状態（すべてOPEN）と公開前依存を本索引へ保存
- [x] #21/#22が完了してから#23を実行する順序を、Issue本文とrunbookに照合
- [ ] #20〜#23の完了条件、go判定、外部承認、実環境証跡が揃う
- [ ] #29を完了として扱う

次回監査では、各子issueへ実行記録・承認者・artifact checksum・backup/restore ID（秘密値やPIIを除く）を追記し、上表の未充足項目を再判定する。

## 5. 監査検証

今回の読み取り専用確認:

- gh issue view 29、20、21、22、23 で本文・state・依存を確認
- gh api の #29/#1 sub_issues で親子関係とstateを確認
- PLAN.md、docs/architecture.md、cloudflare/runbooks/epic-1-release-gates.md、cloudflare/runbooks/deployment.mdを照合
- root公開必須ファイルの存在確認
- git status --short --branch で共有作業ツリーの未コミット変更を確認

この索引の追加以外に、実装・設定・外部環境・GitHub Issue状態は変更していない。
