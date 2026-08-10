# oraja-training 技術仕様（v1）

この文書が**実装の正典**。調査の経緯と根拠は `PLAN.md` を参照。両者が矛盾した場合は **SPEC.md が優先**する。

最終更新: 2026-08-09（改訂3: `clear` は結果ランプ、`.brd` の `gauge` は開始時の選択ゲージと確定。
両者を `credited_gauge_kind` / `selected_gauge_kind` に分離し、曖昧な `clear=2/3` の誤推定を廃止）

---

## A-1. 目的と非目標

**目的**: beatoraja のローカルDBを読み取り専用で監視し、プレイ単位の練習ログを蓄積した上で、次のセッションで叩くべき譜面・順序・回数を提示する。

**非目標（v1では作らない）**:
- BMSパーサの自作
- `.brd` の判定再現（ノート単位のFAST/SLOW・ミス位置）
- IR連携
- course の生成
- プレイ中の動的再推定（receding horizon）
- 表を跨いだ（★ ↔ sl）確率比較

---

## A-2. 絶対的な不変条件

1. **beatoraja のDBへは、いかなる経路でも書き込まない。**
   `score.db` / `scoredatalog.db` / `scorelog.db` / `songdata.db` / `songinfo.db` すべて。
   特に `songdata.db` は oraja-constellator が `bmscf_*` テーブルで使用中。
2. **`player-file/*.db` はユーザーの実データ。** テストでは `file:...?immutable=1` で開く。
3. ライブ読取は `file:...?mode=ro` + `busy_timeout`。
   **`immutable=1` をライブDBに使ってはならない**（変更されない前提で施錠を省くため、書込み中の不整合を読む）。
4. 難易度表はキャッシュ付きで取得する。スクレイピングやIRへの大量アクセスをしない。
5. 書き込み先は `assistant.db` のみ。

---

## A-3. 入力データの仕様（実機DBで実測確認済み）

### beatoraja DB（`player/<name>/`）

| DB | テーブル | 意味 | 実測 |
|---|---|---|---|
| `score.db` | `score` PK(sha256,mode) | **ベスト集約**。1行=1プレイではない | 933行 |
| | `player` PK(date) | 日次累積 | 26行 |
| `scoredatalog.db` | `scoredatalog` PK(sha256,mode) | **直近プレイのスナップショット**（上書きされる） | 384行 |
| `scorelog.db` | `scorelog` PKなし | 自己ベスト更新の前後値のみ | 465行 |
| `songdata.db` | `song` PK(path) | 譜面メタ。`sha256` にindex | 66,156行 |
| `songinfo.db` | `information` PK(sha256) | 密度等 | 65,998行 |

`score` / `scoredatalog` の共通カラム:

```
sha256, mode, clear, epg,lpg,egr,lgr,egd,lgd,ebd,lbd,epr,lpr,ems,lms,
notes, combo, minbp, avgjudge, playcount, clearcount, trophy, ghost,
option, seed, random, date, state, scorehash
```

`scorelog` のカラム（この環境）:

```
sha256, mode, clear, oldclear, score, oldscore, combo, oldcombo,
minbp, oldminbp, date
```

### 落とし穴（すべて実測で確認済み）

