# 合成 beatoraja fixture

`synthetic_beatoraja.py` は、実プレイヤーのスナップショットを使わず、pytest の
一時ディレクトリへ `score.db`、`scoredatalog.db`、`scorelog.db`、`songdata.db`、
`songinfo.db` の5DBを生成する。`manifest.json` は行数ではなく、fixture の版、
対象ケース、期待する異常系、catalog/model の出所を固定する契約である。

baseline は読み取り専用テスト用の一貫した静的コピーで、通常プレイ、非PB、IR
aggregate、course/途中終了、欠落・重複、ちょうど1日境界、optional judge column
不在を含む。`catalog.json` には SP7 の owned / unowned、同一MD5の異なるSHA-256、
SHA-256欠落の table entry と、golden 用の固定 model state を収録する。

WAL/SHM/journal、破損SQLite、途中更新、累積カウンター巻戻りは baseline を汚さず、
次の variant builder で別ディレクトリに生成する。

```python
build_sidecar_fixture(root, "-wal")
build_sidecar_fixture(root, "-shm")
build_sidecar_fixture(root, "-journal")
build_corrupt_sqlite_fixture(root)
build_partial_update_fixture(root)
build_counter_rollback_fixture(root)
```

実データ、個人名、ローカル絶対パス、BMS本体は含めない。fixture を増やすときは
まず `REQUIRED_CASES` と `manifest.json` の期待値を更新し、variant は必ず別rootへ
生成すること。
