#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_run.py — 毎日引け後に1回走る監視ジョブ（Phase 1）

流れ:
  1. 週次スキャン（月曜 / 記憶が空の初回 / --scan）: range_scanner.evaluate() の合格銘柄を state.watching に登録
       初回は watch_start（一括）、以後は new（週次追加）。登録済み銘柄の箱は再計測しない（凍結ルール）
  2. 毎日: watching の各銘柄を終値で判定（state_machine.decide）→ near / breakout / dropped
       breakout と dropped は archived へ。breakout は結末（target_hit / back_below）が出るまで見続ける
  3. data/state.json を保存（上書き）、data/events.json に追記（既存イベントは書き換えない）
  4. イベントがあった日だけ Discord に通知（該当銘柄の IF チャートを添付）。無い日は静か

冪等性: 銘柄ごとに last_checked より新しい足が無ければ何もしない
        （休場日や二重実行で同じイベントを二度出さない。登録当日も判定しない）

使い方:
  python engine/daily_run.py                                    # 今日（JST）。曜日に応じて週次スキャン
  python engine/daily_run.py --scan --no-notify --data out/data   # 動作確認（data/ を汚さない）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date as _date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))  # engine/ 内の兄弟モジュールを import できるように
import range_scanner as rs  # noqa: E402
import state_machine as sm  # noqa: E402
import store  # noqa: E402

ROOT = rs.ROOT
EMOJI = {"watch_start": "👀", "new": "👀", "near": "🔔", "breakout": "🚀",
         "dropped": "💤", "target_hit": "🎯", "back_below": "↩️"}
LABEL = {"watch_start": "監視開始", "new": "週次追加", "near": "上限に接近", "breakout": "ブレイクアウト",
         "dropped": "監視解除", "target_hit": "IF目標到達", "back_below": "ブレイク失敗"}
WEEKDAYS_JP = "月火水木金土日"


# ----------------------------------------------------------------- 日足からの小さな読み取り
def bar_date_of(df) -> str:
    return pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")


def high_before_today(df, n: int) -> float:
    """当日を除く直近 n 本の高値の最大。足りなければあるだけで計算、無ければ NaN（→ breakout 不成立）。"""
    prev = df["High"].iloc[:-1].tail(n)
    return float(prev.max()) if len(prev) else float("nan")


def vol_x_today(df, n: int = 20):
    """当日出来高 ÷ 前 n 日平均。記録用（判定には使わない）。"""
    prev = df["Volume"].iloc[:-1].tail(n)
    if len(prev) == 0 or float(prev.mean()) <= 0:
        return None
    return round(float(df["Volume"].iloc[-1]) / float(prev.mean()), 2)


def months_since_high(df, close: float) -> int:
    """終値 close 以上の高値を最後に付けた日から何ヶ月か（データ内に無ければデータ全長）。"""
    prev = df.iloc[:-1]
    hit = prev[prev["High"] >= close]
    last = hit.index[-1] if len(hit) else df.index[0]
    days = (pd.Timestamp(df.index[-1]) - pd.Timestamp(last)).days
    return max(1, int(round(days / 30.44)))


def bars_since(df, iso_date: str) -> int:
    return int((df.index > pd.Timestamp(iso_date)).sum())


def jp_date(iso: str) -> str:
    return f"{iso}（{WEEKDAYS_JP[_date.fromisoformat(iso).weekday()]}）"


# ----------------------------------------------------------------- チャート・通知
def render_event_chart(ev: dict, entry: dict, df, chart_dir: Path):
    """イベント用 IF チャート。箱は state の凍結値で描く。描画失敗で本体を止めない。"""
    try:
        age = int(entry["range_age_at_reg"]) + bars_since(df, entry["registered"])
        r = rs.ScanResult(
            code=ev["code"], name=entry["name"], ok=True, reasons=[], last_date=ev["date"],
            close=float(ev["close"]), range_high=float(entry["range_high"]), range_low=float(entry["range_low"]),
            band_pct=float(entry["band_pct"]), slope_pct=float("nan"), turnover=float("nan"),
            range_age=age, if_target=float(entry["if_target"]), metrics=entry["metrics_at_reg"],
        )
        return rs.render_if_chart(df, r, Path(chart_dir), filename=f"{ev['id']}.png", tag=ev["type"].upper())
    except Exception as e:  # noqa: BLE001 — チャートは付加物。失敗しても記録と通知は続ける
        print(f"[chart] {ev['id']}: failed ({type(e).__name__}: {e})")
        return None


