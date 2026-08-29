# CLAUDE.md — 株BOTダイアリー

日本株のレンジ銘柄を発掘・監視し、ブレイクアウト等のイベントを日記のように蓄積するボット＋アプリ。
設計の全体像・データ契約・フェーズ計画は **必ず `SPEC.md` を読むこと**。

## リポジトリ構成

- `engine/` … Python製エンジン（レンジ検出・イベント判定・通知）
- `app/` … ダイアリーUI（単一HTML、GitHub Pagesで配信予定）
- `data/` … ボットが書く出力（state.json / events.json / charts/）。手で編集しない
- `vault/` … Obsidian用Markdown出力（Phase 2.5）
- `.github/workflows/` … 毎日18:30 JSTの自動実行

## 絶対ルール

- 自動売買・発注機能は**絶対に作らない**（提案もしない）
- Webhook URL・APIキー等の秘密情報をコードやコミットに**絶対に含めない**（GitHub Secretsを使う）
- `data/events.json` は追記専用。既存イベントの書き換え・削除をしない
- 箱の凍結ルールを守る：監視中の銘柄の range_high / range_low を再計測しない
- イベントには検出時の指標スナップショットを必ず記録する（出来高は記録するが判定に使わない）
- データ契約（SPEC.md §4）の変更は、先に影響範囲を説明して承認を得てから

## 作業の進め方

- SPEC.md のフェーズを**1つずつ**。ユーザーのOKなしに次のフェーズへ進まない
- 大きめの変更は、実装前に短い計画を提示してから着手する
- 変更後は毎回実行して確認：
  - `python engine/range_scanner.py --demo`（合成データ自己テスト。RANGE_GOODのみ検出が正解）
  - Phase 1以降：イベント台本テスト（SPEC.md §6）
- 既存の動くもの（エンジン・HTML試作・CI）を壊す変更は、先に代替案を提示する
- コミットは小さく、メッセージは日本語で「何をなぜ」

## 環境メモ

- Python 3.12 / 依存は `requirements.txt`（yfinance, pandas, numpy, requests, matplotlib）
- タイムゾーンは常にJST、日付は `YYYY-MM-DD`
- チャート内の文字は英数字のみ（CI環境に日本語フォントが無いため）
- 東証ティッカーは `XXXX.T`（例: キオクシア `285A.T`）
