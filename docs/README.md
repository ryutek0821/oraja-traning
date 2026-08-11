# 設計契約

このディレクトリは、公式 Cloudflare 環境とセルフホスト環境の境界を含む、Phase 0（issue #2）の設計正典です。

- [アーキテクチャと責務](architecture.md)
- [データ分類](data-classification.md)
- [API・認証・監査契約](api-contract.md)
- [移行と rollback](migration-rollback.md)
- [ドメインエンティティ](contracts/domain-entities.md)
- [機械可読契約](contracts/README.md)
- [ADR-0001: 信頼境界](adr/0001-trust-boundaries.md)
- [ADR-0002: ID・所有権・ライフサイクル](adr/0002-domain-identifiers-lifecycle.md)
- [ADR-0003: job・revision・冪等性](adr/0003-revision-idempotency.md)

既存の `SPEC.md` はローカル MVP の実装正典です。本ディレクトリは、将来のサービス層の境界と wire contract を定義します。既存ローカル CLI の挙動をこの issue だけで変更したり、Cloudflare へデプロイしたりしません。
