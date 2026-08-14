# 脅威モデル追補

## 既知の公開履歴

現在のpublic `ryutek0821/oraja-traning` はserver branchを既に公開している。#22/#23の分割前に全Git refsをsecret/PII scanし、検出資格情報をrotationする。Git clone、fork、cache等の過去copyは回収不能であり、private化を機密性の回復とは扱わない。

## 境界

- ownerはcredentialから解決し、body/path/object keyを認可根拠にしない。
- token、Profile key、capability secretはhashまたはenvelope encryptionで保存する。
- auditにpayload、token、秘密URLを残さない。
- 集合モデルは`semantic-rules.v1.json`の全privacy gateを満たさない限り公開しない。
- 削除確定時はProfile keyを破棄し、backupを含め即時読不能化する。