| 項目 | 内容 |
|---|---|
| **ゲージ列が無い** | `ScoreData` クラスには `int gauge` があるが**DBに永続化されない** |
| ⚠️ **`clear` は結果ランプであり、選択ゲージではない** | `clear=4..7` だけは結果として認定されたゲージを `EASY / NORMAL / HARD / EXHARD` と推定できる。`clear=2/3` はアシスト状態と実ゲージを分離できず、0/1/8以上も不明。実測は **結果側推定236件 / 不明148件**（0が12、1が100、2/3が35、8が1） |
| **実際の選択ゲージ** | `ASSIST_EASY / EASY / NORMAL / HARD / EXHARD / HAZARD`。`LIGHT_ASSIST_EASY` はゲージ種別ではなくクリアランプ |
| ⚠️ **Gauge Auto Shift** | 開始時に選んだゲージと、終了時に認定されるゲージが変わり得る。`.brd` の開始ゲージと `clear` 由来の結果側推定を同じ列・同じ説明変数として扱わない |
| ⚠️ **`trophy` はゲージの指標にならない** | `SongTrophy` enum（下記）だが、**ゲージ文字とオプション文字が混在**する。実測でHARDクリア99件のうち**23件は `h` を含まず `r` のみ**。`option` 由来の情報としてのみ扱い、ゲージ導出に使わないこと |
| `clear` 値 | 0 NoPlay / 1 Failed / 2 AssistEasy / 3 LightAssistEasy / 4 Easy / 5 Normal / 6 Hard / 7 ExHard / 8 FC / 9 Perfect / 10 Max |
| ⚠️ **`clear=0` の意味はDBで異なる** | `scoredatalog` では**コースプレイ**（12件。4曲連続クラスタで出現、全て `trophy='g'`。ゲージ持ち越しのため独立試行ではない）。`score` では**NoPlay=未プレイ**（10件）。同一の規則を両DBに適用してはならない |
| `mode` カラムの意味 | `score` / `scoredatalog` の `mode` は**未定義LN譜面のLN種別**（0=LN / 1=CN / 2=HCN）。実測は 0が370、1が14。`song.mode`（7=SP / 14=DP の鍵盤数）とは**別物**。混同しないこと |
| EXスコア | `(epg+lpg)*2 + egr+lgr` |
| **`song.level` は使用不可** | `#PLAYLEVEL` 生値。実測で `0`〜`24` と `10000` が混在。能力尺度にしてはならない |
| **`score.date = 0` はIR取込** | 545行 / playcount 1318。自環境プレイは388行 / 789 |
| `scorelog` のカラム差異 | 環境により `avgjudge` / `oldavgjudge` が**無い**（この環境がそれ）。`PRAGMA table_info` で実行時検出すること |
| ⚠️ **`ems`/`lms` は空POOR。進行度ではない** | `clear >= 2` の272行すべてで **`ems`/`lms` を除く10列の合計が `notes` と厳密一致**（272/272、誤差ゼロ）。12列合計にすると272行すべてが `notes` を超える（中央値 +1.06%、最大 +12.2%）。**進行度（`judged` / `survival`）の計算に `ems`/`lms` を入れてはならない** |
| ⚠️ **`score` の判定内訳は1プレイの内訳ではない** | IR行（`date = 0`）は **`lpg`/`lgr` が全行0**、10列合計が `notes` を超える（297/298行、中央値 +1.25%）。IR側が粗い内訳しか返さないため。ローカル行（`date > 0, clear >= 2`）でも61/302行が `notes` を超える。**判定内訳を使う処理は `scoredatalog` 由来の行に限ること** |
| **IR由来内訳の判別ルール** | `lpg = 0 かつ lgr = 0` が IR由来内訳の署名。実測で**完全分離**する: `notes` を超える61行は 61/61 が該当、厳密一致する240行は 0/240。61行は全て `scoredatalog` にも存在する（＝ローカルでプレイ済みで `date` は更新されたが、内訳はIR由来のまま残っている）。SPEC A-6 の「`date = 0` の除外だけではIR混入を除ききれない」の実証 |
| `option` | RANDOM系が365/384 = 95%。**固定レーン前提の特徴（レーン偏り等）は作らない** |
| `state` | 全行 0。情報量ゼロ |
| `journal_mode` | 実測は全DB `delete`。ただしWAL・DB置換に耐える実装にすること |
| プレイ間隔 | 実測の最小は42秒。scorelog 465行に同一 `(sha256, date)` の重複はゼロ。それでも `playcount` を世代キーにして衝突を回避する |

### `trophy` のエンコード

`SongTrophy` enum（[ScoreData.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ScoreData.java)）:

```
EASY='g'   GROOVE='G'  HARD='h'      EXHARD='H'   NORMAL='n'
MIRROR='m' RANDOM='r'  R_RANDOM='o'  S_RANDOM='s' H_RANDOM='p'
SPIRAL='P' ALL_SCR='a' EX_RANDOM='R' EX_S_RANDOM='S'
BATTLE='B' BATTLE_ASSIST='b'
```

実測との整合: EASY clear→`r` / HARD clear→`rh`,`hr` / EXHARD→`Hrh`,`hrH` / course→全て`g`。

### `songinfo.information` の文字列形式

`distribution`: `#` プレフィックスの後、**1秒バケット × 7列 × base36(2文字)**。

```
[0] LN皿   [1] 皿密度   [2] 皿ノーツ   [3] LN鍵   [4] 鍵密度   [5] 鍵ノーツ   [6] 地雷
```

