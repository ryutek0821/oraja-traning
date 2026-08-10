# Codex 依頼: Step 1 実装の修正（レビュー指摘の反映）

Step 1 の実装（`src/oraja_training/` と `tests/`）をレビューし、実データ `player-file/` で
検証した結果、仕様と実装の不備が見つかった。ゲージの追加調査結果はユーザー承認済みで、
`SPEC.md` は改訂3へ更新する。**改訂後の `SPEC.md` が正典**。それに合わせて実装を直してほしい。

---

## 絶対に守ること

1. **beatoraja のDBへは、いかなる経路でも書き込まない。**
   `score.db` / `scoredatalog.db` / `scorelog.db` / `songdata.db` / `songinfo.db` すべて。
   `-wal` / `-shm` を生成させることも禁止。書き込み先は `assistant.db` のみ。
2. **`player-file/` の中身を変更しない。** 挙動確認で書き込みが必要なら、必ず別ディレクトリに
   コピーしてから行う。作業前後で `md5 player-file/*.db` が一致すること。
3. **ライブ読取に `immutable=1` を使わない。** `mode=ro` + `busy_timeout` のまま。
   `immutable=1` は静的スナップショット（backfill・テスト）専用。
4. **`SPEC.md` と矛盾する事実を見つけたら報告する。** ゲージについては下記タスク4の
   一次ソース調査結果と、ユーザーが承認した2列分離を正として実装する。
5. **今回のスコープ外の機能を足さない。** 難易度表取得・特徴量・モデル・serve は Step 2 以降。
6. 実行環境は Python 3.11 以降。開発機の既定 `python3` は 3.9.6 なので注意
   （検証には 3.13 で作った `.venv` がある）。

---

## 背景（なぜ直すのか）

`plays.survival` は「譜面のどこまで到達したか」を表す**一次目的変数**（SPEC A-7 の完走モデル）。
現行実装は判定12列すべてを合計しているが、**`ems`/`lms` は空POOR（余分な押鍵）で譜面ノーツではない**。

実測による証明:

- `scoredatalog` の `clear >= 2`（最後まで到達した）272行すべてで、
  **`ems`/`lms` を除いた10列の合計が `notes` と厳密一致**（272/272、誤差ゼロ）
- 12列合計だと 272行すべてが `notes` を超える（中央値 +1.06%、最大 +12.2%）

つまり `survival` が系統的に膨張し、`completed = survival >= 0.95` の閾値が
実質「約94%到達（最悪85%到達）」にずれている。現スナップショットでのラベル反転は1行だけだが、
膨張量は押鍵の荒さに比例するため放置できない。

**`SPEC.md` A-4 の旧コメント「LN二重判定で >1.0 になり得る」は原因の説明が誤りだった**（改訂済み）。
真因は空POOR。この説明を信じたまま直すと、間違った直し方になる。

---

## 修正タスク

### 1. `judged` / `survival` から空POOR を除く（`collect/normalize.py`）

- `judged` = `epg+lpg+egr+lgr+egd+lgd+ebd+lbd+epr+lpr`（**10列**）
- `empty_poor` = `ems+lms` を新たに保存する（情報を捨てない。押鍵の荒さの指標として後で使う）
- `survival` = `judged / notes`。`notes` が 0 / NULL なら NULL
- `completed` = `survival >= 0.95`。`survival` が NULL なら NULL

### 2. `ir_import` 行の派生値を NULL にする（`collect/normalize.py` / `collect/backfill.py`）

`score` の判定内訳は**1プレイの内訳ではない**（実測: IR行は `lpg`/`lgr` が全行0、
10列合計が `notes` を超えるのが 297/298行）。したがって `source = 'ir_import'` の行では
`judged` / `empty_poor` / `survival` / `completed` を **NULL** にする。

`clear` / `ex` / `minbp` / `notes` / `playcount` は保存する。`bp_rate` も保存してよいが、
外部由来のため 1.0 を超え得る（実測1行: `minbp=2366 > notes=2364`）。

### 3. ゲージを開始時と結果側の2列に分ける（`db/store.py` / `collect/normalize.py`）

現行は `gauge = clear`（ランプ値 2..7 の整数）を格納している。Step 3 で `.brd` から回収する
ゲージは開始時の選択ゲージであり、結果ランプとは意味も時点も異なる。次の2列に分離する。

- `credited_gauge_kind TEXT`: 結果ランプから推定できる `EASY | NORMAL | HARD | EXHARD | NULL`
- `selected_gauge_kind TEXT`: `.brd` 由来の開始ゲージ
  `ASSIST_EASY | EASY | NORMAL | HARD | EXHARD | HAZARD | NULL`。Step 1 では全行 NULL

### 4. `clear` と選択ゲージの一次ソース確認結果（ユーザー承認済み）

