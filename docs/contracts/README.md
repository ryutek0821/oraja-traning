# v1 contract catalog

JSON Schema 2020-12を正本とし、すべてclosed shapeとする。

| Contract | Boundary |
|---|---|
| `ir-submission.v1.schema.json` | public IR client → server |
| `play-event.v1.schema.json` | authenticated server internal |
| `upload-manifest.v1.schema.json` | five DB upload |
| `container-input-manifest.v1.schema.json` | server → Container |
| `container-output-manifest.v1.schema.json` | Container → server |
| `artifact-manifest.v1.schema.json` | revision publisher |
| `aggregate-eligibility.v1.schema.json` | server eligibility decision |
| `domain-entities.v1.json` | ID/owner/lifecycle catalog |
| `semantic-rules.v1.json` | cross-record/temporal policy |

外部IRと内部PlayEventは意図的に分離する。course、DP/PMS、owner、provenance、trust、任意fieldは外部payloadに存在しない。内部PlayEventは`live_ir`またはserverが欠落分だけ生成した`five_db_backfill`を表し、backfillはaggregate不適格をschemaで強制する。時刻がserver時刻より5分超未来、5DB合計5GiB超、冪等競合、latest単調性、privacy gate等はJSON Schemaだけで表せないためsemantic rulesと契約テストで固定する。

正常・拒否fixtureは`examples/`に置き、Java client、TypeScript server、該当Python処理で同じcorpusを使う。
