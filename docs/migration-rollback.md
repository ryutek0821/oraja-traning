# Migration / rollback 方針

Phase 0 の成果物は設計と schema の追加だけで、データベース、Worker、Container、サービスを変更しない。後続実装は次の段階で進める。

## 段階的 migration

| Phase | 変更 | 読み取り | rollback |
|---|---|---|---|
| 0 契約確定 | ADR/schema/examples/tests | runtime変更なし | git revert。外部 stateなし |
| 1 shadow | event/upload manifest とserver付与owner/provenanceを追加 | 公式経路内で新旧出力を比較 | 新write pathを止める。accepted recordは削除しない |
| 2 profile partition | D1 Account/Profile/Device、profile DO、private R2 prefix | profile-scoped readを新経路で検証 | latest pointerを旧 immutable revisionへ戻す。raw objectは保管 |
| 3 async generation | Queue/Workflow/Container、job idempotency | old/new output を digest 比較 | revisionごとの artifactを削除せず、pointerだけ前revisionへ戻す |
| 4 cutover | official IR/API/Web/MCP を段階有効化 | health、ACK、revision、auditを監視 | feature flagで入口を止め、既存 accepted eventの再処理を同じ keyで継続 |

## Schema migration ルール

1. 未公開draft v1は互換維持せず、この契約で置換する。
2. 公開後の破壊的変更は即時切替し、旧clientへ426と更新情報を返す。
3. DB schema は expand → backfill → verify → cutover の順にし、破壊的な in-place downgrade は行わない。
4. 旧clientは426受信後に再送を停止する。
5. raw object の移動はcopy + hash verify + pointer cutover + retention後deleteとし、Profile境界を越えない。

## Rollback の不変条件

- accepted PlayEvent は rollback で二重受理・改変しない。
- `latest_pointer` は `candidate_revision > current_revision` のみ前進する通常規則を使う。意図的な rollback は、監査付きの `rollback_to_revision` 操作として、既存の ready revision を指す別の pointer revision を発行する。
- 旧 job の遅い完了が新しい latest を巻き戻してはならない。
- model/artifact は immutable。破損した object を上書きせず、新しい revision と digest を発行する。
- migration failure は transaction/feature flag 単位で止める。削除・再生成・backup expiryを自動で巻き戻さない。

## 検証ゲート

各段階で以下を満たすまで cutover しない。

- schema normal/reject fixture が通る
- owner関係とpolicy version付きeligibility testが通る
- duplicate/reordered Queue で effect が1つ、latest が単調である
- upload digest、Container input/output digest、artifact digest が連鎖する
- restore rehearsal で account/profile/DO/R2 の境界と deletion policy が再現できる
