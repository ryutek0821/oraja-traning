# ADR 0003: 冪等性・Job・revision

- Status: Accepted

`POST /v1/ir/events` はProfile DO transactionでPlayEvent、Job、予約revision、outbox、Alarmを原子的に保存した時点で202を返す。Queue送信はAlarmが冪等に行う。同じevent ID/owner/contract version/canonical payloadは既存Jobを200で返し、どれかが違えばeffectを作らず409にする。

Jobは`queued/running/succeeded/failed`。一時失敗は指数backoff+jitterで5回、その後DLQ。同じJob IDで運営再実行できる。Queueの重複・逆順を前提とする。

live IRはaccepted eventごとにrevisionを予約する。Player RecommendとDaily Menuを同一revisionで一括公開し、latestは`candidate > current`のCASだけで更新する。5DB backfill一式は1 Job・1 revisionとする。
