# SPEC.md — 株BOTダイアリー 設計書

このプロジェクトの経緯と設計判断の全記録。実装前に必ず読むこと。
日々のルールは `CLAUDE.md` にある。

## 1. ビジョン（1段落）

日本株の「レンジ（ボックス）銘柄」を数値定義で毎週発掘し、毎日引け後に監視し、ブレイクアウト等のイベントが起きた日だけ通知する。通知は日記のように蓄積され、「待った日数に通知の価値が比例する」体験を作る。名前の由来は『IFチャート』——レンジに「もし上抜けたら」の目標線を先に描いておく発想。**予測ツールではなく、候補発見＋イベント検知＋記録のツール**。検出したレンジが抜ける保証はない前提で設計する（失敗してもレンジに戻るだけ）。

## 2. すでにあるもの（動作確認済み）

| ファイル | 内容 |
|---|---|
| `engine/range_scanner.py` | レンジ検出エンジン。CONFIG（幅20%以内・傾き±8%・売買代金1億円以上等）＋スコア（BB幅スクイーズ0.35/ATR収縮0.25/出来高枯れ0.20/位置0.20）。`--demo`で合成データ5銘柄の自己テスト（RANGE_GOODのみ検出が正解）。IFチャートPNG生成（レンジの箱・幅・継続日数・IF目標線・出来高パネル）。Discord通知（画像添付対応） |
| `app/stock_bot_diary.html` | ダイアリーUIの試作。左：注目レンジ株（箱つきスパークライン）、右：カレンダー・通知（LINE風に蓄積）・日記。現在はモックデータ＋「翌日へ進む」デモ。Phase 2で `data/*.json` を読むよう配線する |
| `tickers.txt` | 監視銘柄（東証は `XXXX.T`。キオクシアは `285A.T`） |
| `.github/workflows/daily_scan.yml` | 平日18:30 JSTに自動実行（GitHub Actions） |
| `vault/`（サンプル） | Obsidian出力の見本（Daily/銘柄/Watchlist の.md） |

## 3. アーキテクチャ：エンジン第一・多フロント

```
[engine (Python, GitHub Actions)]
   │  毎日18:30 JST: 監視 → イベント判定 → 書き込み
   ▼
[data/]  state.json（記憶） events.json（追記ログ） charts/*.png
   │
   ├─→ [app/] GitHub Pagesで配信、JSONをfetchして表示（Phase 2）
   ├─→ [vault/] Obsidian用Markdown（Phase 2.5）
   └─→ [通知] Discord（現行）→ LINE Messaging API / Web Push（Phase 3）
```

価値の本体はエンジン。フロントは差し替え可能な服。**エンジンと画面はJSONの契約だけで繋ぐ**こと。

## 4. データ契約（最重要・変更は慎重に）

### data/state.json — ボットの記憶
```json
{
  "updated": "2026-09-02",
  "watching": {
    "7011.T": {
      "name": "三菱重工業",
      "status": "watching",
      "registered": "2026-08-28",
      "range_high": 2850, "range_low": 2515,
      "band_pct": 12.5, "range_age_at_reg": 74,
      "if_target": 3185,
      "metrics_at_reg": {"score": 61.2, "pos": 91, "vol_ratio": 0.78,
                          "bbw_pctile": 18, "atr_ratio": 0.86},
      "last_close": 2841, "last_checked": "2026-09-01"
    }
  },
  "archived": {}
}
```
- `status`: `watching` | `near` | `breakout` | `dropped`
- ブレイク済み・解除済みは `archived` に移動（削除しない。履歴＝検証データ）

### data/events.json — 追記専用のイベントログ（日記と検証の源泉）
```json
[
  {
    "id": "2026-09-02-7011.T-breakout",
    "date": "2026-09-02", "type": "breakout",
    "code": "7011.T", "name": "三菱重工業", "close": 2969,
    "detail": {"gain_pct": 4.2, "vol_x": 1.8, "months_high": 8},
    "range": {"high": 2850, "low": 2515, "age": 74},
    "metrics_at_reg": {"score": 61.2, "pos": 91, "vol_ratio": 0.78,
                        "bbw_pctile": 18, "atr_ratio": 0.86},
    "if_target": 3185
  }
]
```
- **検出時の全指標スナップショットを必ず残す**。出来高倍率（vol_x）は記録するが判定には使わない（「出来高フィルターは有効か」を後で自分のデータで検証するため）。今記録しないものは永遠に検証できない。

## 5. イベント状態機械（判定は終値ベース・引け後1回）