def build_message(run_date: str, events: list, state: dict) -> str:
    fp = rs.fmt_price
    lines = [f"📔 {jp_date(run_date)} のイベント {len(events)}件"]
    for e in events:
        d, r, t = e["detail"], e["range"], e["type"]
        entry = state["watching"].get(e["code"]) or state["archived"].get(e["code"]) or {}
        waited = f"（登録から{sm.days_between(entry['registered'], e['date'])}日目）" if entry.get("registered") else ""
        vol = d.get("vol_x")
        vol_txt = f"出来高 {vol}x" if vol is not None else "出来高 -"
        if t in ("watch_start", "new"):
            body = (f"箱 {fp(r['low'])}–{fp(r['high'])}（幅{d['band_pct']}%・{r['age']}日）"
                    f"→ IF {fp(e['if_target'])}  score {e['metrics_at_reg'].get('score')}")
        elif t == "near":
            body = f"終値 {fp(e['close'])}（上限 {fp(r['high'])} まで {d['gap_pct']:+.1f}%）{waited}"
        elif t == "breakout":
            body = (f"終値 {fp(e['close'])}（上限比 {d['gain_pct']:+.1f}%）{d['months_high']}ヶ月ぶり高値・{vol_txt}"
                    f" → IF {fp(e['if_target'])} {waited}")
        elif t == "dropped":
            body = f"終値 {fp(e['close'])}（下限 {fp(r['low'])} 比 {d['loss_pct']:+.1f}%）→ archived {waited}"
        elif t == "target_hit":
            body = f"終値 {fp(e['close'])} ≥ IF {fp(e['if_target'])}（上限比 {d['gain_pct']:+.1f}%）{waited}"
        else:  # back_below
            body = f"終値 {fp(e['close'])} < 上限 {fp(r['high'])}（箱に戻った。検証データとして記録）{waited}"
        lines.append(f"{EMOJI[t]} {LABEL[t]} {e['code']} {e['name']}  {body}")
    return "\n".join(lines)[:1900]


# ----------------------------------------------------------------- 本体（純粋にデータだけを受け取る。テストはここを叩く）
def run_daily(run_date: str, frames: dict, tickers: list, data_dir, do_scan: bool,
              chart_dir=None, notify: bool = False, cfg: dict = sm.CONFIG) -> list:
    """1 日分を処理して、追記されたイベントのリストを返す。

    frames  : {code: 日足 DataFrame}（最後の行が「今日」の足）
    tickers : [(code, name), ...] スキャン対象
    """
    data_dir = Path(data_dir)
    state_path, events_path = data_dir / "state.json", data_dir / "events.json"
    state = store.load_state(state_path)
    known = store.load_events(events_path)
    first_run = not state["watching"] and not state["archived"]   # 記憶が空 → 初回一括 watch_start
    new_events, chart_jobs = [], []

    # 1. 週次スキャン → 新規登録のみ（既存の箱には触らない）
    if do_scan:
        for code, name in tickers:
            df = frames.get(code)
            if df is None or len(df) == 0:
                continue
            bar = bar_date_of(df)
            if not sm.can_reregister(code, state, known + new_events, bar, cfg):
                continue
            r = rs.evaluate(code, name, df)
            if not r.ok:
                continue
            entry = sm.register_entry(name, bar, r.close, r.range_high, r.range_low,
                                      r.band_pct, r.range_age, r.if_target, r.metrics)
            state["watching"][code] = entry
            ev_type = "watch_start" if first_run else "new"
            ev = sm.make_event(bar, ev_type, code, entry, r.close,
                               sm.build_detail(ev_type, r.close, entry, vol_x_today(df)))
            new_events.append(ev)
            chart_jobs.append((ev, entry, df))

    # 2. 監視中の判定（終値ベース）
    for code, entry in list(state["watching"].items()):
        df = frames.get(code)
        if df is None or len(df) == 0:
            continue
        bar = bar_date_of(df)
        if bar <= entry["last_checked"]:      # 新しい足が無い（休場日・二重実行・登録当日）
            continue
        close = float(df["Close"].iloc[-1])
        d = sm.decide(entry["status"], close, entry["range_high"], entry["range_low"], entry["if_target"],
                      high_before_today(df, cfg["months_high_days"]), cfg)
        entry = {**entry, "status": d.status, "last_close": sm.num(close), "last_checked": bar}
        if d.event:
            months = months_since_high(df, close) if d.event == "breakout" else None
            ev = sm.make_event(bar, d.event, code, entry, close,
                               sm.build_detail(d.event, close, entry, vol_x_today(df), months))
            new_events.append(ev)
            chart_jobs.append((ev, entry, df))
        if d.archive:
            del state["watching"][code]
            state["archived"][code] = entry
        else:
            state["watching"][code] = entry

    # 3. ブレイク後の結末（記録のみ。target_hit / back_below のどちらかが出たら終わり）
    for code, entry in list(state["archived"].items()):
        if entry.get("status") != sm.BREAKOUT or sm.has_outcome(known + new_events, code, entry["registered"]):
            continue
        df = frames.get(code)
        if df is None or len(df) == 0:
            continue
        bar = bar_date_of(df)
        if bar <= entry["last_checked"]:
            continue
        close = float(df["Close"].iloc[-1])
        d = sm.decide(sm.BREAKOUT, close, entry["range_high"], entry["range_low"], entry["if_target"],
                      float("nan"), cfg)
        entry = {**entry, "last_close": sm.num(close), "last_checked": bar}
        if d.event:
            ev = sm.make_event(bar, d.event, code, entry, close,
                               sm.build_detail(d.event, close, entry, vol_x_today(df)))
            new_events.append(ev)
            chart_jobs.append((ev, entry, df))
        state["archived"][code] = entry

    # 4. 保存（state は上書き、events は追記）
    state["updated"] = run_date
    store.save_state(state_path, state)
    added = store.append_events(events_path, new_events)
    added_ids = {e["id"] for e in added}

    # 5. チャート → 通知（イベントがあった日だけ）
    charts = []
    if chart_dir is not None:
        for ev, entry, df in chart_jobs:
            if ev["id"] in added_ids:
                p = render_event_chart(ev, entry, df, chart_dir)
                if p is not None:
                    charts.append(p)
    if notify and added:
        rs.notify_discord(build_message(run_date, added, state), charts[:10])
    return added


