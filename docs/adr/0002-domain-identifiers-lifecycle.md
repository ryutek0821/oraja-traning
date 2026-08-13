# ADR 0002: 識別子・所有・ライフサイクル

- Status: Accepted

全Entity IDはlowercase UUIDv7。AccountとProfileを分離し、`PROFILE_LIMIT`のdefaultを1とする。Profileだけを削除した場合はAccountを残し、slotを再利用できる。

Profile削除申請は7日間取消可能とし、期間満了後に削除を確定する。削除確定時にProfile envelope keyを破棄し、30日以内のbackupを含め即時読不能化する。生5DB、PlayEvent、個人モデルはProfile削除まで、superseded成果物本体は90日、manifest/digestはProfile削除まで保持する。失敗Jobのpayloadなし診断、IP/User-Agentは30日、payload/tokenなし監査は1年保持する。

Exportは正規化履歴、設定、推薦・モデル履歴、AIジャーナル、同意、manifest/digestを含み、元5DBを含めない。

認証期限は `semantic-rules.v1.json` を正本とする。IR tokenは256-bit/hash保存/個別失効/期限なし/90日未使用失効。Web cookieはSecure/HttpOnly/SameSite/`__Host-`、30日idle、重要操作は15分以内再認証。MCP accessは1時間、rotating refreshは30日。recommend/today URLは別の256-bit hash保存secretとし、失効後は即404。
