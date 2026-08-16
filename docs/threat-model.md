# 脅威モデル

## 資産と境界

| 資産 | 所有者 | 境界 | 必須制御 |
|---|---|---|---|
| credential、recovery code | Account | D1 control plane | Argon2id/hash-only保存、rate limit、単回使用 |
| play event、model state | Profile | Profile Durable Object | account/profile/device predicate、transaction冪等化 |
| 提出5DB、生成artifact | Profile | private R2 | envelope encryption、profile prefix、digest重複排除 |
| 承認済みadvisor journal | Profile | D1 + MCP | pending既定、明示scope、明示承認 |
| 集合model | 公式service | 公式trust domain | self-hosted/withdrawn data拒否、privacy gate |

## 脅威と緩和策

- cross-account identifierは認可入力にせず、認証済みsubjectとD1の複合predicateからownerを解決する。body、path、object keyを認可根拠にしない。
- replayと重複配送はUUIDv7 event ID、payload digest、profile-local transactionをQueue投入前に照合する。
- token、Profile key、capability secretはhashまたはenvelope encryptionで保存し、capability URLはrotationまたはprofile削除で失効させる。
- uploadはSQLite sidecar、上限超過、未知のファイル名、不正manifest、plaintext envelope metadataを拒否する。
- auditにpayload、token、秘密URLを残さない。
- MCPは集約resourceと明示的なページングtoolだけを公開し、生DB objectと会話全文を返さない。
- 集合modelは`semantic-rules.v1.json`の全privacy gateを満たさない限り公開しない。
- 削除確定時はProfile keyを破棄し、backupを含め即時読不能化する。

## 既知の公開履歴

現在のpublic `ryutek0821/oraja-traning` はserver branchを既に公開している。#22/#23の分割前に全Git refsをsecret/PII scanし、検出資格情報をrotationする。Git clone、fork、cache等の過去copyは回収不能であり、private化を機密性の回復とは扱わない。

## 残存リスク

実Cloudflare binding設定、secret rotation、restore drill、複数clientでのOAuth試験、全Git refsのsecret/PII scanは、production承認前にstagingで完了する。
