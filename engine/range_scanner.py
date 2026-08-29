#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
range_scanner.py — 株BOTダイアリー レンジ検出エンジン（Phase 0）

使い方:
  python engine/range_scanner.py --demo                # 合成データ5銘柄の自己テスト（RANGE_GOODのみ検出=合格）
  python engine/range_scanner.py                       # tickers.txt を実データでスキャン → チャート → Discord
  python engine/range_scanner.py --limit 3 --no-notify # 動作確認（先頭3銘柄・通知なし）

何をするか:
  1. 日足（Open/High/Low/Close/Volume）から「箱（レンジ）」を数値定義で検出する
  2. 合格銘柄の IF チャート PNG を描く（箱・幅・継続日数・IF目標線・出来高パネル）
  3. 合格銘柄があれば Discord Webhook に通知する（画像添付）。無い日は静かに終わる

判定の流れ:
  硬いフィルタ（幅・傾き・売買代金）
    → スコア（BB幅スクイーズ 0.35 / ATR収縮 0.25 / 出来高枯れ 0.20 / 箱内位置 0.20）
    → min_score 以上で合格
  出来高は「枯れ」としてスコアに使うが、ブレイク等のイベント判定（Phase 1）には使わない。

約束（CLAUDE.md）:
  - 自動売買・発注は作らない
  - 秘密情報は埋め込まない（Webhook URL は環境変数 DISCORD_WEBHOOK_URL からのみ読む）
  - チャート内の文字は英数字のみ（CI に日本語フォントが無い）
  - データ取得は fetch_yfinance() に隔離（壊れたらそこだけ差し替える）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9), "JST")

# --------------------------------------------------------------------------- CONFIG
CONFIG: dict = {
    # --- 箱（レンジ）の定義 ---
    "range_days": 60,           # 箱を測る窓（営業日）。High の最大・Low の最小を箱の上下辺とする
    "max_band_pct": 20.0,       # 箱の幅 (high-low)/mid の上限 [%]
    "max_slope_pct": 8.0,       # 窓内の終値回帰直線の傾き（始点比）の上限 [±%]
    # --- 流動性 ---
    "turnover_days": 20,        # 売買代金を平均する日数
    "min_turnover_jpy": 1e8,    # 平均売買代金の下限 [円]（1億円）
    # --- スコア ---
    "min_score": 40.0,
    "weights": {"bbw": 0.35, "atr": 0.25, "vol": 0.20, "pos": 0.20},
    "bb_period": 20,            # ボリンジャーバンド期間
    "bbw_pctile_days": 250,     # BB幅パーセンタイルの母集団（営業日）
    "atr_period": 14,           # 短期 ATR
    "atr_long_period": 50,      # 長期 ATR（atr_ratio = 短期/長期。<1 で収縮）
    "vol_short_days": 20,       # 出来高 短期平均
    "vol_long_days": 60,        # 出来高 長期平均（vol_ratio = 短期/長期。<1 で枯れ）
    # --- 取得・描画 ---
    "history_period": "2y",     # yfinance の取得期間
    "min_rows": 130,            # これ未満の日足しか無い銘柄は判定しない
    "chart_days": 130,          # チャートに描く日数
    "chart_future_days": 12,    # 箱と IF 目標線を右に延ばす営業日数
}

DEMO_KINDS = ["RANGE_GOOD", "TREND_UP", "WIDE_RANGE", "THIN_VOLUME", "EXPANDING"]
DEMO_EXPECT = {"RANGE_GOOD"}


# --------------------------------------------------------------------------- utilities
def setup_stdout() -> None:
    """Windows コンソール等で日本語が UnicodeEncodeError にならないよう UTF-8 に寄せる。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def make_dates(end, n: int) -> pd.DatetimeIndex:
    """終端 end までの営業日インデックスを n 本返す。

    pandas 3 系の bdate_range は終端が休日だと期待より 1 日短くなるため、
    periods+5 で作って末尾 n 本を切り出す（SPEC.md §8）。
    """
    return pd.bdate_range(end=end, periods=n + 5)[-n:]


def fmt_price(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{int(round(x)):,}" if abs(x - round(x)) < 1e-9 else f"{x:,.1f}"


def _clean(obj):
    """JSON 出力用: NaN → None、numpy 型 → Python 型。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return None if math.isnan(float(obj)) else float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


