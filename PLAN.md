# oraja-training — beatoraja 上達アシストツール 企画プラン

作成日: 2026-08-09 / 最終同期: 2026-08-09（SPEC改訂3・Step 1修正版）
調査: Claude Code（Web調査） + Codex（GitHub一次ソース確認・実データ検証）

> **この文書は調査記録とロードマップであり、実装の正典ではない。**
> 実装が従うのは `SPEC.md`。両者が矛盾した場合は **SPEC.md が優先**する。
> 2026-08-09時点で、旧案だったBMSパーサ、RANDOM Monte Carlo、course生成、
> receding horizon、固定レーン特徴はMVPから除外済み。

---

## 0. 一行結論

**「次に何を叩くか」を出すツールは既にある（bms-nexus / LR2IRリコメンド / 推定難易度表）。空白は「どの順で・何曲・いつ再訪するか」を、多次元の個人差と時間変化を踏まえて出し、"翌日保持"と"未練習譜面への転移"で効果を検証する閉ループ・コーチ。**

---

## 1. 調査結果：データソース（一次ソース確認済み）

### 1.1 beatoraja ローカルDB（`beatoraja/player/<name>/`）

| ファイル | テーブル | 中身 | 注意点 |
|---|---|---|---|
| `score.db` | `info`, `player`, `score` | 譜面ごとのベスト集約 | **1行 = 1プレイではない**。`epg..lms`は最高EXスコア時の内訳、`minbp`は別プレイの最小BP、`combo`も別プレイの最大値になり得る |
| `scorelog.db` | `scorelog` | 自己ベスト更新の前後値 | ランプ/EX/コンボ/minBP/平均判定のどれかが更新された時**だけ**追加。失敗プレイ・非更新プレイは残らない → 成功確率の推定には不十分 |
| `scoredatalog.db` | `scoredatalog` | `score`と同じ全カラム。**PK=(sha256,mode) で上書き** → 1譜面1行＝**直近プレイのスナップショット** | ⚠️ **全プレイログではない（P0で確認、§11）。**失敗・非更新プレイを含むため、常駐コレクタで以後のイベントを保存し、欠落も検知できる |
| `songdata.db` | `folder`, `song` | 譜面メタ + ファイルパス | `path`が実体識別の中心、`sha256`にindexあり |
| `songinfo.db` | `information` | `density`, `peakdensity`, `enddensity`, `distribution`, `speedchange`, `lanenotes` | **`distribution` は 1秒バケット × 7列 × base36(2文字)**（列順: LN皿 / 皿密度 / 皿ノーツ / LN鍵 / 鍵密度 / 鍵ノーツ / 地雷）。鍵盤と皿を分離した秒単位密度が全譜面分ある。乱打・トリル・縦連はここから識別できないためv1では扱わない |

`score` テーブル主要カラム:
```
sha256, mode, clear, epg,lpg,egr,lgr,egd,lgd,ebd,lbd,epr,lpr,ems,lms,
notes, combo, minbp, avgjudge, playcount, clearcount, trophy, ghost,
option, seed, random, date, state, scorehash
```
- `e*`=FAST側 / `l*`=SLOW側、`pg/gr/gd/bd/pr/ms` = PGREAT/GREAT/GOOD/BAD/POOR/MISS
- EX score = `(epg+lpg)*2 + egr+lgr`
- `clear`: 0 NoPlay / 1 Failed / 2 AssistEasy / 3 LightAssistEasy / 4 Easy / 5 Normal / 6 Hard / 7 ExHard / 8 FC / 9 Perfect / 10 Max
- `date` は Unix epoch 秒（INTEGER）
- `judged` は `ems/lms`（空POOR）を除いた10判定列の合計。`empty_poor = ems+lms` として分離する
- `score.date = 0` のIR集約は1プレイの判定内訳ではないため、学習用派生値を作らない
- `clear` は**結果ランプ**であり開始ゲージではない。結果側ゲージを推定できるのは `clear=4..7` のみ

出典: [ScoreDatabaseAccessor.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ScoreDatabaseAccessor.java) / [ScoreLogDatabaseAccessor.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ScoreLogDatabaseAccessor.java) / [ScoreDataLogDatabaseAccessor.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ScoreDataLogDatabaseAccessor.java) / [SongInformationAccessor.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/song/SongInformationAccessor.java)

### 1.2 リプレイ `.brd`（`player/<name>/replay/*.brd`）

- GZIP圧縮JSON。`keyinput` は URL-safe Base64 + GZIP
- 1入力イベント = 符号付きキーコード1byte + LE時刻8byte（**μs単位、押下/離上の両方**）
- 加えて `sha256`, 開始時の選択ゲージ, **実際のレーンシャッフル**, BMSの`#RANDOM`選択, RANDOMオプションとseed, 日時
- ⚠️ **ノート単位の判定結果は保持していない。** FAST/SLOWやミス位置を出すには 譜面 + 実配置 + beatoraja判定ロジック との再照合が必要
- ⚠️ 自動保存条件つき4スロットのみ → 全プレイ履歴にはならない
- ⚠️ `ReplayData.gauge` は `config.getGauge()`、つまり**開始ゲージ**。結果ランプ `clear` はアシストやGauge Auto Shift後の状態を反映し、同義ではない
- 選択ゲージは `ASSIST_EASY / EASY / NORMAL / HARD / EXHARD / HAZARD`。`LIGHT_ASSIST_EASY` はゲージではなくランプ