| 遷移 | 条件 | イベント |
|---|---|---|
| （新規） | 週次スキャン合格・未登録 | `watch_start`（初回一括）/ `new`（週次追加） |
| watching → near | 終値 ≥ range_high × (1 − 0.015) | `near`（遷移時に1回だけ） |
| watching/near → breakout | 終値 > range_high **かつ** 終値 > 過去126営業日高値（≒6ヶ月ぶり高値、当日除く） | `breakout` |
| watching/near → dropped | 終値 < range_low | `dropped`（archivedへ） |
| breakout → （記録のみ） | 終値 ≥ if_target | `target_hit` |
| breakout → （記録のみ） | 終値 < range_high | `back_below`（ブレイク失敗。検証の宝） |

- `near_pct`(1.5%) と `months_high_days`(126) はCONFIGで可変。
- **箱の凍結ルール**: range_high/low は登録時に固定し、監視中は再計測しない（毎日測り直すとブレイク判定が動く的になるため）。週次スキャンは**新規銘柄の追加のみ**。
- イベントなしの日は何も書かない（"静かな日"は表示側でカレンダーから導出）。

## 6. フェーズ計画（1フェーズずつ。受け入れ条件を満たしてから次へ）

- **Phase 0 — リポジトリ整理**: 上記レイアウトへ移動、CI緑、`--demo`合格。
- **Phase 1 — エンジン完成（最重要）**: 状態機械＋イベント判定＋`data/*.json`書き込み（Actionsからコミット）＋イベント日のみDiscord通知（該当銘柄のIFチャート添付）。
  受け入れ: 合成データの**台本テスト**（例: 登録→2日目near→3日目breakout→別銘柄5日目dropped という価格系列を与え、期待通りのイベント列がevents.jsonに出る）が自動テストとして緑。
- **Phase 2 — 画面配線**: GitHub Pagesで`app/`配信、`data/*.json`をfetchして表示（モック削除）。受け入れ: スマホのブラウザで実銘柄・実イベントが見える。
- **Phase 2.5 — Obsidian出力**: 同一イベントから`vault/Daily/日付.md`・`vault/銘柄/*.md`を生成（`vault/`のサンプルが仕様）。
- **Phase 3 — PWA＋Web Push**: manifest＋Service Worker。Android先行、iOSは「ホーム画面に追加」前提と明記。
- **Phase 4 — 検証**: `outcomes.json`（各breakoutの+5日/+20日リターン、target_hit有無、back_below有無）を自動追記し、成績ページを作る。「検出レンジの何%が抜けたか」「出来高条件の有無で成績は変わるか」に自分のデータで答える。

## 7. 非目標（やらないこと）

- 自動売買・発注機能（永久に対象外）
- ザラ場のリアルタイム監視（終値ベースのみ）
- 複数ユーザー対応・認証・課金
- 有料インフラ前提の構成（サーバー代0円を維持）

## 8. 既知の注意点

- yfinanceは非公式。壊れたら `pip install -U yfinance` → 直らなければJ-Quants API（有料）へ。取得層は `fetch_yfinance()` に隔離済みで差し替え容易に保つ。
- GitHub Actionsのcronは数分〜数十分遅延しうる。60日間コミットが無いとスケジュール停止。
- pandas 3系の `bdate_range` は終端が休日だと期待より1日短い（`periods+5`して末尾スライスで回避済み。同種の暦バグに注意）。
- タイムゾーンは常にJST。日付キーは `YYYY-MM-DD`。

## 9. 運用上の決定（時系列で追記する）

- **2026-08-30**: Phase 0・1 を実装し GitHub（`suzukinz/stock-bot-diary`）へ。CI 緑。初回スキャンで 8058.T 三菱商事を登録（ボットが `data/` を自分でコミット）。
- **2026-08-31 通知は使わない**: Discord 未設定のまま運用する（リアルタイム性は不要という判断）。通知層は `notify_discord()` に隔離してあり、`DISCORD_WEBHOOK_URL` を Secrets に入れれば翌日から復活する。画面（Phase 2）がこのボットの唯一の窓。
- **2026-08-31 リポジトリを public に変更**: 無料プランの GitHub Pages は public 限定のため。リポジトリに秘密情報は無く、日記の手書きメモは端末の localStorage にのみ保存される（公開されない）。Pages は `main` のルートから配信（`.nojekyll`）: https://suzukinz.github.io/stock-bot-diary/
- **Phase 2 の画面**はスパークラインではなく「箱ゲージ」（下限〜上限〜IF目標と終値の位置）＋ボット生成の IF チャート PNG を使う。価格履歴はデータ契約に無く、契約は変えない。