# --------------------------------------------------------------------------- result
@dataclass
class ScanResult:
    code: str
    name: str
    ok: bool
    reasons: list            # 不合格理由（空なら合格）
    last_date: str
    close: float
    range_high: float
    range_low: float
    band_pct: float
    slope_pct: float
    turnover: float          # 直近平均売買代金 [円]
    range_age: int           # 終値が箱の中に収まり続けている営業日数
    if_target: float         # IF目標 = range_high + 箱の高さ
    metrics: dict            # score / pos / vol_ratio / bbw_pctile / atr_ratio（state.json の metrics_at_reg と同形）


def _empty_result(code: str, name: str, reason: str) -> ScanResult:
    nan = float("nan")
    return ScanResult(code, name, False, [reason], "-", nan, nan, nan, nan, nan, nan, 0, nan,
                      {"score": nan, "pos": nan, "vol_ratio": nan, "bbw_pctile": nan, "atr_ratio": nan})


# --------------------------------------------------------------------------- indicators
def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def _pctile_rank(population: pd.Series, value: float) -> float:
    """value が母集団の下から何%の位置か（0..100）。低いほどスクイーズ。"""
    s = population.dropna()
    if len(s) == 0 or math.isnan(value):
        return float("nan")
    return float((s < value).mean() * 100)


def _range_age(close: pd.Series, low: float, high: float) -> int:
    """末尾から遡って、終値が [low, high] に収まり続けている営業日数。"""
    inside = ((close >= low) & (close <= high)).to_numpy()[::-1]
    return int(len(inside)) if inside.all() else int(np.argmin(inside))


def _linear_score(x: float, best: float, worst: float) -> float:
    """x=best で 100、x=worst で 0 になる線形スコア（範囲外は 0..100 に丸める）。"""
    if math.isnan(x):
        return 50.0
    return float(np.clip((worst - x) / (worst - best) * 100, 0, 100))


def score_of(m: dict) -> float:
    w = CONFIG["weights"]
    bbw_s = 100 - m["bbw_pctile"] if not math.isnan(m["bbw_pctile"]) else 50.0
    atr_s = _linear_score(m["atr_ratio"], best=0.6, worst=1.2)
    vol_s = _linear_score(m["vol_ratio"], best=0.5, worst=1.1)
    pos_s = float(np.clip(m["pos"], 0, 100)) if not math.isnan(m["pos"]) else 50.0
    return w["bbw"] * bbw_s + w["atr"] * atr_s + w["vol"] * vol_s + w["pos"] * pos_s


