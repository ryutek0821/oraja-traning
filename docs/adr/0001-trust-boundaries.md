# ADR 0001: 公式限定サービス境界

- Status: Accepted
- Scope: v1

## Decision

v1は日本向け・日本語・公式Cloudflareサービス・SP 7鍵通常曲だけを提供する。course、DP、PMS、self-host、import trust domainを公式service contractに設けない。既存ローカル利用はSQLite adapterとして維持するが、公式集合学習へ接続しない。外部IR payloadはプレイ事実だけを持ち、Account/Profile/Device、公式provenance、eligibilityは認証済みserverが付与する。すべての認可はcredentialから完全なowner鎖を解決して判定する。

集合学習は登録条件とし、撤回は収集停止と退会・削除への移行を意味する。法務レビューで必須同意が許容されない場合だけ、初期OFFの任意参加へ変更し、撤回後も個人機能を継続する。

日本法務レビューは集合学習、国外委託、規約、年齢・保護者同意方針を一般公開前に行う。必要な方針が確定しない場合は一般登録を開始しない。法務判断と事業上のリスク受容はこの技術ADRでは承認しない。

## Repository boundary

serverとIR公開物は将来別repositoryへ分離する。license、visibility、公開対象の最終決定と、private化・新repo作成・履歴移行は#22/#23でのみ承認・実行する。Phase 0ではrepository状態を変更しない。

現public repoはserver branchを既に公開した。全Git refsをsecret/PII scanし、検出資格情報をrotationする。過去copyは回収不能な既知公開として脅威モデルに残す。
