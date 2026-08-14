# oraja-training

beatoraja のローカルSQLite DBを**読み取り専用**で取り込み、Ryuhei向けの
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
  --assistant-db ./assistant.db \
  --table satellite=https://stellabms.xyz/sl/table.html \
  --table genocide=https://your-current-genocide-table.example/table.html
```

取得結果はETag/Last-Modified付きで`.cache/tables/`へ保存され、通信失敗時は最後の正常な
キャッシュを使います。GENOCIDEの公開URLは移転することがあるため、beatorajaで現在
使用している表URLを指定してください。

schema version 1の既存`assistant.db`は正しい値へ復元できないため、in-place移行しません。
version 2は履歴を保持してversion 3へ移行します。現行schema versionは3です。

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

beatoraja側では、**自動リプレイ保存の1枠を `ALWAYS` に変更してください**。
次の実装段階で、失敗プレイを含む開始時の選択ゲージを `.brd` リプレイから回収するために必要です。

停止は `Ctrl-C` です。入力側の `score.db` / `scoredatalog.db` / `scorelog.db` /
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