# --------------------------------------------------------------------------- evaluate
def evaluate(code: str, name: str, df) -> ScanResult:
    """1銘柄の日足を評価して ScanResult を返す。副作用なし。"""
    cfg = CONFIG
    if df is None:
        return _empty_result(code, name, "fetch failed")
    if len(df) < cfg["min_rows"]:
        return _empty_result(code, name, f"rows {len(df)} < {cfg['min_rows']}")

    win = df.tail(cfg["range_days"])
    high = float(win["High"].max())
    low = float(win["Low"].min())
    mid = (high + low) / 2
    band_pct = (high - low) / mid * 100

    y = win["Close"].to_numpy(dtype=float)
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    fitted_start = intercept
    fitted_end = intercept + slope * (len(y) - 1)
    slope_pct = (fitted_end - fitted_start) / fitted_start * 100

    turnover = float((df["Close"] * df["Volume"]).tail(cfg["turnover_days"]).mean())
    close = float(df["Close"].iloc[-1])
    pos = (close - low) / (high - low) * 100 if high > low else 50.0

    ma = df["Close"].rolling(cfg["bb_period"]).mean()
    sd = df["Close"].rolling(cfg["bb_period"]).std()
    bbw = 4 * sd / ma                                   # (upper-lower)/mid
    hist = bbw.tail(cfg["bbw_pctile_days"]).iloc[:-1]   # 当日を除いた母集団で順位付け
    bbw_pctile = _pctile_rank(hist, float(bbw.iloc[-1]))

    tr = _true_range(df)
    atr_short = tr.rolling(cfg["atr_period"]).mean().iloc[-1]
    atr_long = tr.rolling(cfg["atr_long_period"]).mean().iloc[-1]
    atr_ratio = float(atr_short / atr_long) if atr_long else float("nan")

    vol_short = df["Volume"].rolling(cfg["vol_short_days"]).mean().iloc[-1]
    vol_long = df["Volume"].rolling(cfg["vol_long_days"]).mean().iloc[-1]
    vol_ratio = float(vol_short / vol_long) if vol_long else float("nan")

    age = _range_age(df["Close"], low, high)
    if_target = high + (high - low)

    raw = {"pos": pos, "vol_ratio": vol_ratio, "bbw_pctile": bbw_pctile, "atr_ratio": atr_ratio}
    score = score_of(raw)
    metrics = {
        "score": round(score, 1),
        "pos": int(round(pos)),
        "vol_ratio": round(vol_ratio, 2) if not math.isnan(vol_ratio) else vol_ratio,
        "bbw_pctile": int(round(bbw_pctile)) if not math.isnan(bbw_pctile) else bbw_pctile,
        "atr_ratio": round(atr_ratio, 2) if not math.isnan(atr_ratio) else atr_ratio,
    }

    reasons = []
    if band_pct > cfg["max_band_pct"]:
        reasons.append(f"band {band_pct:.1f}% > {cfg['max_band_pct']:.0f}%")
    if abs(slope_pct) > cfg["max_slope_pct"]:
        reasons.append(f"slope {slope_pct:+.1f}% > +/-{cfg['max_slope_pct']:.0f}%")
    if turnover < cfg["min_turnover_jpy"]:
        reasons.append(f"turnover {turnover / 1e8:.2f} oku < {cfg['min_turnover_jpy'] / 1e8:.0f} oku")
    if score < cfg["min_score"]:
        reasons.append(f"score {score:.1f} < {cfg['min_score']:.0f}")

    return ScanResult(
        code=code, name=name, ok=not reasons, reasons=reasons,
        last_date=pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
        close=round(close, 1), range_high=round(high, 1), range_low=round(low, 1),
        band_pct=round(band_pct, 1), slope_pct=round(slope_pct, 1), turnover=round(turnover),
        range_age=age, if_target=round(if_target, 1), metrics=metrics,
    )