検算: 109秒の曲 = `1 + 109×14 ≒ 1555` 文字。base36は `0-9a-z`、最大 `36²-1`。

`speedchange`: `speed,time_ms` の繰り返し。
`lanenotes`: レーンごとに `normal,long,mine` の3値。

### `.brd` リプレイ（`player/<name>/replay/*.brd`）

gzip圧縮JSON。`keyinput` は URL-safe Base64 + GZIP（1イベント = 符号付きキーコード1byte + LE時刻8byte、μs単位）。

**v1で読むのは `gauge` / `seed` / 実レーンシャッフル / `#RANDOM`選択 / `sha256` / 日時 のみ。`keyinput` は復号しない。**

`ReplayData.gauge` は `BMSPlayer` が `config.getGauge()` を保存するため、**開始時に選択したゲージ**である。
一方、`clear` はアシスト、フルコンボ、Gauge Auto Shift 後の状態を反映した**結果ランプ**であり、同義ではない。

⚠️ Step 0（Windows実機確認）で生成条件・上書き条件を検証してから Step 3 に着手すること。

---

## A-4. `assistant.db` スキーマ

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE charts (
  sha256      TEXT PRIMARY KEY,
  md5         TEXT,
  title       TEXT,
  artist      TEXT,
  notes       INTEGER,
  song_mode   INTEGER,              -- 7=SP, 14=DP
  path        TEXT,
  updated_at  INTEGER NOT NULL      -- Unix epoch 秒
);
CREATE INDEX idx_charts_md5 ON charts(md5);

-- 本ツールの中核資産。1行 = 1プレイ
CREATE TABLE plays (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256                    TEXT NOT NULL,
  mode                      INTEGER NOT NULL,
  played_at                 INTEGER NOT NULL,   -- Unix epoch 秒
  playcount                 INTEGER NOT NULL,   -- イベント世代キー
  source_generation         INTEGER NOT NULL,   -- DB置換の検知用
  source                    TEXT NOT NULL,      -- collector | legacy_last_snapshot | ir_import
  clear                     INTEGER NOT NULL,
  ex                        INTEGER,
  minbp                     INTEGER,
  notes                     INTEGER,
  judged                    INTEGER,            -- 判定された譜面ノーツ数。epg+lpg+egr+lgr+egd+lgd+ebd+lbd+epr+lpr
                                                --   ⚠️ ems/lms（空POOR）は含めない
  empty_poor                INTEGER,            -- ems+lms。進行度ではなく押鍵の荒さの指標
  survival                  REAL,               -- judged/notes。完走時は厳密に 1.0。**>1.0 は実装バグ**
  completed                 INTEGER,            -- survival >= 0.95
  bp_rate                   REAL,               -- minbp/notes
  credited_gauge_kind       TEXT,               -- clearから推定できる結果側ゲージ。EASY | NORMAL
                                                --   | HARD | EXHARD | NULL
  selected_gauge_kind       TEXT,               -- .brdの開始ゲージ。ASSIST_EASY | EASY | NORMAL
                                                --   | HARD | EXHARD | HAZARD | NULL
  option                    INTEGER,
  seed                      INTEGER,
  random                    INTEGER,
  trophy                    TEXT,
  is_course                 INTEGER NOT NULL DEFAULT 0,
  exceeded_aggregate_score  INTEGER NOT NULL DEFAULT 0,
  lost_events               INTEGER NOT NULL DEFAULT 0,
  payload_hash              TEXT NOT NULL,
  ingested_at               INTEGER NOT NULL,
  UNIQUE(source_generation, sha256, mode, playcount)
);
CREATE INDEX idx_plays_played_at ON plays(played_at);
CREATE INDEX idx_plays_sha256 ON plays(sha256, mode);

-- content-diff のカーソル
CREATE TABLE chart_cursor (
  source_generation INTEGER NOT NULL,
  sha256            TEXT NOT NULL,
  mode              INTEGER NOT NULL,
  last_playcount    INTEGER NOT NULL,
  last_payload_hash TEXT NOT NULL,
  PRIMARY KEY(source_generation, sha256, mode)
);

CREATE TABLE collector_state (
  key               TEXT PRIMARY KEY,   -- 'scoredatalog' | 'replay'
  source_generation INTEGER NOT NULL,
  last_mtime        REAL,
  last_size         INTEGER,
  last_run_at       INTEGER,
  last_error        TEXT
);

