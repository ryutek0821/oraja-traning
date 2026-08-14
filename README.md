# oraja-training

beatoraja のローカルSQLite DBを**読み取り専用**で取り込み、プロフィール単位の
Personal Recommend難易度表と、1日10万判定ノーツの日替わりメニューを生成する
ローカルツールです。履歴・特徴・推薦結果は専用の`assistant.db`だけに保存します。

## セットアップ

Python 3.11以降を使用します。

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
pytest
```

## 初回セットアップ

beatorajaを停止するか、別途コピーした静的なplayer DBディレクトリを指定します。
backfillは入力DBを `immutable=1` で開き、書込み先は指定した`assistant.db`だけです。

```console
oraja-training initialize \
  --db-dir /path/to/beatoraja/player/PLAYER_NAME \
  --assistant-db ./assistant.db
```

このコマンドは5DBのbackfill、日次比較の基準点作成、`songinfo.db`の譜面特徴生成を
まとめて行います。同梱実データでは65,998譜面中65,712譜面（99.57%）に特徴が出ます。

次に難易度表を取得します。表ページ・header JSONのどちらも指定できます。

```console
oraja-training tables refresh \
  --assistant-db ./assistant.db
```

取得結果はETag/Last-Modified付きで`.cache/tables/`へ保存され、通信失敗時は最後の正常な
キャッシュを使います。既定ではGENOCIDE（発狂難易度表）、Overjoy、Satellite、Stellaの
4表を取得し、各表の尺度を混ぜずにクリアランプから適正帯を推定します。

schema version 1の既存`assistant.db`は正しい値へ復元できないため、in-place移行しません。
version 2以降は履歴を保持して段階的に移行します。現行schema versionは6です。

## 毎日の更新

プレー終了後にbeatorajaを終了し、`score.db`と`scoredatalog.db`を同じ時点でコピーします。
翌日分の通常メニューは次の1コマンドで生成できます。

```console
oraja-training daily-update \
  --score-db /path/to/submitted/score.db \
  --scoredatalog-db /path/to/submitted/scoredatalog.db \
  --assistant-db ./assistant.db \
  --output-dir ./export/current
```

疲労日は`--readiness tired`を付けます。10万打鍵の量は維持しながら、高負荷上位の
皿・瞬間密度譜面を避け、候補帯を易しい側へ移します。同一DBペアの再実行はno-opです。
片方だけ新しいDB、カウンター巻き戻り、SQLite sidecar付きのライブコピーは拒否します。

日次更新のたびに完走確率モデルも再評価します。200結果・10日へ達するまではcold-start、
到達後も時間順holdoutと日単位bootstrapの改善ゲートを通った版だけを推薦へ使用します。
これは普段の設定下での観測完走確率であり、開始ゲージ別のクリア確率ではありません。

### ウォームアップ選定

BMS固有の対照研究は見当たらないため、一般的なウォームアップ研究と鍵盤演奏の疲労研究を
プレーデータへ保守的に当てはめます。短時間で段階的に強度を上げるというレビュー知見と、
反復鍵盤動作による前腕疲労が打鍵精度を下げるという実験結果を根拠にしています
（[McGowan et al., 2018](https://pubmed.ncbi.nlm.nih.gov/29968230/)、
[Goubault et al., 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC8047012/)、
[Drinkwater et al., 2010](https://pubmed.ncbi.nlm.nih.gov/20795334/)）。

- 発狂・Overjoy・Satellite・Stellaを別尺度のまま扱い、各表のHARDクリア前線を推定
- HARD/EXHARD、または直近30日で2回以上完走かつBP 5%以下の譜面だけを採用
- 前線の2段階下から前線までを4曲以内で並べ、目標は更新ではなく`COMFORT`
- 終盤密度、瞬間発狂、皿、LN、ソフラン/停止、微縦連、長いジャック、同時押し、曲長の
  極端値を除外
- 疲労日や前回ウォームアップ不調時は帯を1段階下げ、条件を満たす曲がなければ空欄と警告

実打鍵は`player`テーブルのPGREAT～POORの10判定列の累積差分です。空POORは含めません。
日替わり本編は期待判定10万以上、失敗時の補填として約1万のRESERVEを追加します。

## beatoraja向け配信

```console
oraja-training serve \
  --export-dir ./export/current \
  --score-db /path/to/live/player/PLAYER_NAME/score.db
```

既定では`127.0.0.1:8765`だけで待ち受けます。

- コックピット: `http://127.0.0.1:8765/`
- Personal Recommend: `http://127.0.0.1:8765/table/recommend/header.json`
- 日替わり表: `http://127.0.0.1:8765/table/today/header.json`

beatorajaのResourcesへ2つの表URLを登録し、日次更新後に難易度表を再読込してください。
Web画面はライブ`score.db`を読み取り専用で確認し、基準点から10万までの進捗を表示します。
難易度表には同一譜面を1回だけ掲載し、focus譜面の2回目と3～5譜面の間隔はWebキューに
別スロットとして表示します。

`oraja-training review --assistant-db ./assistant.db`で直近日の打鍵、プレー数、ランプ・
EX・BP更新、日次提出で復元できなかったプレー数を確認できます。

## 2週間の自己実験

coach推薦と同レベルrandom controlは、session単位で決定的に割り付けます。まず実験を開始し、
表示された`experiment_id`を以後のコマンドへ渡します。

```console
oraja-training experiment start --assistant-db ./assistant.db \
  --name p6-two-week --seed PRIVATE_FIXED_SEED --starts-at 1800000000
oraja-training experiment assign --assistant-db ./assistant.db \
  --experiment-id 1 --session-key 2026-08-15-am --session-at 1800000000 \
  --candidates-json ./candidates.json
oraja-training experiment resolve --assistant-db ./assistant.db \
  --experiment-id 1
oraja-training experiment report --assistant-db ./assistant.db \
  --experiment-id 1
```