# --------------------------------------------------------------------------- synthetic data (--demo)
def make_synthetic(kind: str, n: int = 320, seed: int = 42, end=None) -> pd.DataFrame:
    """自己テスト用の合成日足。kind ごとに「箱として合格/不合格」の性質を作り込む。

    RANGE_GOOD  : 幅約10%の箱、ボラ収縮、出来高枯れ、終値は箱の上寄り → 合格
    TREND_UP    : 一本調子の上昇 → 傾き・幅で不合格
    WIDE_RANGE  : 横ばいだが幅30%超 → 幅で不合格
    THIN_VOLUME : RANGE_GOOD と同じ形だが出来高が薄い → 売買代金で不合格
    EXPANDING   : 硬いフィルタは通るが、ボラ拡大・出来高増加・箱の中段 → スコアで不合格
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = 1000.0
    volume = np.full(n, 500_000.0) * (1 + 0.3 * rng.random(n))
    vol_mult = np.ones(n)

    if kind in ("RANGE_GOOD", "THIN_VOLUME"):
        sigma = np.linspace(14, 4, n)                                # ノイズ収縮
        amp = np.linspace(45, 22, n)                                 # 振れ幅も収縮 → BB幅が過去より狭い（スクイーズ）
        close = base + amp * np.sin(2 * np.pi * (t - n + 8) / 30)    # 最終日は山の頂上付近（箱の上寄り）
        vol_mult[-60:] = np.linspace(1.0, 0.5, 60)                   # 直近60日で出来高が枯れる
        if kind == "THIN_VOLUME":
            volume = volume * 0.06                                   # 売買代金 ≈ 0.3億円
    elif kind == "TREND_UP":
        sigma = np.full(n, 8.0)
        close = 800 * np.exp(0.005 * t)                              # +0.5%/日の一本調子
    elif kind == "WIDE_RANGE":
        sigma = np.full(n, 10.0)
        close = base + 150 * np.sin(2 * np.pi * (t - n + 1) / 30)    # ±15% の大きな箱
    elif kind == "EXPANDING":
        sigma = np.linspace(3, 16, n)                                # ボラ拡大
        close = base + 20 * np.sin(2 * np.pi * (t - n + 15) / 30)    # 最終日は箱の中段
        vol_mult[-60:] = np.linspace(1.0, 1.8, 60)                   # 出来高が増えている
    else:
        raise ValueError(f"unknown kind: {kind}")

    close = close + rng.normal(0, 1, n) * sigma
    open_ = np.concatenate([[close[0]], close[:-1]]) + rng.normal(0, 1, n) * sigma * 0.3
    wick = np.abs(rng.normal(0, 1, n)) * sigma * 0.5
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = volume * vol_mult

    end = end or pd.Timestamp(today_jst())
    idx = make_dates(end=end, n=n)
    return pd.DataFrame(
        {"Open": open_.round(1), "High": high.round(1), "Low": low.round(1),
         "Close": close.round(1), "Volume": volume.round()},
        index=idx,
    )


# --------------------------------------------------------------------------- data source
def fetch_yfinance(code: str, period=None, retries: int = 2):
    """yfinance で日足を取得する。非公式 API なので壊れたらここだけ差し替える（SPEC.md §8）。

    戻り値: Open/High/Low/Close/Volume の DataFrame（index は tz なしの日付）。失敗時 None。
    """
    import yfinance as yf  # 遅延 import: --demo はネットワーク無しで動く

    period = period or CONFIG["history_period"]
    last_err = None
    for attempt in range(retries + 1):
        try:
            df = yf.Ticker(code).history(period=period, auto_adjust=False)
            if df is None or df.empty:
                raise ValueError("empty response")
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            df = df[df["Volume"] > 0]
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
            df.index = pd.DatetimeIndex(df.index).normalize()
            return df.astype(float)
        except Exception as e:  # noqa: BLE001 — 取得層の例外はすべて「取得失敗」として扱う
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"[fetch] {code}: failed ({type(last_err).__name__}: {last_err})")
    return None


def load_tickers(path: Path) -> list:
    """tickers.txt → [(code, name), ...]。'#' 以降はコメント。名前が無ければコードを名前にする。"""
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split(maxsplit=1)
        code = parts[0].upper()
        name = parts[1].strip() if len(parts) > 1 else code
        items.append((code, name))
    return items


# --------------------------------------------------------------------------- chart
def render_if_chart(df: pd.DataFrame, r: ScanResult, out_dir: Path,
                    filename: str = None, tag: str = "IF chart") -> Path:
    """IF チャート PNG を描く。文字は英数字のみ（CI に日本語フォントが無いため銘柄名は描かない）。

    filename: 出力ファイル名（既定 "{code}_{date}.png"）。tag: タイトル末尾のラベル（イベント種別など）。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    cfg = CONFIG
    d = df.tail(cfg["chart_days"])
    last = pd.Timestamp(d.index[-1])
    future = pd.bdate_range(start=last, periods=cfg["chart_future_days"] + 1)[1:]
    start_idx = max(0, len(d) - r.range_age)
    x0 = mdates.date2num(pd.Timestamp(d.index[start_idx]).to_pydatetime())
    x_last = mdates.date2num(last.to_pydatetime())
    x1 = mdates.date2num(pd.Timestamp(future[-1]).to_pydatetime())

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(10, 6.4), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax.plot(d.index, d["Close"], color="#1f4e79", lw=1.4, label="Close")

    # 箱（レンジ）
    ax.fill_between([x0, x1], r.range_low, r.range_high, color="#f2b705", alpha=0.15, lw=0)
    ax.hlines([r.range_low, r.range_high], x0, x1, colors="#c98a00", lw=1.2)
    # IF 目標線（もし上抜けたら）
    ax.hlines(r.if_target, x_last, x1, colors="#2e8b57", lw=1.6, linestyles="--")

    gain = (r.if_target / r.close - 1) * 100
    ax.text(x1, r.if_target, f"IF target {fmt_price(r.if_target)} ({gain:+.1f}%) ",
            ha="right", va="bottom", fontsize=9, color="#2e8b57", fontweight="bold")
    ax.text(x1, r.range_high, f"range high {fmt_price(r.range_high)} ",
            ha="right", va="bottom", fontsize=8, color="#8a5f00")
    ax.text(x1, r.range_low, f"range low {fmt_price(r.range_low)} ",
            ha="right", va="top", fontsize=8, color="#8a5f00")
    slope_txt = "" if math.isnan(r.slope_pct) else f"  slope {r.slope_pct:+.1f}%"
    ax.text(0.01, 0.97,
            f"Range {fmt_price(r.range_low)}-{fmt_price(r.range_high)}  band {r.band_pct:.1f}%  "
            f"age {r.range_age}d{slope_txt}\n"
            f"score {r.metrics['score']:.0f}  pos {r.metrics['pos']}  bbw_pctile {r.metrics['bbw_pctile']}  "
            f"atr_ratio {r.metrics['atr_ratio']}  vol_ratio {r.metrics['vol_ratio']}",
            transform=ax.transAxes, ha="left", va="top", fontsize=8.5, color="#333",
            bbox={"boxstyle": "round,pad=0.4", "fc": "white", "ec": "#ddd", "alpha": 0.9})

    ax.set_title(f"{r.code}   close {fmt_price(r.close)}   {r.last_date}   [{tag}]",
                 loc="left", fontsize=11, fontweight="bold")
    y_lo = min(float(d["Low"].min()), r.range_low) * 0.97
    y_hi = max(float(d["High"].max()), r.if_target) * 1.03
    ax.set_ylim(y_lo, y_hi)
    ax.set_xlim(mdates.date2num(pd.Timestamp(d.index[0]).to_pydatetime()), x1)
    ax.grid(alpha=0.3)
    ax.set_ylabel("Price (JPY)")

    axv.bar(d.index, d["Volume"], width=0.8, color="#9aa5b1")
    axv.set_ylabel("Volume")
    axv.yaxis.set_major_formatter(
        FuncFormatter(lambda v, _: f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K"))
    axv.grid(alpha=0.3)
    axv.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (filename or f"{r.code}_{r.last_date}.png")
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- notify
def build_message(date: str, hits: list) -> str:
    lines = [f"📦 レンジ候補 {date}（{len(hits)}銘柄）"]
    for r in hits:
        lines.append(
            f"・{r.code} {r.name}  終値 {fmt_price(r.close)}  "
            f"箱 {fmt_price(r.range_low)}–{fmt_price(r.range_high)}（幅{r.band_pct:.1f}%・{r.range_age}日）  "
            f"IF {fmt_price(r.if_target)}  score {r.metrics['score']:.0f}"
        )
    return "\n".join(lines)[:1900]  # Discord の content 上限は 2000 文字


def notify_discord(content: str, image_paths: list) -> bool:
    """Discord Webhook へ投稿する（画像は最大10枚）。URL は環境変数からのみ読み、ログにも出さない。"""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("[notify] DISCORD_WEBHOOK_URL not set -> skip")
        return False
    import requests

    handles = []
    files = {}
    try:
        for i, p in enumerate(list(image_paths)[:10]):
            fh = open(p, "rb")
            handles.append(fh)
            files[f"files[{i}]"] = (Path(p).name, fh, "image/png")
        resp = requests.post(url, data={"payload_json": json.dumps({"content": content})},
                             files=files or None, timeout=30)
        if resp.status_code >= 300:
            print(f"[notify] Discord responded {resp.status_code}: {resp.text[:200]}")
            return False
        print(f"[notify] sent ({len(files)} image(s))")
        return True
    except requests.RequestException as e:
        # 例外メッセージには URL（トークン入り）が含まれうるので型名だけ出す
        print(f"[notify] failed: {type(e).__name__}")
        return False
    finally:
        for fh in handles:
            fh.close()


# --------------------------------------------------------------------------- reporting
def print_table(results: list) -> None:
    head = (f"{'code':<17}{'ok':<4}{'close':>9}{'box(low-high)':>19}{'band%':>7}{'slope%':>8}"
            f"{'turn(oku)':>10}{'age':>5}{'score':>7}{'pos':>5}{'vol_r':>7}{'bbw_p':>7}{'atr_r':>7}  reasons")
    print(head)
    print("-" * len(head))
    for r in results:
        m = r.metrics
        box = f"{fmt_price(r.range_low)}-{fmt_price(r.range_high)}"
        turn = "-" if math.isnan(r.turnover) else f"{r.turnover / 1e8:.2f}"
        print(f"{r.code:<17}{('OK' if r.ok else '--'):<4}{fmt_price(r.close):>9}{box:>19}"
              f"{r.band_pct:>7.1f}{r.slope_pct:>8.1f}{turn:>10}{r.range_age:>5}"
              f"{m['score']:>7.1f}{m['pos']:>5}{m['vol_ratio']:>7}{m['bbw_pctile']:>7}{m['atr_ratio']:>7}"
              f"  {'; '.join(r.reasons)}")


def write_json(path: Path, date: str, results: list) -> None:
    payload = {"date": date, "config": CONFIG, "results": [_clean(asdict(r)) for r in results]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[json] wrote {path}")


# --------------------------------------------------------------------------- runners
def run_demo(args) -> int:
    """合成データ 5 銘柄を評価し、RANGE_GOOD だけが検出されれば 0、そうでなければ 1 を返す。"""
    out_dir = args.out or Path(tempfile.gettempdir()) / "stock_bot_diary_demo"
    results = []
    frames = {}
    for i, kind in enumerate(DEMO_KINDS):
        df = make_synthetic(kind, seed=100 + i)
        r = evaluate(f"DEMO_{kind}", kind, df)
        results.append(r)
        frames[r.code] = df

    print(f"== demo: synthetic {len(DEMO_KINDS)} tickers (expected detection: {sorted(DEMO_EXPECT)})")
    print_table(results)

    detected = {r.name for r in results if r.ok}
    charts = []
    if not args.no_chart:
        for r in results:
            if r.ok:
                charts.append(render_if_chart(frames[r.code], r, out_dir))
        for p in charts:
            print(f"[chart] {p}")
    if args.json:
        write_json(args.json, today_jst(), results)

    if detected == DEMO_EXPECT:
        print(f"DEMO PASS: detected {sorted(detected)} == expected {sorted(DEMO_EXPECT)}")
        return 0
    print(f"DEMO FAIL: detected {sorted(detected)} != expected {sorted(DEMO_EXPECT)}")
    return 1


def run_scan(args) -> int:
    date = today_jst()
    tickers = load_tickers(args.tickers)
    if args.limit:
        tickers = tickers[: args.limit]
    out_dir = args.out or (ROOT / "data" / "charts")
    print(f"== scan {date}: {len(tickers)} tickers from {args.tickers}")

    results = []
    frames = {}
    for code, name in tickers:
        df = fetch_yfinance(code)
        r = evaluate(code, name, df)
        results.append(r)
        frames[code] = df
        time.sleep(0.4)  # yfinance への礼儀

    print_table(results)
    hits = sorted((r for r in results if r.ok), key=lambda r: -r.metrics["score"])

    charts = []
    if hits and not args.no_chart:
        for r in hits:
            charts.append(render_if_chart(frames[r.code], r, out_dir))
        for p in charts:
            print(f"[chart] {p}")
    if args.json:
        write_json(args.json, date, results)

    if not hits:
        print("quiet day: no range candidates -> no notification")
    elif args.no_notify:
        print(f"{len(hits)} candidate(s); notification skipped (--no-notify)")
    else:
        notify_discord(build_message(date, hits), charts)
    return 0


def main(argv=None) -> int:
    setup_stdout()
    ap = argparse.ArgumentParser(description="株BOTダイアリー レンジ検出エンジン")
    ap.add_argument("--demo", action="store_true", help="合成データ 5 銘柄の自己テスト（RANGE_GOOD のみ検出で合格）")
    ap.add_argument("--tickers", type=Path, default=ROOT / "tickers.txt", help="監視銘柄ファイル")
    ap.add_argument("--out", type=Path, default=None,
                    help="チャート出力先（既定: data/charts。--demo 時は一時ディレクトリ）")
    ap.add_argument("--json", type=Path, default=None, help="スキャン結果を JSON で書き出す（動作確認用）")
    ap.add_argument("--limit", type=int, default=None, help="先頭 N 銘柄だけ処理する（動作確認用）")
    ap.add_argument("--no-chart", action="store_true", help="チャートを描かない")
    ap.add_argument("--no-notify", action="store_true", help="Discord へ通知しない")
    args = ap.parse_args(argv)
    return run_demo(args) if args.demo else run_scan(args)


if __name__ == "__main__":
    sys.exit(main())
