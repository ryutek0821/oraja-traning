# beatoraja 上達アシストツール — 実装依頼 #1（データ層とコレクタ）

> ⚠️ **この依頼は完了済み。内容の一部は `CODEX_PROMPT_step1_fix.md` で上書きされています。**
> 特に下記「4. 派生値」の `judged` の定義（判定12種の合計）は**誤り**でした。
> ゲージも単一列ではなく、結果側の `credited_gauge_kind` と開始時の `selected_gauge_kind` に分離済みです。
> 現行スキーマはversion 2で、version 1からのin-place移行はありません。
> 正しい定義は改訂後の `SPEC.md` A-4.1 を参照してください。
> このファイルは初回依頼時の記録として残しています。**新規の作業指示として使わないこと。**

> このファイルは**全文がCodexへの依頼文**です。そのままコピーして渡してください。
> 推奨: `sandbox: workspace-write` / `cwd` = このリポジトリのルート。

---

## 0. 前提

beatoraja（BMSのプレイヤー、Java製）のローカルSQLite DBを読み取り専用で監視し、プレイ単位の練習ログを蓄積して、次に叩くべき譜面・順序・回数を提示するツールを作ります。今回はその**データ層とコレクタ**を実装してください。

**リポジトリは実質空です。**現在あるのは `SPEC.md` / `PLAN.md` / この依頼文 / `player-file/`（テスト用DB）だけで、**ソースコードは1行もありません。**プロジェクトの雛形から作ってください。

- Python 3.11+ / `src/oraja_training/` レイアウト / `pyproject.toml` / `pytest`
- **このStepの依存は標準ライブラリ（`sqlite3` / `gzip` / `json` / `hashlib`）と `pytest` のみ。**`SPEC.md` A-5 に挙がっている `fastapi` 等は後のStepのもので、今は追加しないでください
- **git のコミット・ブランチ作成・push はしないでください。**作業ツリーに置くだけにしてください

**着手前に `SPEC.md` を全文読んでください。**特に「A-2 絶対的な不変条件」と「A-3 落とし穴」は、破るとユーザーの環境に実害が出ます。

---

## 1. 絶対に守ること

1. **beatoraja のDBへ書き込まない。**
   `score.db` / `scoredatalog.db` / `scorelog.db` / `songdata.db` / `songinfo.db` すべて読み取り専用です。
   特に `songdata.db` は別ツール（oraja-constellator）が `bmscf_*` テーブルで使用中で、書き込むとユーザーの環境が壊れます。
2. **`player-file/*.db` はユーザーの実プレイデータです。**テストでは必ず `file:...?immutable=1` で開いてください。
3. ライブ読取は `file:...?mode=ro` + `busy_timeout`。
   **`immutable=1` をライブDBに使わないでください。**変更されない前提で施錠を省くため、書込み中の不整合を読みます。
4. 書き込み先は `assistant.db` のみ。
5. BMSファイルのパーサを書かないでください。ネットワークアクセスもこのStepでは不要です。

---

## 2. 実装するもの

### 2.1 `db/readers.py` + `db/store.py`

`SPEC.md` A-5 の関数契約どおりに実装してください。

- **`PRAGMA table_info` でカラム集合を実行時に検出**し、SELECT列を組み立てること。
  `scorelog` に `avgjudge` / `oldavgjudge` が**存在しない環境があります**（同梱の `player-file/scorelog.db` がそれです）。ハードコードしたSELECTは壊れます
- `assistant.db` のスキーマは `SPEC.md` A-4 のDDLをそのまま使用し、`schema_version` によるマイグレーション機構を入れること

### 2.2 `collect/poller.py`（このツールの中核）

`scoredatalog.db` は `PRIMARY KEY(sha256, mode)` で、**プレイのたびに行が上書き**されます。ポーリングして差分を保存しない限り、プレイ履歴は永久に失われます。

**取りこぼしの「防止」より「検知」を優先してください。**取りこぼしをゼロにはできませんが、起きたことを後から知れれば分析側で扱えます。

