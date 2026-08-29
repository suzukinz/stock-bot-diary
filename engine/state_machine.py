#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_machine.py — 監視銘柄の状態機械（SPEC.md §5）。副作用なし・pandas 非依存。

  watching ──(終値 ≥ range_high×(1−near_pct))──▶ near                 … イベント near（遷移時に1回だけ）
  watching/near ──(終値 > range_high かつ 終値 > 過去N日高値)──▶ breakout … archived へ（結末を記録するため判定は続ける）
  watching/near ──(終値 < range_low)──▶ dropped                        … archived へ
  breakout ──(終値 ≥ if_target)──▶ target_hit（記録のみ・結末確定）
  breakout ──(終値 < range_high)──▶ back_below（記録のみ・結末確定＝ブレイク失敗。検証の宝）

原則:
  - 判定は終値ベース・引け後1回
  - 箱（range_high / range_low）は登録時に凍結。ここでは一切再計測しない
  - 出来高（vol_x）は detail に記録するが判定には使わない
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date

CONFIG = {
    "near_pct": 1.5,          # near: 終値 ≥ range_high × (1 − near_pct/100)
    "months_high_days": 126,  # breakout: 終値 > 過去126営業日の高値（当日除く）≒ 6ヶ月ぶり高値
    "rearm_days": 60,         # archived 後、同じ銘柄を再登録できるまでの冷却期間（暦日）
    "scan_weekday": 0,        # 週次スキャンを回す曜日（0=月曜）。記憶が空の初回は曜日に関係なく回す
}

WATCHING, NEAR, BREAKOUT, DROPPED = "watching", "near", "breakout", "dropped"
EVENT_TYPES = ("watch_start", "new", "near", "breakout", "dropped", "target_hit", "back_below")
OUTCOME_EVENTS = ("target_hit", "back_below")   # breakout の結末。どちらか1回で確定


@dataclass(frozen=True)
class Decision:
    status: str            # 遷移後の status
    event: str | None      # 発火するイベント種別（無ければ None）
    archive: bool          # True なら watching → archived へ移す


def decide(status: str, close: float, range_high: float, range_low: float,
           if_target: float, high_n: float, cfg: dict = CONFIG) -> Decision:
    """今日の終値 close と、当日を除く過去N日高値 high_n（不明なら NaN）から遷移を決める。

    high_n が NaN のとき `close > high_n` は False になるので breakout は成立しない（保守的）。
    """
    if status in (WATCHING, NEAR):
        if close < range_low:
            return Decision(DROPPED, "dropped", True)
        if close > range_high and close > high_n:
            return Decision(BREAKOUT, "breakout", True)
        if status == WATCHING and close >= range_high * (1 - cfg["near_pct"] / 100):
            return Decision(NEAR, "near", False)
        return Decision(status, None, False)
    if status == BREAKOUT:
        if close >= if_target:
            return Decision(BREAKOUT, "target_hit", False)
        if close < range_high:
            return Decision(BREAKOUT, "back_below", False)
        return Decision(BREAKOUT, None, False)
    return Decision(status, None, False)   # dropped: 何もしない


# ----------------------------------------------------------------------- 記録の形（SPEC.md §4）
def num(x) -> int | float:
    """JSON 用の数値: 整数なら int、そうでなければ小数1桁（東証の呼値はほぼ整数）。"""
    f = float(x)
    return int(f) if f.is_integer() else round(f, 1)


def days_between(a: str, b: str) -> int:
    """暦日差 b − a（YYYY-MM-DD）。"""
    return (_date.fromisoformat(b) - _date.fromisoformat(a)).days


def register_entry(name: str, bar_date: str, close: float, range_high: float, range_low: float,
                   band_pct: float, range_age: int, if_target: float, metrics: dict) -> dict:
    """state.json の watching[code] に入れる 1 件。ここで箱が凍結される。"""
    return {
        "name": name,
        "status": WATCHING,
        "registered": bar_date,
        "range_high": num(range_high), "range_low": num(range_low),
        "band_pct": round(float(band_pct), 1), "range_age_at_reg": int(range_age),
        "if_target": num(if_target),
        "metrics_at_reg": dict(metrics),
        "last_close": num(close), "last_checked": bar_date,
    }


def build_detail(event: str, close: float, entry: dict, vol_x, months_high=None) -> dict:
    """イベント種別ごとの detail。vol_x は必ず記録する（判定には使わない）。"""
    hi, lo = float(entry["range_high"]), float(entry["range_low"])

    def pct(a: float, b: float) -> float:
        return round((a / b - 1) * 100, 1)

    if event in ("watch_start", "new"):
        d = {"band_pct": entry["band_pct"], "pos": entry["metrics_at_reg"].get("pos")}
    elif event == "near":
        d = {"gap_pct": pct(close, hi)}          # 上限まであと何%（負の値）
    elif event == "dropped":
        d = {"loss_pct": pct(close, lo)}         # 下限をどれだけ割ったか
    else:                                        # breakout / target_hit / back_below
        d = {"gain_pct": pct(close, hi)}         # 箱上限比
    d["vol_x"] = vol_x
    if event == "breakout":
        d["months_high"] = months_high
    return d


def make_event(date: str, event: str, code: str, entry: dict, close: float, detail: dict) -> dict:
    """events.json の 1 件。登録時の指標スナップショットを必ず抱える。"""
    return {
        "id": f"{date}-{code}-{event}",
        "date": date, "type": event,
        "code": code, "name": entry["name"], "close": num(close),
        "detail": detail,
        "range": {"high": entry["range_high"], "low": entry["range_low"], "age": entry["range_age_at_reg"]},
        "metrics_at_reg": entry["metrics_at_reg"],
        "if_target": entry["if_target"],
    }


# ----------------------------------------------------------------------- events.json から導く判断
def has_outcome(events: list, code: str, since_date: str) -> bool:
    """since_date（登録日）以降に target_hit / back_below が記録済みか。"""
    return any(e.get("code") == code and e.get("type") in OUTCOME_EVENTS and e.get("date", "") >= since_date
               for e in events)


def last_archive_date(events: list, code: str):
    dates = [e["date"] for e in events if e.get("code") == code and e.get("type") in ("breakout", "dropped")]
    return max(dates) if dates else None


def can_reregister(code: str, state: dict, events: list, bar_date: str, cfg: dict = CONFIG) -> bool:
    """週次スキャンでこの銘柄を（再）登録してよいか。

    - watching にいる → 不可（箱の凍結ルール。再計測しない）
    - archived の breakout で結末待ち → 不可（同じ銘柄を二重に追わない）
    - archived から rearm_days（暦日）経っていない → 不可（落ちた直後の同じ箱を拾い直さない）
    """
    if code in state["watching"]:
        return False
    arch = state["archived"].get(code)
    if arch is None:
        return True
    if arch.get("status") == BREAKOUT and not has_outcome(events, code, arch["registered"]):
        return False
    last = last_archive_date(events, code)
    if last is None:
        return True
    return days_between(last, bar_date) >= cfg["rearm_days"]