CREATE TABLE chart_features (
  sha256          TEXT PRIMARY KEY,
  feature_version INTEGER NOT NULL,
  density_mean REAL, density_p90 REAL, density_p99 REAL,
  end_density REAL, burst_max REAL,
  scratch_rate REAL, scratch_p90 REAL, scratch_combo_rate REAL,
  ln_rate REAL, soflan_var REAL, soflan_changes INTEGER, stop_count INTEGER,
  chart_seconds REAL, total_notes INTEGER
);

-- 表ごとに別行（★ と sl を単一尺度に潰さない）
CREATE TABLE table_entries (
  table_id   TEXT NOT NULL,       -- 'genocide' | 'satellite'
  level      TEXT NOT NULL,
  sha256     TEXT,
  md5        TEXT,
  title      TEXT,
  fetched_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_table_entries
  ON table_entries(table_id, level, COALESCE(sha256, md5));

CREATE TABLE model_state (
  target           TEXT NOT NULL,    -- completed | bp_rate | clear_easy
  version          INTEGER NOT NULL,
  trained_at       INTEGER NOT NULL,
  params_json      TEXT NOT NULL,
  n_train          INTEGER NOT NULL,
  logloss          REAL,
  brier            REAL,
  baseline_logloss REAL,
  PRIMARY KEY(target, version)
);

-- prequential 評価用（予測はプレイ「前」に保存する）
CREATE TABLE predictions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256             TEXT NOT NULL,
  mode               INTEGER NOT NULL,
  predicted_at       INTEGER NOT NULL,
  model_target       TEXT NOT NULL,
  model_version      INTEGER NOT NULL,
  p_pred             REAL NOT NULL,
  baseline_p         REAL,
  candidate_set_json TEXT,
  selection_prob     REAL,
  is_exploration     INTEGER NOT NULL DEFAULT 0,
  resolved_play_id   INTEGER
);

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  arm        TEXT,                  -- A | B（ランダム化A/B用）
  slots_json TEXT NOT NULL
);

