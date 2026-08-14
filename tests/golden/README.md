# Golden 基準と更新手順

`synthetic_backfill.json` は backfill / snapshot の集計基準、`synthetic_outputs.json`
は匿名合成 catalog を使った正規化 play 列、Player Recommend 候補、Daily Menu、model
状態の基準である。`collection_manifest.json` は issue #3 着手前の commit
`3679083b1dac709884ba575b08ba7c35dc4ec12e` における 42 test functions /
56 collected cases を固定する。

golden を更新するのは、次のいずれかが意図的に変わったときだけとする。

- 正規化列、推薦アルゴリズム、model contract、menu quota の仕様変更
- 合成 fixture のケース追加・修正で、期待出力をレビュー済みの場合

更新手順:

1. `tests/fixtures/synthetic_beatoraja.py` の `FIXTURE_VERSION`、`REQUIRED_CASES`、
   manifest の期待値を先に更新する。
2. `UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run python tests/golden/update_synthetic.py --write`
   を実行する。これは `synthetic_outputs.json` だけを書き換える。
3. golden diff に実データ、個人名、絶対パス、BMS 本体、揺れる時刻がないことを確認する。
4. `UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run python tests/golden/update_synthetic.py --check`
   と `UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run pytest -q tests/test_synthetic_fixture.py tests/test_golden.py tests/test_backfill.py tests/test_snapshot.py`
   を実行する。

レビューでは、fixture の追加ケースが `manifest.json` にあり、baseline DB と variant
DB が混ざっていないこと、10万判定 + 1万 RESERVE、model 状態、正規化列の変更理由が
説明されていることを必須とする。golden の数値だけを手編集して受入条件を緩めては
ならない。
