# -*- coding: utf-8 -*-
"""
台本テスト（SPEC.md §6 Phase 1 の受け入れ条件）

合成データ 4 銘柄を 1 日ずつ進め、events.json に期待どおりのイベント列が出ることを確かめる。
  A: 登録 → 2日目 near → 3日目 breakout → 6日目 target_hit
  B: 登録 → 5日目 dropped
  C: 登録 → 2日目 near（以後も上限付近に居座るが near は 1 回だけ）
  D: 登録 → 2日目 near → 3日目 breakout → 5日目 back_below（ブレイク失敗）
ネットワークも Discord も使わない。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import daily_run as dr
import range_scanner as rs
import state_machine as sm
import store

D0 = "2026-08-28"                       # 登録日（金曜）
DAY0 = pd.Timestamp(D0)
STOCKS = {"AAAA.T": ("Alpha", 100), "BBBB.T": ("Bravo", 101), "CCCC.T": ("Charlie", 102), "DDDD.T": ("Delta", 103)}
TICKERS = [(code, name) for code, (name, _) in STOCKS.items()]

ENTRY_KEYS = {"name", "status", "registered", "range_high", "range_low", "band_pct", "range_age_at_reg",
              "if_target", "metrics_at_reg", "last_close", "last_checked"}
EVENT_KEYS = {"id", "date", "type", "code", "name", "close", "detail", "range", "metrics_at_reg", "if_target"}
METRIC_KEYS = {"score", "pos", "vol_ratio", "bbw_pctile", "atr_ratio"}


# ----------------------------------------------------------------------------- 道具
def base_frames() -> dict:
    """登録日までの日足（--demo と同じ RANGE_GOOD 型。seed 違いで 4 銘柄）。"""
    return {code: rs.make_synthetic("RANGE_GOOD", seed=seed, end=DAY0) for code, (_, seed) in STOCKS.items()}


def extend(df: pd.DataFrame, closes: list) -> pd.DataFrame:
    """台本の終値を翌営業日から順に足す（OHLC は終値の周りに小さく、出来高は直近平均）。"""
    dates = pd.bdate_range(start=df.index[-1] + pd.Timedelta(days=1), periods=len(closes))
    prev = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].tail(20).mean())
    rows = []
    for c in closes:
        c = float(c)
        rows.append({"Open": prev, "High": max(prev, c) * 1.002, "Low": min(prev, c) * 0.998,
                     "Close": c, "Volume": vol})
        prev = c
    return pd.concat([df, pd.DataFrame(rows, index=dates)])


def register_all(data_dir) -> dict:
    base = base_frames()
    added = dr.run_daily(D0, base, TICKERS, data_dir, do_scan=True, chart_dir=None)
    assert [(e["code"], e["type"]) for e in added] == [(c, "watch_start") for c in STOCKS]
    return base


def key(e: dict) -> tuple:
    return (e["date"], e["code"], e["type"])


# ----------------------------------------------------------------------------- 台本
def test_scenario(tmp_path):
    data = tmp_path / "data"
    base = register_all(data)
    state = store.load_state(data / "state.json")
    assert set(state["watching"]) == set(STOCKS) and state["archived"] == {}
    box = {c: state["watching"][c] for c in STOCKS}
    h126 = {c: float(base[c]["High"].tail(126).max()) for c in STOCKS}

    mid = lambda c: (box[c]["range_high"] + box[c]["range_low"]) / 2       # noqa: E731 箱の中段（静か）
    near = lambda c: box[c]["range_high"] * 0.99                            # noqa: E731 上限の1.5%以内、上限は超えない
    brk = lambda c: max(box[c]["range_high"], h126[c]) * 1.02               # noqa: E731 上限も126日高値も超える
    tgt = lambda c: box[c]["if_target"] * 1.01                              # noqa: E731 IF目標到達
    below = lambda c: box[c]["range_low"] * 0.97                            # noqa: E731 下限割れ
    back = lambda c: box[c]["range_high"] * 0.98                            # noqa: E731 箱に戻る
    script = {                       # 1日目 … 6日目
        "AAAA.T": [mid, near, brk, brk, brk, tgt],
        "BBBB.T": [mid, mid, mid, mid, below, below],
        "CCCC.T": [mid, near, near, near, near, near],
        "DDDD.T": [mid, near, brk, brk, back, back],
    }
    full = {c: extend(base[c], [f(c) for f in script[c]]) for c in STOCKS}
    days = [d.strftime("%Y-%m-%d") for d in full["AAAA.T"].index[-6:]]

    log = []
    for k, day in enumerate(days, start=1):
        frames = {c: full[c].iloc[: len(base[c]) + k] for c in STOCKS}
        added = dr.run_daily(day, frames, TICKERS, data, do_scan=False, chart_dir=data / "charts")
        log += [key(e) for e in added]

    expected = [
        (days[1], "AAAA.T", "near"), (days[1], "CCCC.T", "near"), (days[1], "DDDD.T", "near"),
        (days[2], "AAAA.T", "breakout"), (days[2], "DDDD.T", "breakout"),
        (days[4], "BBBB.T", "dropped"), (days[4], "DDDD.T", "back_below"),
        (days[5], "AAAA.T", "target_hit"),
    ]
    assert log == expected

    # events.json: 登録 4 件 + 台本 8 件が、この順で全部残っている
    events = store.load_events(data / "events.json")
    assert [key(e) for e in events] == [(D0, c, "watch_start") for c in STOCKS] + expected

    # state.json: 最終状態
    state = store.load_state(data / "state.json")
    assert set(state["watching"]) == {"CCCC.T"} and state["watching"]["CCCC.T"]["status"] == "near"
    assert state["archived"]["AAAA.T"]["status"] == "breakout"
    assert state["archived"]["BBBB.T"]["status"] == "dropped"
    assert state["archived"]["DDDD.T"]["status"] == "breakout"
    assert state["updated"] == days[-1]
    # 箱の凍結: 登録時の値が最後まで変わらない
    for c in STOCKS:
        e = state["watching"].get(c) or state["archived"][c]
        assert (e["range_high"], e["range_low"], e["if_target"]) == (box[c]["range_high"], box[c]["range_low"], box[c]["if_target"])

    # breakout の detail（SPEC の例と同じ形）と、指標スナップショット
    brk_ev = next(e for e in events if key(e) == (days[2], "AAAA.T", "breakout"))
    assert {"gain_pct", "vol_x", "months_high"} <= set(brk_ev["detail"])
    assert brk_ev["detail"]["gain_pct"] == pytest.approx((brk_ev["close"] / box["AAAA.T"]["range_high"] - 1) * 100, abs=0.1)
    assert brk_ev["range"] == {"high": box["AAAA.T"]["range_high"], "low": box["AAAA.T"]["range_low"],
                               "age": box["AAAA.T"]["range_age_at_reg"]}
    assert brk_ev["metrics_at_reg"] == box["AAAA.T"]["metrics_at_reg"]
    # チャートはイベント id 名で残る
    assert (data / "charts" / f"{brk_ev['id']}.png").exists()

    # データ契約（SPEC.md §4）の形
    for e in events:
        assert set(e) == EVENT_KEYS and set(e["metrics_at_reg"]) == METRIC_KEYS
        assert e["id"] == f"{e['date']}-{e['code']}-{e['type']}"
    for e in list(state["watching"].values()) + list(state["archived"].values()):
        assert set(e) == ENTRY_KEYS and set(e["metrics_at_reg"]) == METRIC_KEYS
    json.loads((data / "events.json").read_text(encoding="utf-8"))   # 壊れていない JSON

    # 冪等性: 同じ足でもう一度走らせても何も増えない（休場日・二重実行）
    frames = {c: full[c] for c in STOCKS}
    assert dr.run_daily(days[-1], frames, TICKERS, data, do_scan=False) == []
    assert dr.run_daily("2026-09-08", frames, TICKERS, data, do_scan=True) == []   # 週次スキャンでも再登録しない
    assert len(store.load_events(data / "events.json")) == len(events)


def test_weekly_scan_adds_new(tmp_path):
    """初回は watch_start、2 回目以降の週次スキャンで見つかった銘柄は new。登録済みは触らない。"""
    data = tmp_path / "data"
    base = base_frames()
    first = [t for t in TICKERS if t[0] in ("AAAA.T", "BBBB.T")]
    added = dr.run_daily(D0, base, first, data, do_scan=True)
    assert [e["type"] for e in added] == ["watch_start", "watch_start"]

    nxt = {c: extend(base[c], [(base[c]["High"].tail(60).max() + base[c]["Low"].tail(60).min()) / 2]) for c in STOCKS}
    added = dr.run_daily("2026-08-31", nxt, TICKERS, data, do_scan=True)
    assert [(e["code"], e["type"]) for e in added] == [("CCCC.T", "new"), ("DDDD.T", "new")]
    state = store.load_state(data / "state.json")
    assert state["watching"]["AAAA.T"]["registered"] == D0 and state["watching"]["CCCC.T"]["registered"] == "2026-08-31"


# ----------------------------------------------------------------------------- 状態遷移表（SPEC.md §5）
@pytest.mark.parametrize("status,close,expect", [
    ("watching", 2700, ("watching", None, False)),      # 箱の中（2841 だと上限の1.5%以内なので near になる）
    ("watching", 2808, ("near", "near", False)),        # ≥ 2850×0.985 = 2807.25
    ("watching", 2806, ("watching", None, False)),      # 接近未満
    ("near",     2808, ("near", None, False)),          # near は 1 回だけ
    ("watching", 2860, ("near", "near", False)),        # 上限は超えたが 126 日高値未満 → breakout ではない
    ("near",     2969, ("breakout", "breakout", True)), # 上限も 126 日高値も超えた
    ("watching", 2969, ("breakout", "breakout", True)),
    ("near",     2514, ("dropped", "dropped", True)),   # 下限割れ
    ("breakout", 3185, ("breakout", "target_hit", False)),
    ("breakout", 2849, ("breakout", "back_below", False)),
    ("breakout", 3000, ("breakout", None, False)),
    ("dropped",  2000, ("dropped", None, False)),
])
def test_decide(status, close, expect):
    d = sm.decide(status, close, range_high=2850, range_low=2515, if_target=3185, high_n=2900)
    assert (d.status, d.event, d.archive) == expect


def test_decide_without_history_never_breaks_out():
    d = sm.decide("watching", 9999, 2850, 2515, 3185, high_n=float("nan"))
    assert d.event == "near" and not d.archive


# ----------------------------------------------------------------------------- 追記専用・再登録ルール
def test_events_append_only(tmp_path):
    path = tmp_path / "events.json"
    e1 = {"id": "2026-09-01-X.T-near", "date": "2026-09-01", "type": "near", "code": "X.T"}
    e2 = {"id": "2026-09-02-X.T-breakout", "date": "2026-09-02", "type": "breakout", "code": "X.T"}
    assert store.append_events(path, [e1]) == [e1]
    before = path.read_text(encoding="utf-8")
    assert store.append_events(path, [e1, e2]) == [e2]          # 重複 id は捨てる
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n]")[: len(before) - 3])   # 既存部分は書き換わらない
    assert store.load_events(path) == [e1, e2]
    assert store.append_events(path, []) == [] and store.load_events(path) == [e1, e2]


def test_can_reregister_rules():
    entry = {"status": "dropped", "registered": "2026-06-01"}
    state = {"watching": {}, "archived": {"X.T": entry}}
    events = [{"id": "2026-07-01-X.T-dropped", "date": "2026-07-01", "type": "dropped", "code": "X.T"}]
    assert not sm.can_reregister("X.T", state, events, "2026-07-15")      # 冷却期間中
    assert sm.can_reregister("X.T", state, events, "2026-09-01")          # 60 日経過
    assert not sm.can_reregister("Y.T", {"watching": {"Y.T": {}}, "archived": {}}, [], "2026-09-01")  # 監視中
    pending = {"watching": {}, "archived": {"Z.T": {"status": "breakout", "registered": "2026-06-01"}}}
    ev = [{"id": "2026-07-01-Z.T-breakout", "date": "2026-07-01", "type": "breakout", "code": "Z.T"}]
    assert not sm.can_reregister("Z.T", pending, ev, "2026-12-01")        # 結末待ちの breakout
    ev.append({"id": "2026-07-10-Z.T-back_below", "date": "2026-07-10", "type": "back_below", "code": "Z.T"})
    assert sm.can_reregister("Z.T", pending, ev, "2026-12-01")