CREATE TABLE revisits (
  sha256        TEXT NOT NULL,
  mode          INTEGER NOT NULL,
  due_at        INTEGER NOT NULL,
  interval_days INTEGER NOT NULL,
  last_result   TEXT,
  PRIMARY KEY(sha256, mode)
);
```

**`schema_version` は 3**。version 2 からは日次取込・ベスト差分・難易度表・推薦履歴用テーブルを加える加算的マイグレーションを行う。version 1 からの in-place マイグレーションは**しない**。version 1 は `judged` に空POOR を含めており、`ems`/`lms` を保存していないため**正しい値を復元できない**。version 1 の `assistant.db` を開いたら、黙って読まずに「削除して backfill をやり直せ」という明示的なエラーで停止すること。

### A-4.1 派生値の規則

| 列 | 規則 |
|---|---|
| `judged` | 判定10列の合計。**`ems`/`lms` は加算しない** |
| `empty_poor` | `ems + lms` |
| `survival` | `judged / notes`。`notes` が 0 / NULL なら NULL |
| `completed` | `survival >= 0.95`。`survival` が NULL なら NULL |
| `credited_gauge_kind` | 結果ランプ `clear` から下表で推定。結果側の記述値であり、開始ゲージの代用は禁止 |
| `selected_gauge_kind` | `.brd` の `gauge` を正規化した開始ゲージ。Step 1 では全行 NULL、Step 3 で収集する |
| `is_course` | `scoredatalog` 由来（`collector` / `legacy_last_snapshot`）かつ `clear = 0` のとき 1 |

`clear` → `credited_gauge_kind` の対応:

```
4 -> EASY   5 -> NORMAL   6 -> HARD   7 -> EXHARD
0 / 1 / 2 / 3 / 8以上 -> NULL
```

一次ソースでは、[`ClearType`](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ClearType.java#L10-L20) の
`AssistEasy(2)` に対応ゲージがなく、`LightAssistEasy(3)` はゲージ0にも対応する。
また [`GaugeProperty`](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/play/GaugeProperty.java#L22-L29) の
選択可能な通常プレイ用ゲージに `LIGHT_ASSIST_EASY` はない。さらに
[`BMSPlayer`](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/play/BMSPlayer.java#L861-L900) は
アシスト時にランプを上書きし、リプレイには `config.getGauge()` を保存する。このため `clear=2` は任意の実ゲージに
強いアシストを適用した結果、`clear=3` は軽いアシストの結果または実ゲージ `ASSIST_EASY` のいずれかであり、
どちらも開始ゲージを一意に復元できない。`HAZARD` もランプからは判別できない。

**`source = 'ir_import'` の行では `judged` / `empty_poor` / `survival` / `completed` を NULL にする。**
A-3 のとおり `score` の判定内訳は1プレイの内訳ではなく、進行度として意味を持たないため。
`clear` / `ex` / `minbp` / `notes` / `playcount` などの生値は保存する。`bp_rate` は保存してよいが、
外部由来のため 1.0 を超え得る（実測1行）。**学習の目的変数にしてはならない**。

---

## A-5. モジュール構成と契約

```
src/oraja_training/
  config.py            Config: beatoraja_dir, player_name, assistant_db, port, poll_interval

  db/readers.py        open_live(path) -> Connection        # mode=ro + busy_timeout
                       open_snapshot(path) -> Connection    # immutable=1（テスト専用）
                       table_columns(conn, table) -> set[str]
                       read_scoredatalog(conn) -> list[ScoreRow]
                       read_score(conn) -> list[ScoreRow]
                       read_songs(conn, sha256s) -> list[SongRow]
                       read_songinfo(conn, sha256s) -> list[InfoRow]
  db/store.py          init(path) -> Connection / migrate()  # スキーマは A-4

  collect/poller.py    Poller.tick() -> TickResult(new_plays, lost_events, generation_changed)
  collect/replay.py    scan(replay_dir) -> list[ReplayMeta]  # gauge/seed/shuffle のみ
  collect/backfill.py  run(db_dir) -> BackfillResult

  tables/fetch.py      fetch_table(table_id, header_url) -> TableData  # ETag対応・キャッシュ
  tables/match.py      resolve(entries, charts) -> MatchReport         # 突合率を返す

  features/songinfo.py decode_distribution(s) -> list[list[int]]       # 7列 × 秒
                       decode_speedchange(s) / decode_lanenotes(s)
  features/build.py    build_all(conn) -> int

  model/fit.py         evaluate_and_fit(conn) -> ModelReport           # 時間順holdout・ゲート判定

  plan/menu.py         build_session(...) -> Session                   # A-8 の構成
  plan/schedule.py     due_revisits(conn, now) -> list

  serve/app.py         ThreadingHTTPServer: /table/header.json /table/score.json / /api/status
  cli.py               initialize / daily-update / tables refresh / features build / menu / review / serve
