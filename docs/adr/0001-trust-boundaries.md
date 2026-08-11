# ADR-0001: 公式環境とセルフホストの信頼境界

- Status: Accepted
- Date: 2026-08-11
- Scope: issue #2 / Phase 0

## Context

現行の Python ツールは、ユーザーの beatoraja DB をローカル SQLite に読み込む self-hosted tool である。#1 では同じドメインコアを公式 Cloudflare 収集サービスでも使う計画がある。一方、セルフホスト環境は運用者・暗号鍵・入力データの真正性を公式サービスが管理できず、公式ユーザーの同意・データ品質・越境保護の前提を満たさない。

## Decision

1. 公式 Cloudflare と self-hosted は別の `trust_domain` として扱い、R2 prefix、profile stream、job partition、集計入力を分ける。
2. `trust_domain` と `aggregate_eligible` は、credential/installation provenance と同意記録から公式 Worker が付与する。client JSON の値をそのまま信用しない。
3. self-hosted の raw DB、event、derived feature、model state は個人利用に限定し、公式集合モデルへ import、federate、UNION しない。
4. 公式集合モデルには raw DB を渡さず、allowlist 済みの versioned event/derived input のみを、`official`・同意・SP7 の条件で投入する。
5. self-hosted が公式 API にアップロードできる将来経路を作る場合も、アップロード元の provenance を `self_hosted` として維持し、集合学習対象にはしない。provenance の昇格 API は作らない。
6. 初期のゲーム範囲は SP 7鍵（`SP7`）に固定する。DP/PMS と未知の rule は入口で reject する。

## Rejected alternatives

- self-hosted の統計だけを「匿名」とみなして公式集合へ混ぜる: 運用者が provenance を証明できず、同意・削除・データ品質の保証がない。
- account ID だけで公式/self-hosted を分ける: ID衝突、バックアップ復元、移行時の越境を防げない。
- Container 内だけで self-hosted を除外する: Queue、R2、集計 query の前段に混入すると後から安全に復元できない。

## Consequences

- event/upload/container/artifact の全 manifest に信頼ドメインと集合学習可否を含める。
- 公式と self-hosted のデータを比較表示する場合も、aggregate input ではなく明示的な profile-scoped read として実装する。
- テストでは self-hosted fixture を official aggregate query に渡すと0件/拒否になること、同じ event ID を境界間で再利用しても所有権が変わらないことを確認する。
