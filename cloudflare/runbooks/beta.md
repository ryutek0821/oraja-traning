# 招待 β 運用 runbook

この手順は staging/production 相当環境で実行するための準備物です。
承認なしに migration、実データ移行、secret設定、deploy は実行しません。

## Dry run

1. 招待コード、allowlist、無料 profile 上限1、同意文書の版を固定する。
2. 合成 fixture で D1/DO/R2 export を暗号化し、隔離 bucket へ restore する。
3. 5DB manifest、IR offline/retry/duplicate、2表生成、MCP scope、削除7日取消を
   staging の mock binding で検証する。
4. `docs/slo.md` の p95 と cross-profile rejection を記録する。

## Daily record

記録項目は日付、revision、Queue age、欠落数、重複数、越境数、restore hash、
重大 incident、対応者、go/no-go です。30日連続で重大欠落・越境0件、全SLO達成、
復元照合成功、日韓第三者再現成功を確認してから公開 gate に進みます。