```

`Poller` の整合性要件: 1回のスキャンで `scoredatalog.db` と `score.db` の**両方**について、読取の前後で
ファイル署名（`.db` / `-wal` / `-shm` の mtime・サイズ・inode）と `PRAGMA data_version` が
変化していないことを確認する。どちらか一方でも変化していたら再試行する。

実行時依存は Python 3.11+ 標準ライブラリのみ（`sqlite3` / `gzip` / `json` / `urllib` / `http.server`）。**BMSパーサは追加しない。**

---

## A-6. 学習データのフィルタ（`model/dataset.py`）

除外:
- `is_course = 1`（コース。ゲージ持ち越しで独立試行ではない）
- `source = 'ir_import'`
- `selected_gauge_kind IS NULL`（開始ゲージ不明）
- 完走モデル以外では `completed = 0`

`source = 'legacy_last_snapshot'` は「384譜面それぞれの最終1プレイ」であり通常の時系列ではない。
**時間順holdoutの対象にせず、事前分布の材料としてのみ使う。**

`credited_gauge_kind` は結果ランプから導出した値なので、開始ゲージの代用やモデルの説明変数にすると
目的変数の漏洩になる。Gauge Auto Shift でも両者は一致しないため、`selected_gauge_kind` と結合しない。

`score.date = 0` の除外だけではIR混入を除ききれない（IR取込後にローカルで1回叩くと `date > 0` になるが `score.clear` は外部由来のまま残りうる）。**これは実測で確認済み**: `score` の `date > 0` かつ `clear >= 2` の302行のうち **61行**は判定10列の合計が `notes` を超え、その61行すべてが `lpg = 0 かつ lgr = 0`（IR由来内訳の署名）で、かつ全行が `scoredatalog` にも存在する。つまり**ローカルでプレイして `date` は更新されたのに、内訳はIR由来のまま**である（`scoredatalog` では272/272行が厳密一致）。したがって役割を分ける:

- `score` = 外部ベストを含む **baseline**（教師にしない）
- `scoredatalog` = 直近ローカル結果
- **コレクタ稼働後のイベントだけが確実な教師データ**

---

## A-7. モデル仕様（`model/fit.py`）

単純な `minbp/notes` 回帰は使わない（ノーツ数で分散が不均一、BPは局所パターンで相関、中断行では未到達ノーツがBPに混入）。二段階にする:

```
1. 完走モデル: C ~ Bernoulli(P(survival >= 0.95))
2. 完走時BP  : 完走行のみ logit((minbp + 0.5)/(notes + 1)) を Ridge 回帰
```

不確実性は日／セッション単位の block bootstrap。中断行は捨てず、完走モデルの教師として使う。

特徴は **表別の単調レベル効果 + 密度 + 皿率 の2〜3軸のみ**から開始。
`f_genocide(level)` と `f_satellite(level)` は**別の単調曲線**とし、単一の `b_i` に潰さない。

**軸を追加してよい条件（ゲート。すべて満たすこと）**:
1. その軸の高低双方に十分なプレイがある
2. leave-one-session-out で log loss が改善する
3. セッション単位 bootstrap で係数の符号が安定する

**必ず出力する指標**: 時間順 log loss / Brier / 較正曲線 / ベースライン（表レベルのみ）との差 / **レベルのみ推薦との Top-12 Jaccard**。
Jaccard ≥ 0.8 なら「実質的に表レベル推薦と同じ」とUIに表示する。**Accuracyは使わない。**

**「FAILEDの85%が完走」は普遍的前提ではなく、現在のゲージ運用下での記述統計。** ローリングで監視し、変化したら別regimeとして扱う。

---

## A-8. メニュー構成（`plan/menu.py`）

判定数予算で構成する。基本10万判定は WARMUP 8k / FOCUS-A 21k / TRANSFER-A 10k /
FOCUS-B 21k / TRANSFER-B 10k / LAMP 15k / REVIEW 10k / PROBE 5k。さらに欠落や短曲を吸収する
RESERVE 10k を別に提示する。focus譜面は3枠離して再試行し、疲労日は10万を維持したまま高負荷候補を除く。

制約付きgreedy: 同一傾向3連続禁止 / 高負荷連続禁止 / 同アーティスト・同差分作者への露出ペナルティ / 予測時間で予算制限。

再訪は初期は固定間隔（成功 1→3→7→14日、失敗は短縮）。

**探索枠**: 提示枠の20〜30%は同じ表・レベル・既プレイ状態からランダム抽出し、候補集合と選択確率を `predictions` に保存する（prequential評価に必須）。

**course は生成しない。セッション開始時に枠を固定して配信する**（難易度表のホットリロード保証が無いため）。

---

## A-9. 出力（`serve/app.py`）

- `GET /table/header.json` … `data_url` を `/table/score.json` へ向ける
- `GET /table/score.json` … `level` を WARMUP / FOCUS-A / TRANSFER-A / FOCUS-B / TRANSFER-B / LAMP / REVIEW / PROBE / RESERVE に分ける
- `GET /` … 順序、目標ゲージ、予測成功率と信頼区間、直近プレイの完走可否とBP率、`.brd` 突合率、最終収集時刻、`lost_events` 件数

---

## A-10. 正直な名乗り

v1が推定するのは `P(今クリアできる)` であって `E(この譜面を練習した後の能力増分)` ではない。

- UI・README に「**学習効果はヒューリスティック、成功確率のみ統計モデル**」と明記する
- v1は「表別レベル＋粗い負荷特性による多様化推薦」。**「多次元弱点推定」を名乗るのは A-7 のゲートを通った軸が増えてから**
- 1秒集計では「16分乱打と高密度同時押し」「隣接トリルと左右交互」「微縦連と通常乱打」を区別できない。乱打・縦連・トリルの軸は oraja-constellator の `bmscf_chart_analysis`（`micro_rate` / `long_jack_rate` / `avg_chord` / `rhythm_family`）を A-7 のゲートで評価して取り込む