# ----------------------------------------------------------------- CLI
def main(argv=None) -> int:
    rs.setup_stdout()
    ap = argparse.ArgumentParser(description="株BOTダイアリー 毎日の監視ジョブ")
    ap.add_argument("--date", default=None, help="実行日 YYYY-MM-DD（既定: 今日 JST）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--scan", action="store_true", help="週次スキャンを強制する")
    g.add_argument("--no-scan", action="store_true", help="週次スキャンをしない")
    ap.add_argument("--tickers", type=Path, default=ROOT / "tickers.txt", help="監視銘柄ファイル")
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="state.json / events.json / charts の置き場")
    ap.add_argument("--limit", type=int, default=None, help="スキャン対象を先頭 N 銘柄に絞る（動作確認用）")
    ap.add_argument("--no-chart", action="store_true", help="チャートを描かない")
    ap.add_argument("--no-notify", action="store_true", help="Discord へ通知しない")
    args = ap.parse_args(argv)

    run_date = args.date or rs.today_jst()
    state = store.load_state(args.data / "state.json")
    fresh = not state["watching"] and not state["archived"]
    weekday = _date.fromisoformat(run_date).weekday()
    do_scan = args.scan or (not args.no_scan and (fresh or weekday == sm.CONFIG["scan_weekday"]))

    tickers = rs.load_tickers(args.tickers)
    if args.limit:
        tickers = tickers[: args.limit]
    codes = [c for c, _ in tickers] if do_scan else []
    codes += list(state["watching"])
    codes += [c for c, e in state["archived"].items() if e.get("status") == sm.BREAKOUT]
    codes = list(dict.fromkeys(codes))   # 順序を保って重複除去
    print(f"== daily run {run_date}  scan={'yes' if do_scan else 'no'}  fetch={len(codes)}  data={args.data}")

    frames = {}
    for code in codes:
        frames[code] = rs.fetch_yfinance(code)
        time.sleep(0.4)   # yfinance への礼儀

    added = run_daily(run_date, frames, tickers, args.data, do_scan,
                      chart_dir=None if args.no_chart else args.data / "charts",
                      notify=not args.no_notify)

    state = store.load_state(args.data / "state.json")
    print(f"watching={len(state['watching'])}  archived={len(state['archived'])}  new_events={len(added)}")
    for e in added:
        print(f"  {EMOJI[e['type']]} {e['id']}  close {rs.fmt_price(e['close'])}  {e['detail']}")
    if not added:
        print("quiet day: no events -> no notification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