1. `scoredatalog.db` / `-wal` / `-shm` の mtime を監視（既定5秒）。**mtimeは読むきっかけにしか使わない**
2. 変化を検知したら `scoredatalog` の**全行を読む**（384〜数千行なので十分軽い）
3. `(sha256, mode)` ごとに、前回の `playcount` と `payload_hash` を `chart_cursor` と比較する
4. イベントキーは `(source_generation, sha256, mode, playcount)`
5. `playcount` 差が1 → 通常取得 /
   **差が2以上 → 最新行のみ保存し `lost_events = delta - 1` を記録**
6. DBファイルの置換・再生成を検知したら `source_generation` を更新する（`playcount` が巻き戻るため）
7. `score.db` を読んだ後に `scoredatalog` の状態を再確認し、変化していれば retry。
   **2つのDBは別ファイルなので更新が原子的ではありません**
8. `payload_hash` は、行の全カラムを正規化した文字列から `sha256` で求めること（`playcount` が変わらないまま内容だけ変わるケースを検知するため）

### 2.3 `collect/backfill.py`

受け入れ基準を検証するために、このStepに含めます。`player-file/` のような静的なDBディレクトリを指定して初回取り込みを行うこと。

- `songdata.db` の `song` から `charts` を構築
- `scoredatalog` の384行を `source = 'legacy_last_snapshot'` として `plays` へ
- `score.db` の `date = 0` の行（545件）を `source = 'ir_import'` として記録

---

## 3. 派生値の算出規則（ここが実装の肝）

`plays` に行を作るとき、以下を確定させてください。

| カラム | 規則 |
|---|---|
| `judged` | 判定12種（`epg,lpg,egr,lgr,egd,lgd,ebd,lbd,epr,lpr,ems,lms`）の合計 |
| `survival` | `judged / notes`。**LNの二重判定で 1.0 を超えることがあります**（実測で最大1.12）。クリップしないでそのまま保存すること |
| `completed` | `survival >= 0.95` |
| `bp_rate` | `minbp / notes` |

### 3.1 ⚠️ `is_course` —— DBによって規則が違います

| 取得元 | `clear = 0` の意味 | `is_course` |
|---|---|---|
| `scoredatalog` | **コースプレイ**（コースでは個別ランプが付かない） | `1` |
| `score` | **NoPlay = 未プレイ**（実測10行） | `0`（そもそもプレイではないので `plays` に入れない） |

**同一の規則を両方のDBに適用しないでください。**

### 3.2 ⚠️ `gauge` —— `trophy` からは導出できません

`clear` ランプはゲージ基準のものが使用ゲージと1:1対応するので、**ランプから導出**してください。

| `clear` | `gauge` | `gauge_source` |
|---|---|---|
| 2〜7（AssistEasy / LightAssistEasy / Easy / Normal / Hard / ExHard） | ランプに対応するゲージ | `'clear_lamp'` |
| 0（NoPlay・コース） | `NULL` | `NULL` |
| 1（Failed） | `NULL` | `NULL` |
| 8以上（FC / Perfect / Max） | `NULL` | `NULL`（**FCランプはゲージ由来ランプを上書きするため判別不可**） |

**`trophy` をゲージ導出に使わないでください。**`SongTrophy` enum はゲージ文字（`g/G/h/H/n`）とオプション文字（`r/m/o/s/p/P/a/R/S/B/b`）が混在しており、**実測でHARDクリア99件のうち23件は `h` を含まず `r` のみ**でした。ゲージの指標として信頼できません。`trophy` は文字列のまま保存するだけにしてください。

失敗プレイのゲージは次のStepで `.brd` リプレイから回収します。今は `NULL` のままで構いません。

### 3.3 `exceeded_aggregate_score`

`score.db` の同一 `(sha256, mode)` と比較し、上回っていれば立ててください。

**「ローカルPB」ではないので、この名前を変えないでください。**`score` は外部IR（Mocha等）から取り込んだベストを含むため、「自己ベスト更新」を意味しません。

