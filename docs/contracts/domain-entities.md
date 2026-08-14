# Domain entities

機械可読正本は`domain-entities.v1.json`。全IDはlowercase UUIDv7で、AccountとProfileを分離する。`PROFILE_LIMIT`のdefaultは1だが設定可能。Profile削除後はAccountを残してslotを再利用できる。

認可は必ずcredentialから`Device → Profile → Account`等のowner鎖を解決する。request body、path、object key内のIDは権限を与えない。PlayEventのowner/provenance/eligibilityはserverが付与し、外部IR clientは指定できない。