出典: [ReplayData.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ReplayData.java) /
[BMSPlayer.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/play/BMSPlayer.java#L861-L900) /
[ClearType.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/ClearType.java#L10-L20) /
[GaugeProperty.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/play/GaugeProperty.java#L22-L29) /
[KeyInputLog.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/input/KeyInputLog.java)

→ Step 3では `gauge` / seed / 実配置 / `#RANDOM`選択などの**メタデータだけ**を読む。
`keyinput` の復号と「なぜ落ちたか」の診断はP7以降。着手前にWindows実機で生成・上書き条件を確認する。

### 1.3 難易度表フォーマット

header（慣例名 `header.json`、参照先は `data_url` が決める）:
`name`, `symbol`, `tag`, `data_url`, `level_order`, `mode`, `course`, `attr`

data（`score.json` / `body.json`）: 配列
`level`(必須), `md5` or `sha256`(いずれか必須), `title`, `artist`, `url`, `url_diff`, `url_pack`, `name_pack`, `org_md5`, `mode`, `comment`, `state`, `tag`

HTMLの `<meta name="bmstable" content="(header URL)">` からheaderへ誘導する形式もある。LR2は主にMD5、beatorajaはSHA256も可。
出典: [難易度表の仕様 (jbmstable-parser Wiki)](https://github.com/exch-bms2/jbmstable-parser/wiki/難易度表の仕様)

表の関係（**安全な整理**）:
- Normal(`☆`) → Insane/発狂 GENOCIDE(`★`) → Overjoy(`★★`) が旧来のSP系列
- **Satellite(`sl`)** = Insane側の代替表、概ね☆11〜★19。EASY基準、ギミック少なめ、地力の段階練習向けに1,000譜面以上
- **Stella(`st`)** = Overjoy側の代替表、★19超
- `slN ↔ ★N` の直接変換は**できない**。`δ`はDP系なのでSPの能力尺度に混ぜない
- 参考: [Satellite/Stella 公式](https://stellabms.xyz/) / [Stella/Satellite推定難易度表ミラー](https://dainihokan.onushi.com/suitei/)（EASY/NORMAL/HARD/FC別 + ST_* + DP_* を表形式で配信、最終更新2025-01-25）

### 1.4 IR の状況

| サービス | API | 方針 |
|---|---|---|
| Mocha IR | 公開API仕様は確認できず | HTML大量取得は避け、運営許諾を取る |
| LR2IR | `getrankingxml.cgi` / `getplayerxml.cgi` 等のCGI/XMLあり | 技術的には可能。低頻度 + キャッシュ + 許諾確認 |
| MinIR | 公開フロントがLambdaのJSON GETを利用（`song/score/list`, `user/event`） | 内部APIで公開保証なし。adapter化 |
| Stairway / Cinnamon | 公開API仕様不明 | 許諾不明 |

→ **MVPはIRなしで成立させる。**IRは後から「明示同意 / 低頻度キャッシュ / 交換可能adapter」として足す。
→ 統計的注意: IRの未プレイは**ランダム欠測ではない**（選曲・粘着・所持状況に強く偏る）。

---

## 2. 既存ツールと「空白」

| ツール | 強み | やっていないこと |
|---|---|---|
| [bms-nexus](https://github.com/c-ikeda123/bms-nexus) | `score.db` + IR集計で2PL風IRT。Easy/Normal/Hard/FC別の**1次元能力**、推薦・逆推薦、ローカルHTTPで難易度表配信（Flask） | 時系列・多次元傾向・忘却・疲労・セッション配合 |
| [oraja-constellator](https://github.com/DJHemorrhoid/oraja-constellator) | BMS解析による腕ガチ/指ガチ微縦連/16分乱打/ディレイ分類。密度・経過日数・ランプ・BP・スコア率フィルタ、ランダムコース生成。`songdata.db`に`bmscf_`テーブルを書く | 個人別成功確率と学習効果の推定 |
| LR2IR「リコメンド」 | **IRTの多段階反応モデル**でランプ群から実力を最尤推定。事実上のデファクト指標 | [解体記事](https://terrastellal.hatenablog.com/entry/mythbusting)が指摘: NOPLAY→EASYの寄与が小さくFAILEDリスクあり、指標依存で難易度感覚が壊れる。「ランプ更新の当たりを付ける参考程度」が適正 |
| 推定難易度表（dainihokan） | ランプ基準別の推定難度を**難易度表として直接配信** | 個人化なし |
| [beatman](https://github.com/esplo/beatman) | 導入・重複整理・未所持チェック、未達ランプから合計ノーツ数を満たす`task`、最終プレイが古い`oldest` | 能力推定・学習効果による順序付け |
| bmscorelogview / BMS Lamp Graph / [oraja_score_viewer](https://github.com/nerewid/oraja_score_viewer) | scorelog・ランプ分布の可視化、振り返り | 推薦しない |

### 埋まっていない空白（= 本ツールの居場所）

1. **多次元の個人差** — 1次元「地力」ではなく、乱打/縦連/トリル/皿/CN/ソフランの軸別に得手不得手を推定する
2. **プレイ単位データの活用** — `scoredatalog.db`（成功も失敗も残る）で**成功確率を校正**する。ランプだけの推定は情報を捨てている
3. **練習の"設計"** — 1曲の推薦ではなく **順序・量・再訪日**。運動学習の知見（干渉・間隔・変動）を組み込んだツールは存在しない
4. **効果の測定** — 即時PB数ではなく「翌日保持」「未練習の同傾向譜面への転移」で価値を測る

---

## 3. 上達（運動スキル獲得）の理論 → BMSへの写像

| 知見 | 出典 | BMSへの実装 |
|---|---|---|
| Deliberate practice | [Ericsson 1993](https://doi.org/10.1037/0033-295X.100.3.363), [Ericsson 2008](https://doi.org/10.1111/j.1553-2712.2008.00227.x) | 1ブロック1目標（例「縦連区間のBP率3%以下」）。プレイ回数でなく**エラー位置・BP・判定**を即時フィードバック |
| Desirable difficulties | [Bjork & Bjork 2020](https://www.unh.edu/teaching-learning-resource-hub/sites/default/files/media/2023-06/itow-introducing-desirable-difficulties-into-practice-and-instruction-bjork-and-bjork.pdf) | RANDOM/MIRROR、近傾向・別密度を混ぜる。**練習中のスコア低下と翌日の保持を分けて評価** |
| Contextual interference | [Shea & Morgan 1979](https://doi.org/10.1037/0278-7393.5.2.179), [メタ分析 2024](https://www.nature.com/articles/s41598-024-65753-3) | 新運指は2〜3曲の小ブロック、その後 乱打→皿→縦連 と交互化。初心者ほど干渉を弱く。※応用場面では効果が小さいという[反証メタ分析](https://www.sciencedirect.com/science/article/abs/pii/S1747938X23000301)もある → 設定可能パラメータにする |
| Spacing effect | [Cepeda 2006](https://doi.org/10.1037/0033-2909.132.3.354) | 同曲粘着を抑え、翌日→3日→7日→14日で再測定 |
| Variable practice | [Schmidt 1975](https://doi.org/10.1037/h0076770) | BPM・密度・左右偏り・同時押し率を変える。**狙う要素以外は近い譜面**にして何が効いたか識別可能に |
| Challenge Point / ZPD | [Guadagnoli & Lee 2004](https://doi.org/10.3200/JMBR.36.2.212-224) | 目標成功確率 `p=0.65〜0.80` を**初期ヒューリスティック**として採用。「85%ルール」は分類学習の結果でありBMSに直接適用できない |
| Performance ≠ Learning | [Soderstrom & Bjork 2015](https://doi.org/10.1177/1745691615569000) | **セッション内の好成績を学習とみなさない**。本ツールの評価軸そのもの |
| IRT / MIRT | LR2IRリコメンド、bms-nexus が採用済 | `P(success)=logistic(aᵢᵀθ − bᵢ)`。ランプ別難度 × 傾向別能力 |
| Elo / Glicko | [Glickman 1999](https://doi.org/10.1111/1467-9876.00159) | 小データ向けオンライン更新。**RDを「能力の不確実性」として探索推薦に使う** |
| BKT / DKT | [Corbett & Anderson 1994](https://doi.org/10.1007/BF01099821), [Piech 2015](https://arxiv.org/abs/1506.05908) | 乱打・皿等をKCとした習得確率。DKTは大規模IR時系列が要るのでMVPには過剰 |
| 忘却曲線 / FSRS | [Bahrick 1991](https://doi.org/10.1111/j.1467-9280.1991.tb00175.x), [FSRS](https://github.com/open-spaced-repetition/free-spaced-repetition-scheduler) | 最終プレイからの経過を状態減衰に。**当日の疲労低下と長期忘却を分離**。FSRSは「定着済み譜面の再訪日決定」に限定して応用（伸長用の`p≈0.7`と保持用の高成功率を混同しない） |

**設計上いちばん重要な帰結**：セッション内の好成績を「学習」とみなさない。価値は**翌日保持**と**同傾向の未練習譜面への転移**で測る。

---

## 4. アーキテクチャ

```
beatoraja DBs (read-only)       .brd (Step 3)       難易度表
score / scoredatalog / songdata / songinfo          header/score.json
             │                    │                       │
             └──── Collector / Replay scan / Table fetch ┘
                                  ↓
                          assistant.db (自前)
                 正規化履歴 / 譜面特徴 / モデル / メニュー
                                  ↓
                    ローカルWeb UI + 難易度表配信
```

### 4.1 データ層の原則

- beatoraja DBには**一切書き込まない**。書き込み先は `assistant.db` のみ
- ライブ読取は `mode=ro` + `busy_timeout`。静的backfillだけ `immutable=1` を使う
- `scoredatalog.db` と `score.db` の両方で、読取前後のDB/WAL/SHM署名と `PRAGMA data_version` を検証する
- `(source_generation, sha256, mode, playcount)` をイベントキーとし、取りこぼしは `lost_events` で可視化する
- `score` は外部ベストを含むbaseline、`scoredatalog` はローカル直近結果として役割を分離する

### 4.2 譜面特徴量

難度は難易度表を事前値にし、v1の傾向特徴は `songinfo` から構築する。BMSパーサは作らない。

- **密度**: 平均・p90・p99、終盤密度、最大バースト
- **皿**: 皿率、皿密度p90、皿複合率
- **LN**: LN率
- **ソフラン**: 速度分散、速度変更回数、STOP数
- **基本量**: 曲長、総ノーツ数

RANDOM使用率が高いため、固定レーン前提の左右偏り・縦連・トリル等はv1の成功確率モデルへ入れない。
WARMUPの安全フィルタに限り、既存の`bmscf_chart_analysis`を検証済みallowlistでread-only取込する。
`grid_bpm`は高速交互の危険度proxyとしてのみ使い、直接のトリル判定とは扱わない。
`stream_sec` / `last_kill`と譜面内密度比で、持続発狂・終盤発狂・非平坦な密度推移を除外する。
解析が無い譜面は低負荷と推定せず、保守的な負荷penaltyと警告を付ける。本ツール自身は
constellator解析を実行せず、beatoraja側DBへ書き込まない。

### 4.3 スキル推定モデル

MVPでは二段階モデルから始める。

```
1. 完走モデル: C ~ Bernoulli(P(survival >= 0.95))
2. 完走時BP  : logit((minbp + 0.5)/(notes + 1)) を Ridge 回帰
```

- `is_course=1`、`ir_import`、開始ゲージ不明を除外する
- `.brd` の `selected_gauge_kind` を開始条件に使い、結果ランプ由来の `credited_gauge_kind` を説明変数へ混ぜない
- 表ごとのレベル効果を別の単調曲線として扱い、単一難度軸へ潰さない
- 特徴軸の追加は、十分な標本・leave-one-session-out改善・bootstrapでの符号安定を全て満たす場合だけ
- 評価は時間順 log loss / Brier / 校正曲線 / レベルのみbaseline / Top-12 Jaccard。Accuracyは使わない
- UIでは「学習効果はヒューリスティック、成功確率のみ統計モデル」と明記する

### 4.4 メニュー生成

12枠セッションの基本構成:
1. **ウォームアップ 2枠**
2. **focus 4枠** — 2譜面を各2回。再試行は3〜5曲離す
3. **交互練習 3枠** — focusと異なる傾向
4. **保持 2枠** — 再訪期限が来た既クリア譜面
5. **標準probe 1枠** — 効果測定用の固定条件

制約付きgreedyで並べる:
- 同一傾向を3曲以上連続させない
- 高負荷（皿・縦連・腕押し）を連続させない
- 同曲・同アーティスト・同差分作者への露出ペナルティ
- セッション予算はノーツ数だけでなく**予測時間と負荷**で制限

スペースドリピティション: 当初は成功時 1→3→7→14日 / 失敗時は短縮の簡易方式。データが溜まったらFSRS型（Difficulty / Stability / Retrievability）へ置換。

提示枠の20〜30%は同じ表・レベル・既プレイ状態からランダム抽出する探索枠とし、
候補集合と選択確率を保存してprequential評価に使う。

### 4.5 beatoraja への出力

**第一候補: ローカルHTTPサーバで難易度表配信**（bms-nexus と同じ機構）
```
http://127.0.0.1:<port>/table/header.json
```
- `score.json` の `level` を `01 WARMUP` / `02 FOCUS` / `03 INTERLEAVE` / `04 REVIEW` / `05 PROBE` にする
- Web UI側で順序、目標ゲージ、予測成功率と信頼区間、直近結果、`.brd`突合率、最終収集時刻、`lost_events`を表示

**副案: `folder/*.json`（カスタムフォルダ）**
実際の形式（[beatoraja/folder/default.json](https://github.com/exch-bms2/beatoraja/blob/master/folder/default.json)）:
```json
[
  { "name": "MY BEST", "sql": "playcount > 0 ORDER BY playcount DESC LIMIT 10" },
  { "name": "CLEAR TYPE", "folder": [
      { "name": "FULL COMBO", "sql": "score.clear >= 8" },
      { "name": "EX HARD CLEAR", "sql": "score.clear = 7" } ] }
]
```
キー: `name` / `sql` / `folder`(ネスト) / `showall`。ハッシュ集合は `sha256 IN (...)`、順序は `ORDER BY CASE` で表現できるが長大になる。

**リアルタイム性の限界（重要）**
- Web UI: `scoredatalog.db` を数秒間隔で監視 → **リザルト後ほぼ即時更新可能**
- beatoraja内フォルダ: 外部JSON/難易度表の自動ホットリロード保証はない。表更新→フォルダ再オープン、または再起動が必要。**プレイ中のコースは更新できない**

→ 「セッション単位で確定して配信、詳細フィードバックはWeb UI側」という役割分担にする。
course生成とプレイ中の動的再推定はv1の非目標。

---

## 5. 差別化（既存3ツールに対する位置づけ）

| 相手 | 差分 |
|---|---|
| bms-nexus | 1次元IRTの「次にクリアできそう」ではなく、**多次元 × 時間変化 × 疲労 × 保持** で「何曲・どの順・いつ再訪」を出す |
| oraja-constellator | 手動ルール抽出ではなく、**個人履歴から成功率と学習効果を校正**。constellatorの特徴抽出は入力資産として再利用可 |
| lamp viewer 各種 | 過去の可視化ではなく、**翌日保持と別譜面転移まで測る閉ループ** |
| LR2IRリコメンド | 単一スカラー指標ではなく**軸別の弱点診断**。指標そのものを目的化させない設計（[解体記事](https://terrastellal.hatenablog.com/entry/mythbusting)の批判への回答） |

---

## 6. MVP スコープ

### 捨てるもの
- IR連携の必須化（後付けadapter）
- DKT / 本格FSRS
- 全リプレイの判定再現
- BMSパーサと固定レーン前提の特徴
- course生成とプレイ中の動的再推定
- **beatoraja DBへの書き込み**
- ネイティブGUI

### 作るもの
1. `scoredatalog` / `score` / `songinfo` / `songdata` のread-only取込とプレイ差分収集
2. 難易度表のキャッシュ取得とsha256/md5突合
3. `songinfo` による密度・皿・LN・ソフラン特徴
4. `.brd` メタデータによる開始ゲージ・実配置の収集
5. 表レベル事前値 + 傾向特徴の校正済み二段階モデル
6. 12枠セッションメニュー + ローカル難易度表配信 + Web UI

### 検証するもの
**2週間の自己実験で、「同レベルからランダム選曲」というベースラインを以下で上回るか:**
- 翌日保持（前日クリアした譜面の翌日成功率）
- 別譜面転移（同傾向の未練習譜面での改善）
- Brier score の校正（予測成功率が実測と合っているか）

---

## 7. 技術スタック

| 層 | 選択 | 理由 |
|---|---|---|
| コア | **Python 3.11+**（標準 `sqlite3` / `http.server`） | Windowsへ依存なしで配置でき、モデル・特徴量・推薦ロジックを一体運用できる |
| UI | **ローカルWeb**（最初はJinja+HTMX or Vanilla JS） | 同一プロセスから beatoraja向け難易度表も配信できる。TS/Reactは分析UIが複雑になってから |
| CLI | 併設 | DB診断・特徴再計算・モデル評価・JSON出力の管理用 |
| 将来 | Rust（BMS大量解析 / 単一exe配布 / ファイル監視） | 最初から全面Rustにするとモデル変更コストが高い |

---

## 8. マイルストーン

| Phase | 内容 | 状態 / 完了条件 |
|---|---|---|
| **P0a: DB実機調査** | 5DBのスキーマ・件数・意味を実測 | **完了** |
| **P0b: Replay実機調査** | Windowsで `.brd` の生成条件・上書き条件・保存率を確認 | **未完了**。Step 3の前提 |
| **P1a: Collector基盤** | read-only backfill、差分収集、schema v4、入力整合性検証 | **完了**。静的2DBの日次取込と重複・巻戻し検出を実装 |
| **P1b: 実運用開始** | 実用 `assistant.db` を作りcollectorを常駐 | **実装完了・実機登録待ち**。`%LOCALAPPDATA%\oraja-training` とログオン時Scheduled Taskを採用。Windows接続回復後に登録・連続稼働を確認する |
| **P2: 難易度表・特徴量** | 表取得/突合と `songinfo` 特徴を構築 | **完了**。ETag/last-good対応、実データ65,712/65,998譜面（99.57%） |
| **P3: Replayメタデータ** | 開始ゲージ・seed・実配置を収集 | **実装完了・実機検証待ち**。制限付きmetadata scanner、slot履歴、一意突合と監査カウンタを実装 |
| **P4: モデル** | 段階モデルを時間順holdoutで評価 | **実装済み**。十分な履歴とゲート通過までは決定的cold-startを使用 |
| **P5: メニュー + 配信** | 10万判定メニュー、ローカル難易度表、最小Web UI | **実装済み**。Personal/Today表、Webキュー、疲労日モードを生成 |
| **P6: 自己実験** | 翌日保持・転移・校正をランダム選曲と比較 | **実験基盤実装済み・実測待ち**。session単位の決定的coach/control割付、候補hash・選択確率、1/3/7/14日保持/転移target、play解決、Brier/arm差と最小標本ゲートをschema v5とCLIへ実装。完了条件は2週間の実測結果が出ること |
| **P7以降** | 状態空間化、リプレイ判定再現 | ローカルMVP後。公式IR・Cloudflare・一般公開は親Epic #1 の Phase 0〜5（#24〜#29）と実装issue #2〜#23で管理する |

---

## 9. リスク・未検証項目

| 項目 | リスク | 対応 |
|---|---|---|
| collector停止中の取りこぼし | `scoredatalog` は上書き式で過去を復元できない | 常駐化し、`playcount` 差を `lost_events` として記録 |
| 2DBの非原子的更新 | 直近結果とベスト集約を別時点で読む | 両DBの署名と `PRAGMA data_version` を前後検証して再試行 |
| 開始ゲージ不明 | Step 1だけでは全行 `selected_gauge_kind=NULL` | `.brd` の実機確認後に収集。`credited_gauge_kind` で代用しない |
| 一人分のデータ量不足 | 過学習 | 表レベルを強い事前値にし、軸追加ゲートと不確実性表示を守る |
| RANDOMで固定配置特徴が変動 | 誤った傾向推定 | v1では固定レーン特徴を作らず、配置非依存の `songinfo` 特徴に限定 |
| 難易度表レベルの粒度 | 同レベル内の個人差が大きい | 表別の単調曲線と少数の特徴軸を併用 |
| IR由来集約の混入 | 教師ラベルが歪む | `ir_import` の派生値をNULLにして学習から除外 |
| 指標の目的化 | 難易度感覚が単一指標へ引っ張られる | 軸別の弱点と次の行動を主役にし、baselineとの差を明示 |
| 効果が出ない可能性 | 運動学習知見がBMSへ転移しない | §6の自己実験をゲートにし、効果がなければ設計を見直す |

---

## 10. 次のアクション

1. Windowsで `install-collector-task.ps1` を実行し、再ログオン後の自動復旧とsource DB不変性を確認する
2. モデル開発と並行して2〜4週間のデータ蓄積を始める
3. beatorajaの自動リプレイ保存1枠を `ALWAYS` にし、Windowsで `.brd` の生成・上書き条件を確認する
4. 難易度表の取得・キャッシュ・sha256/md5突合を実装する
5. `songinfo` 特徴、Replayメタデータ、モデル、メニュー、配信の順に進める

運用メモ: このプロジェクトはGitHubリポジトリで管理し、コード変更は issue 起点とする。
親Epic #1 の実装issueは、ユーザーの明示承認、専用ブランチ、受入確認の順に進める。

---

## 11. P0 実機調査結果（2026-08-09 実施・実DBで検証）

対象: リポジトリ外に配置した検証用5DB。全て read-only（`immutable=1`）で参照。

### 11.1 いちばん重要な訂正 —— `scoredatalog.db` は全プレイログではない

実スキーマ: `PRIMARY KEY(sha256, mode)`。つまり**プレイのたびに上書き**され、1譜面につき1行しか残らない。

| 実測 | 値 |
|---|---|
| `score` 行数 | 933譜面 |
| `scoredatalog` 行数 | **384**（`score`の部分集合、log_only=0件） |
| `scorelog` 行数 | 465 |
| `scoredatalog` / `scorelog` の期間 | 2026-05-27 〜 2026-08-08（この日にbeatoraja側の記録が始まった） |
| `scoredatalog.date` / `.playcount` と `score` の一致 | **384/384 で完全一致** |

**では何が入っているのか** —— `score` はベスト集約、`scoredatalog` は**直近プレイのスナップショット**。証拠:

| 比較 | 件数 |
|---|---|
| `scoredatalog.clear < score.clear`（直近プレイのランプがベスト未満） | **46** |
| うち「直近=FAILED(1) かつ ベスト≧EASY(4)」 | **19** |
| EXスコアが `scoredatalog < score` | **106** |
| `clear` 分布（直近プレイ） | NoPlay 12 / Failed 100 / LAE 35 / Easy 78 / Normal 37 / Hard 99 / ExHard 22 / FC 1 |

→ **失敗プレイも非更新プレイも確かに記録されている。**`scorelog`（更新時のみ）では絶対に取れない情報。
ただしLAE 35は結果ランプであり、開始ゲージではない。結果側ゲージを一意に推定できるのは
Easy 78 / Normal 37 / Hard 99 / ExHard 22の計236行で、残る148行は不明。

**アーキテクチャ上の帰結（最重要）**

> 過去分は「1譜面あたり直近1プレイ」しか遡れない。しかし**常駐コレクタが `scoredatalog.db` の変更をポーリングして差分を保存すれば、それ以降はプレイ単位ログを構築できる。**ポーリング間に同一譜面を複数回プレイした場合、最新結果以外は復元できないが、`playcount` 差から欠落数を検知できる。
>
> つまり本ツールは**Day 1から常駐プロセスとして動かさないと学習データが一切貯まらない。**「使いたい時に起動する分析ツール」では成立しない。これはMVPの形そのものを規定する。

`score.db` の `date`/`playcount` も同時に更新されるので、両者を併用すれば「今回のプレイでベスト更新したか」も差分から復元できる。

### 11.2 難易度レベルは `song.level` を使えない

`song.level` は BMS ヘッダの `#PLAYLEVEL` そのまま。プレイ済み933譜面の分布は `12`(259) / `11`(89) / `13`(61) / `0`(59) / `10`(56) … に加えて `24`, `10000` も混在。作者申告値なので**能力尺度として使用不可**。

→ **難易度表（Satellite / Stella / GENOCIDE / 推定難易度表）を md5・sha256 で突合するのが必須。**P1 Collector の中核作業。

### 11.3 `songinfo.distribution` が想定以上に使える

[SongInformation.java](https://github.com/exch-bms2/beatoraja/blob/master/src/bms/player/beatoraja/song/SongInformation.java) で形式を確認:

- **1秒ごとに7列、各列を base36 2文字**（`0-9a-z`、最大 36²−1）でエンコードした文字列
- 列順: `[0]`LN皿 `[1]`皿密度 `[2]`皿ノーツ `[3]`LN鍵 `[4]`鍵密度 `[5]`鍵ノーツ `[6]`地雷
- 実測でも 109秒の曲 = 1555文字（1 + 109×14.25）で整合
- `speedchange` = `speed,time_ms` の繰り返し / `lanenotes` = レーンごとに `normal,long,mine` の3値

**BMS解析なしで即座に取れる特徴**（ライブラリ全66,156譜面分）:
密度カーブ（平均・p90・p99・終盤・バースト）/ **皿密度と鍵盤密度の分離** /
**皿複合（同一秒に皿と鍵盤が同時に高い）** / LN率 / 地雷 / ソフラン（`speedchange`の分散・変更回数・STOP数）

**BMS本体解析が必要なもの**: 乱打（16分系列・レーンエントロピー）/ トリル / 縦連・微縦連 / 同時押しサイズ

→ v1は `songinfo` だけで開始し、BMSパーサは作らない。RANDOM使用率が365/384と高いため、
固定レーン配置に依存する特徴も採用しない。BMS本体解析はMVP後に別途評価する。

### 11.4 IR取り込みデータの混入 —— モデル学習で分離が必要

| `score.date` | 行数 | `SUM(playcount)` |
|---|---|---|
| `= 0`（日時なし = **IR取り込み由来**） | 545 | 1318 |
| `> 0`（自環境プレイ） | 388 | 789 |

`SUM(score.playcount)` = 2107 に対し、`player` テーブルの累積 `playcount` は 507。**大半はMocha IR等からのインポートで、自分の手で叩いた記録ではない。**

追加実測では、IR行の `lpg/lgr` は全行0で、判定10列合計が `notes` を超える行が297/298あった。
これは1プレイの判定内訳ではない。backfillでは未プレイ3行を除いた542行を `ir_import` として保存し、
`judged` / `empty_poor` / `survival` / `completed` は全てNULLにする。モデル教師には使わない。

### 11.5 ライブラリと突合率

| 項目 | 値 |
|---|---|
| `song` 行数 | 66,156 |
| `songinfo.information` 行数 | 65,998 |
| プレイ済み933譜面のうち `song` に存在 | **929 (99.6%)** |
| 同 `songinfo` に存在 | **929 (99.6%)** |

→ sha256 での突合はほぼ完全。欠損4件は削除済みファイル等と推定。

### 11.6 oraja-constellator が導入済み

`songdata.db` に `bmscf_*` テーブルが存在（`bmscf_base_membership` 155,313行 / `bmscf_density_membership` 290,720行 / `bmscf_filter_condition` 52行）。

ただし **`bmscf_chart_analysis` は 0行**（譜面解析が未実行、または初期化済み）。

→ 2つの含意:
1. constellator の解析結果は、存在する場合だけWARMUP安全フィルタへread-only取込する。成功確率モデルには使わず、解析実行も本ツールのスコープ外
2. 同時に、**本ツールが `songdata.db` に書き込むと衝突する**。§4.1の「beatoraja DBには書き込まない」方針は正しかった

### 11.7 プレイヤープロフィール（＝ MVPの想定ユーザーは自分）

| 項目 | 値 |
|---|---|
| プレイモード | **7key SP のみ**（933譜面中932。DP・5keyはほぼゼロ） |
| 譜面の出所 | 全て `Insane BMS (2025-12-14)` パック配下 = **発狂BMS** |
| 記録期間 | 2026-05-27 〜 2026-08-08（`player` テーブル26日分） |
| 累積 | 507プレイ / 約17時間 |
| 直近セッション（8/8 22:43〜23:37, 約1時間） | 20プレイ。うち **FAILED 14, EASY 4, NORMAL 1, その他1** |
| 直近セッションの譜面選択 | `#PLAYLEVEL` で 0〜21 まで散らばり、同一譜面の連続試行なし |

**観察**: 1曲あたり約3分で次々に別譜面へ移り、失敗率70%。これは「ランダムに埋めている」状態で、本ツールが解こうとしている問題そのもの。ただし**モデル学習データとしては388プレイ分しかなく、しかも大半が1譜面1〜2回**。

→ **§9の「一人分のデータ量不足」リスクが現実の数字として確認された。**表別レベル効果を強い事前値にし、
個人側の特徴も最初は2〜3軸に絞らないと過学習する。

### 11.8 P0・Step 1レビューを受けた確定事項

| # | 修正 |
|---|---|
| 1 | **常駐コレクタが必須。**`scoredatalog.db` をポーリングし、自前の `plays` へ差分を追記する。停止中の欠落は `lost_events` で検知する |
| 2 | `judged` は空POORを除く10列、`empty_poor=ems+lms`。完走時 `survival` は厳密に1.0 |
| 3 | `score` のIR集約は派生4項目をNULLにし、モデル教師から除外する |
| 4 | `clear` は結果ランプ、`.brd.gauge` は開始ゲージ。`credited_gauge_kind` と `selected_gauge_kind` を分離する |
| 5 | `clear=2/3` は開始ゲージを復元できない。結果側推定は `clear=4..7` のみ |
| 6 | schema versionは3。version 1から復元不能なのでin-place移行せず、version 2は履歴を保持してversion 3へ移行する |
| 7 | v1特徴量は `songinfo` に限定し、BMSパーサ・固定レーン特徴・RANDOM Monte Carloを作らない |
| 8 | 難易度表突合は必須。表ごとのレベル効果は単一軸に潰さない |
| 9 | コレクタ稼働後2〜4週間は、予測より記録と可視化を優先する |

### 11.9 未解決・次に確かめること

- Windows実機で `.brd` の生成条件・上書き条件・自動保存率を確認する
- `.brd` と `plays` の突合率を測り、`selected_gauge_kind` がどの程度回収できるか確認する
- 直近プレイに BP が総ノーツ数に近い記録（例: 3414ノーツ中BP 3182）がある —— 放置・中断の可能性。**外れ値フィルタの基準を決める必要がある**
- `player` テーブルの26行が何単位か（日次スナップショットに見えるが `date` が日境界に揃っている）
- Windows実機でScheduled Task登録、再ログオン後の自動復旧、source DB不変性を確認する

解決済み: `scoredatalog.clear=0` の12行はコースプレイ、`score.clear=0` はNoPlay。
`scorelog` のカラム差は `PRAGMA table_info` による実行時検出で対応済み。

---

## 12. Step 1 実装状況（2026-08-09）

- read-only reader、schema v4、backfill、content-diff Poller、静的2DB日次取込を実装済み
- Replayはobject形式のGZIP JSONだけを制限付きで読み、`keyinput` を復号・保存しない。slot上書き履歴と未一致・曖昧・破損カウンタを保存する
- Replay日時以降30秒以内の `sha256 + mode` がただ1件のplayへ対応した場合だけ `selected_gauge_kind` を更新する
- `initialize` / `daily-update` により入力DBへWAL等を書かず、SHA-256で同一提出を冪等化
- 初期実測値は個人データ由来のため公開版から除外。再現可能な合成fixtureを回帰基準とする
- legacy: max survival 1.0 / `survival>1` 0 / completed 368 / `judged=notes` 272/272
- credited gauge: HARD 99 / EASY 78 / NORMAL 37 / EXHARD 22 / NULL 148
- `selected_gauge_kind` はStep 1では384/384 NULL
- 全テスト成功。入力DBのハッシュ・mtimeは前後一致し、WAL/SHMは生成されていない

---

## 13. #1 親Epicの現状監査（2026-08-11）

### 13.1 仕様の読み分け

この文書の前半（§1〜§12）は、ローカル self-hosted MVP の調査結果と実装計画で
ある。公式収集サービスの目標状態は、親Epic #1 と `docs/architecture.md`、
`docs/api-contract.md`、`docs/contracts/` が定義する。両者を同じ「完了」として
数えない。

`SPEC.md` の A-0 にもこの境界を記載した。したがって、v1 の「IR連携なし」は
公式IRを永続的に禁止する意味ではなく、ローカルMVPの受入範囲を示す。

### 13.2 現行実装と #1 の目標との差分

| 境界 | 監査時に確認できる実体 | 判定 | 担当issue |
|---|---|---|---|
| ローカル収集・推薦 | `src/oraja_training/collect`、`db/store.py`、`serve/app.py` と既存pytest | ローカルMVPの範囲で実装済み。単一 `player_name`、`assistant.db`、localhost配信のまま | #3/#4、既存MVP |
| Phase 0 契約 | `docs/architecture.md`、ADR、`docs/contracts/`、契約テスト | 設計成果物はある。GitHub上の #2 は受入・クローズ前であり、サービス実装完了の証拠ではない | #2 |
| Cloudflare基盤 | `cloudflare/wrangler.jsonc`、migration、Workerのhealth/version/auth、設定検査 | 作業ツリーに基盤草案はあるが、Profile DO/Container/Workflowはhealth骨格。preview/staging/productionのデプロイ証跡なし | #5〜#11 |
| 公式IR | `cloudflare/ir/README.md` はJava/Gradle境界の予約だけ | `IRConnection` JAR、`IR_SEND_ALWAYS`、spool、ACK/再送は未実装 | #12/#13 |
| 5DB提出・生成・表配信 | service contract上のAPI定義のみ。現行CLIは2DBのローカル提出、表はlocalhost配信 | multipart、暗号化R2、Container生成、revision公開、capability URLは未受入 | #9〜#14 |
| Web・削除・復元 | 最小静的Webと認証骨格、既存migration/rollbackメモ | 日韓ダッシュボード、export、7日取消、30日backup、`deleteAll()`/restore drillは未完了 | #15/#16 |
| OAuth / Remote MCP / AI記憶 | Workerの `/mcp` route、OAuth metadata、Resources/Tools、journal受入の証跡なし | 未実装 | #17〜#19 |
| β・OSS・一般公開 | `LICENSE`、`SECURITY.md`、`CONTRIBUTING.md`、30日運用記録、release artifactなし | 未実装。公開・改名・一般登録・本番deployは実行していない | #20〜#23 |

「作業ツリーに存在する」「設計に書かれている」「受入済み」「デプロイ済み」は
別の状態である。特に未コミットの別worker成果物や `.example.invalid` の環境設定は、
本番接続・実データ移行・公開の証拠として扱わない。

### 13.3 親Epicの完了ゲート

親Epic本文のチェックリストだけで子issueを完了扱いにしない。Phase Epic #24〜#29
と実装issue #2〜#23のGitHub上の受入状態を正とし、次の順序を守る。

1. #2〜#4で契約、合成fixture、domain coreの受入を完了する。
2. #5〜#19で公式基盤、収集、生成、Web、削除、OAuth、MCP、AI journalを実装し、
   自動試験と越境negative testを通す。
3. #20で脅威モデル・SLO・復元/削除runbook、#21で招待β30日、#22でOSS公開準備を
   完了する。
4. #21/#22のgo判定後に限り、#23のlaunch runbookを別途承認して改名・公開・一般登録・
   本番deployを実行する。

監査時点では #2〜#23 はすべてOPENであるため、#1 は未完了である。今回の担当では
issue状態の変更、実装issueの代行、外部環境への接続・公開を行わない。

### 13.4 外部操作の扱い

デプロイ、migration、実データ移行、GitHub公開/改名、一般登録は、
[`cloudflare/runbooks/epic-1-release-gates.md`](cloudflare/runbooks/epic-1-release-gates.md)
の承認ゲートと証跡様式に従う。runbookは手順と停止/rollback条件だけを定義し、
この監査ではコマンドを実行しない。

## 参考リンク

**既存ツール**
- [oraja-constellator](https://github.com/DJHemorrhoid/oraja-constellator) / [bms-nexus](https://github.com/c-ikeda123/bms-nexus) / [beatman](https://github.com/esplo/beatman) / [oraja_score_viewer](https://github.com/nerewid/oraja_score_viewer) / [bmscorelogview](https://voidproc.com/blog/archives/358) / [BMS Lamp Graph](https://lnt.softether.net/cgi-bin/beatoraja/index.php)

**仕様**
- [beatoraja](https://github.com/exch-bms2/beatoraja) / [難易度表の仕様](https://github.com/exch-bms2/jbmstable-parser/wiki/難易度表の仕様) / [第2通常難易度表:導入支援](https://bmsnormal2.syuriken.jp/bms_dtmanager.html)
- [Satellite/Stella](https://stellabms.xyz/) / [推定難易度表](https://dainihokan.onushi.com/suitei/)

**上達論・コミュニティ知見**
- [「リコメンド」の解体](https://terrastellal.hatenablog.com/entry/mythbusting) ← 最重要
- [BMSの練習方法（剣持）](https://note.com/kenmo_bms/n/nbf9140410903) / [発狂皆伝に合格するまで（またりん）](https://note.com/matarin/n/ndb6ce6b06ccd) / [BMS上達理論](https://kagetsu5737.com/bms-mainichi-zyoutatsu)

**学術**
- Ericsson 1993 / Bjork & Bjork 2020 / Shea & Morgan 1979 / [CI メタ分析 2024](https://www.nature.com/articles/s41598-024-65753-3) / [CI 反証メタ分析](https://www.sciencedirect.com/science/article/abs/pii/S1747938X23000301) / Cepeda 2006 / Schmidt 1975 / Guadagnoli & Lee 2004 / Soderstrom & Bjork 2015 / Glickman 1999 / Corbett & Anderson 1994 / [Piech 2015 (DKT)](https://arxiv.org/abs/1506.05908) / [Predicting Chart Difficulty in Rhythm Games](https://www.researchgate.net/publication/350066144)