### 3.4 `mode` カラムの罠

`score` / `scoredatalog` の `mode` は**未定義LN譜面のLN種別**（0=LN / 1=CN / 2=HCN）です。`song.mode`（7=SP / 14=DP の鍵盤数）とは**別物**なので、混同しないでください。

---

## 4. 受け入れ基準

同梱の `player-file/` の5DBに対して、以下がすべて通ること。
**数値はすべて実測値です。ズレたら実装が間違っています。**

| # | 対象 | 期待 |
|---|---|---|
| 1 | `pytest` | 全通過 |
| 2 | backfill 実行後 | `plays` が **384行**（`source = 'legacy_last_snapshot'`） |
| 3 | 同上 | `is_course = 1` が **12行** |
| 4 | 同上 | `survival < 0.5` が **10行**、`completed = 0` が **15行** |
| 5 | 同上 | `survival > 1.0` の行が存在する（クリップしていない証明） |
| 6 | 同上 | `gauge_source = 'clear_lamp'` が **271行**、`gauge IS NULL` が **113行** |
| 7 | 同上 | `source = 'ir_import'` が **545行**（`score.date = 0`）。これらは `plays` の384行とは別に数えること |
| 8 | 同上 | `charts` が **66,156行**（`songdata.db` の `song`） |
| 9 | poller 単体テスト | `playcount` が 5→7 に飛んだ入力に対し、**1行挿入 + `lost_events = 1`** |
| 10 | poller 単体テスト | `playcount` が巻き戻る（DB差し替え相当）と `source_generation` が更新される |
| 11 | poller 単体テスト | 同じ状態で2回 tick しても行が増えない（冪等性） |
| 12 | **全テスト終了後** | **`player-file/*.db` の mtime とサイズが変化していないこと。これをテストで検証すること**（書き込んでいない証明） |

参考までに、`scoredatalog` の `clear` 分布の実測は
`0:12 / 1:100 / 3:35 / 4:78 / 5:37 / 6:99 / 7:22 / 8:1`（計384）です。基準3・6はここから導けます。

---

## 5. 成果物

- `pyproject.toml`、上記モジュール、`pytest` テスト
- `cli.py` に少なくとも `backfill` と `collect --daemon` を用意
- `README.md` に:
  - セットアップと実行手順
  - **beatoraja側で「自動リプレイ保存を1枠 ALWAYS にする」設定変更が必要**である旨
    （次のStepで失敗プレイのゲージを `.brd` から取るため）
  - **常駐が前提**であること（起動していない間のプレイは記録されず、`lost_events` としてのみ残る）
- 最後に、実装した内容と設計判断の要約を報告してください

## 6. 報告してほしいこと

実装中に `SPEC.md` の記述と矛盾する事実を見つけた場合、**勝手に仕様を直さず報告してください。**

この領域はドキュメントが乏しく、実測しないと分からない落とし穴が多くあります。実際、初版の仕様には「`trophy` からゲージを復元できる」という誤りが含まれており、実データで否定されました。片方が独断で前提を変えると齟齬がそのまま埋め込まれます。

特に、受け入れ基準の数値が合わない場合は、**基準に合わせて実装を歪めるのではなく、なぜ合わないのかを報告してください。**

---

## 付録: 次のStep（今回は着手不要）

- **Step 3** `collect/replay.py` — `.brd`（gzip JSON）から `gauge` / `seed` / レーンシャッフルのみ回収。`keyinput` は復号しない。
  ⚠️ Windows実機で `.brd` の生成条件・上書き条件を確認してから仕様を確定するため、**今回は着手しないでください**
- **Step 4** `tables/` — 難易度表（GENOCIDE ★ / Satellite sl）の取得とハッシュ突合
- **Step 5** `features/` — `songinfo.distribution` のデコードと特徴量生成
- **Step 6** `model/` — 完走モデルと完走時BPモデル（ゲート付き）
- **Step 7** `plan/` + `serve/` — セッション生成とローカル難易度表配信