`candidates.json`は`coach`、`control`、`transfer`の各配列を持ち、要素は
`{"sha256":"…","mode":0,"p_pred":0.7}`です。候補集合hash、arm確率、譜面の
selection probabilityを保存し、同じsession keyの再実行は同じ割付を返します。選曲した譜面の
保持と未練習類似譜面への転移を1/3/7/14日後に評価します。期限内のplayが一意な場合だけ解決し、
複数は`duplicate`、未観測は`missing`として除外します。各armの事前最小標本数に達するまでは
arm差とBrier差を`inconclusive`として出しません。書込み先は`assistant.db`だけです。

### Codexでの日次・定期レビュー

プレー後に静的コピーした2DBをCodexへ渡し、「READMEの毎日の更新を実行し、review結果と
翌日メニューの偏りを確認して」と依頼してください。Codexは`daily-update`後、10万判定の
充足、ランプ更新、取りこぼし、モデル状態を確認できます。週1回程度は「難易度表もrefreshし、
突合率・特徴量生成率・モデル指標の推移を確認して」と追加すると、表の更新も追随できます。
提出前はbeatorajaを終了し、2DBを同じタイミングでコピーしてください。

### RYU-DESKTOP2での版切り替え

Codexが生成した`export/current`を任意の一時フォルダへ転送した後、PowerShellで検証・
有効化します。既存の有効版は削除せず、ポインタだけを切り替えるため戻せます。

```powershell
.\scripts\activate-release.ps1 -BundleDir C:\path\to\export\current
.\scripts\start-windows.ps1 `
  -ScoreDb D:\path\to\beatoraja\player\PLAYER_NAME\score.db
```

SSH利用時も同じbundleをRYU-DESKTOP2へ転送して`activate-release.ps1`を呼びます。
接続できない日はbundleを手動コピーして同じ手順を使えます。

## 常駐コレクタ

ライブDBは `mode=ro` と `busy_timeout` で開きます。既定では5秒間隔です。

```console
oraja-training collect --daemon \
  --db-dir /path/to/beatoraja/player/PLAYER_NAME \
  --assistant-db ./assistant.db
```

`scoredatalog.db` は譜面ごとの直近結果を上書きするため、**常駐が前提**です。
起動していない間の複数プレイは復元できず、最新行だけを保存して不足分を
`lost_events` として記録します。

Windowsでは、ユーザーPowerShellから次を実行するとログオン時のScheduled Taskとして登録できます。所有DBとログは `%LOCALAPPDATA%\oraja-training` に保存します。

```powershell
.\scripts\install-collector-task.ps1 `
  -DbDir "D:\path\to\beatoraja\player\PLAYER_NAME" `
  -StartNow
```

解除は同じコマンドへ `-Uninstall` を付けます。登録・解除は `OrajaTrainingCollector` だけを対象にします。

beatoraja側では、**自動リプレイ保存の1枠を `ALWAYS` に変更してください**。
collectorは `replay/*.brd` のGZIP JSONから、開始ゲージ・seed・実配置などの
allowlist済みmetadataだけを読みます。`keyinput` は復号・保存しません。圧縮/展開サイズを
制限し、読取前後でファイルが同一の場合だけ採用します。

Replayは `sha256 + mode + date` がただ1件のplayに完全一致した場合だけ
`selected_gauge_kind` を更新します。未一致・曖昧一致・破損・読取中の上書きはplayを
変更せず、tickの `replay_*` カウンタと `replay_metadata` のslot履歴で監査できます。

停止は `Ctrl-C` です。入力側の `.brd`、`score.db` / `scoredatalog.db` / `scorelog.db` /
`songdata.db` / `songinfo.db` には書き込みません。

## 同梱スナップショットで判明した仕様差

実DBでは同一SHA-256の重複行があり得るため、譜面数は行数ではなくSHA-256単位で数えます。
仕様DDLの `charts.sha256 PRIMARY KEY` を守るため、`charts` は65,998行になります
（重複158組はメタデータが同じでパスだけが異なり、辞書順最小パスを保持します）。

また `score.date = 0` の545行中3行は `clear = 0` かつ `playcount = 0` です。
これは未プレイなので、仕様の意味規則に従い `ir_import` は542行になります。

`scoredatalog` の `clear >= 2` は272/272行で、`ems` / `lms` を除く判定10列の合計が
`notes` と一致しました。`ems` / `lms` は空POORなので `judged` には含めず、合計を
`empty_poor` として別に保存します（384行の合計11,255、最大168、ゼロ2行）。

`score` のIR由来298行では `lpg` / `lgr` が全行0で、297/298行は判定10列の合計が
`notes` を超えます。1プレイの判定内訳ではないため、取り込む542行の `judged` /
`empty_poor` / `survival` / `completed` はすべてNULLにします。

また、`clear` は結果ランプであり、`.brd` の `gauge` は開始時に選択したゲージです。
`clear=2/3` はアシスト状態との区別ができず、Gauge Auto Shift でも両者は一致し得ないため、
`credited_gauge_kind` と `selected_gauge_kind` に分けて保存します。Step 1では後者はNULLです。

さらに `score` には、コース集約と見られる例外があります。`date > 0` の
`clear = 0` が7行あり、連結SHAと `mode = 20 / 10000` の行も4行あります。
このStepでは `score` のローカル行をプレイ履歴へ使わないため保存結果には影響しませんが、
今後 `score.mode` をLN種別として扱う処理では明示的な除外が必要です。