```
clear 4 -> EASY   5 -> NORMAL   6 -> HARD   7 -> EXHARD
clear 0 / 1 / 2 / 3 / 8以上 -> NULL
```

`ClearType.AssistEasy(2)` には対応ゲージがなく、`LightAssistEasy(3)` はゲージ0にも対応する。
選択可能なゲージ種別に `LIGHT_ASSIST_EASY` は存在しない。`BMSPlayer` はアシスト時にランプを
上書きし、`.brd` には `config.getGauge()` を保存する。したがって `clear=2/3` から開始ゲージは
復元不能であり、Gauge Auto Shift 時は開始ゲージと結果側ゲージも一致しない。

### 5. `score.db` も整合性検証の対象にする（`collect/poller.py`）

現行 `_read_consistent_snapshot()` は `scoredatalog` の署名と `PRAGMA data_version` しか
検証しておらず、`score.db` は読取中に変化し得る。両方を検証し、どちらかが変化していたら再試行する。

### 6. `schema_version` を 2 にする（`db/store.py`）

version 1 は `judged` に空POOR を含み、`ems`/`lms` を保存していないため**正しい値を復元できない**。
in-place マイグレーションは実装しない。version 1 の `assistant.db` を開いたら、
黙って読まずに「削除して backfill をやり直せ」という明示的なエラーで停止すること。

### 7. `README.md` を更新する

「同梱スナップショットで判明した仕様差」に、空POOR と IR判定内訳の件を追記する。件数は下の受け入れ基準の値を使う。

---

## テスト

既存13件を維持したうえで、最低限これを追加する。**すべて合成データで書き、`player-file/` は使わない。**

- `judged` が `ems`/`lms` を含まないこと
- `scoredatalog` 由来の行で `survival > 1.0` が発生しないこと
- `ir_import` 行で `judged` / `empty_poor` / `survival` / `completed` が NULL になること
- `clear` 0/1/2/3/8以上 で `credited_gauge_kind` が NULL になること
- `clear` 4..7 が正しい `credited_gauge_kind` にマップされること
- Step 1 では `selected_gauge_kind` が常に NULL になること
- `schema_version = 1` の DB を開いたら明示的なエラーで停止すること
- `score.db` が読取中に変化したら `Poller` が再試行すること

---

## 受け入れ基準（実測値。1つでも外れたら実装ミス）

`player-file/` に対して `backfill` を実行した結果が**この値と完全に一致**すること。

**backfill の戻り値**（現行から変化なし）

```json
{"charts": 65998, "ir_imports": 542, "legacy_plays": 384,
 "skipped_ir_noplay": 3, "song_rows_seen": 66156}
```

**`source = 'legacy_last_snapshot'`（384行）**

| 指標 | 期待値 |
|---|---|
| `max(survival)` | **厳密に 1.0**（浮動小数の誤差も許容しない） |
| `survival > 1.0` の行数 | **0** |
| `abs(survival - 1.0) < 1e-12` の行数 | **365** |
| `completed = 1` の行数 | **368**（修正前は369。反転する1行は `clear=1`, 空POOR=168, `notes=953`） |
| `clear = 1`（FAILED）100行のうち `completed = 1` | **84**（修正前は85） |
| `judged = notes` となる `clear >= 2` の行数 | **272 / 272** |
| `is_course = 1` の行数 | **12** |
| `sum(empty_poor)` / `max(empty_poor)` / `empty_poor = 0` の行数 | **11255 / 168 / 2** |

**`credited_gauge_kind` の分布（`legacy_last_snapshot` 384行）**

| credited_gauge_kind | 行数 |
|---|---|
| `HARD` | 99 |
| `EASY` | 78 |
| `NORMAL` | 37 |
| `EXHARD` | 22 |
| `ASSIST_EASY` | 0 |
| `HAZARD` | 0 |
| NULL | 148（内訳: `clear=0` が12、`clear=1` が100、`clear=2/3` が35、`clear=8` が1） |

結果側推定236 / 不明148 は SPEC A-3 の実測と一致し、`selected_gauge_kind` は384行すべて NULL。

**`source = 'ir_import'`（542行）**

`judged` / `empty_poor` / `survival` / `completed` が **542行すべてで NULL**。

**書き込み禁止**

`backfill` と `collect` の前後で `md5 player-file/*.db` が一致し、
`player-file/` に `-wal` / `-shm` が生成されていないこと。

---

## 完了時に報告してほしいこと

1. 上の受け入れ基準の実測結果（表の各値を実際に走らせた数字で）
2. タスク4の一次ソース確認結果と、参照した beatoraja のファイル・行
3. `SPEC.md` と矛盾する事実を見つけた場合はその内容（**直さずに報告**）
4. スコープ外だと判断して手を付けなかった箇所があればその理由
