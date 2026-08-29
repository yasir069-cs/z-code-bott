"""Market structure — the highest-priority layer of the decision hierarchy.

Deterministic, pure functions over a closed-candle OHLCV frame. Detects:
  * fractal swing pivots (config.STRUCT_PIVOT_LEFT / _RIGHT),
  * the HH / HL / LH / LL label sequence,
  * trend vs range vs consolidation,
  * Break of Structure (BOS, continuation) and Change of Character (CHoCH, the
    first counter-trend break),
  * displacement (impulse) candles and their retest.

Look-ahead safety: a swing pivot is only *confirmed* once STRUCT_PIVOT_RIGHT
candles have closed after it, so the last few candles are never retro-labelled
as pivots. BOS/CHoCH are judged from the current close breaking an
already-confirmed prior swing — the break itself is the live signal, the level
it breaks is historical.

`analyze()` is the module entry point. It never raises on thin data — it
returns a structure dict with ``trend="range"`` and ``bias="neutral"`` when
there are too few swings to classify, so the decision core can treat "not
enough structure" as one of its NO_TRADE reasons rather than crashing.
"""
from typing import Optional

import numpy as np
import pandas as pd

import config
import ta_utils


def swing_pivots(high: np.ndarray, low: np.ndarray,
                 left: int, right: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Strict fractal pivots.

    A swing high at i is the unique maximum of the window [i-left, i+right];
    a swing low is the unique minimum. Uniqueness avoids a flat run producing a
    cluster of adjacent pivots. Only positions with `right` confirmed candles
    after them are considered (no look-ahead).

    Returns (highs, lows), each a list of (position, price) oldest→newest.
    """
    n = len(high)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(left, n - right):
        wh = high[i - left:i + right + 1]
        wl = low[i - left:i + right + 1]
        if high[i] == wh.max() and np.count_nonzero(wh == high[i]) == 1:
            highs.append((i, float(high[i])))
        if low[i] == wl.min() and np.count_nonzero(wl == low[i]) == 1:
            lows.append((i, float(low[i])))
    return highs, lows


def _labelled_swings(highs: list[tuple[int, float]], lows: list[tuple[int, float]],
                     index: pd.Index) -> list[dict]:
    """Merge highs and lows into one time-ordered, labelled swing sequence."""
    merged: list[dict] = []
    for pos, price in highs:
        merged.append({"kind": "H", "pos": pos, "price": price})
    for pos, price in lows:
        merged.append({"kind": "L", "pos": pos, "price": price})
    merged.sort(key=lambda s: s["pos"])

    last_high: Optional[float] = None
    last_low: Optional[float] = None
    for s in merged:
        if s["kind"] == "H":
            s["label"] = None if last_high is None else ("HH" if s["price"] > last_high else "LH")
            last_high = s["price"]
        else:
            s["label"] = None if last_low is None else ("HL" if s["price"] > last_low else "LL")
            last_low = s["price"]
        s["ts"] = index[s["pos"]]
    return merged


def _last_of(swings: list[dict], kind: str) -> Optional[dict]:
    for s in reversed(swings):
        if s["kind"] == kind:
            return s
    return None


def _classify_trend(swings: list[dict], atr: float) -> str:
    """uptrend / downtrend / range / consolidation from the recent swings."""
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return "range"

    # Overall swing span too small relative to ATR -> consolidation.
    span = max(s["price"] for s in swings) - min(s["price"] for s in swings)
    if atr > 0 and span < config.STRUCT_RANGE_ATR * atr:
        return "consolidation"

    higher_highs = highs[-1]["price"] > highs[-2]["price"]
    higher_lows = lows[-1]["price"] > lows[-2]["price"]
    lower_highs = highs[-1]["price"] < highs[-2]["price"]
    lower_lows = lows[-1]["price"] < lows[-2]["price"]

    if higher_highs and higher_lows:
        return "uptrend"
    if lower_highs and lower_lows:
        return "downtrend"
    return "range"


def analyze(df: pd.DataFrame, cfg=config) -> dict:
    """Full market-structure read of a frame's most recent closed candle.

    Returns a dict (see module docstring). Safe on thin data: falls back to
    trend="range", bias="neutral" with empty events.
    """
    empty = {
        "trend": "range", "bias": "neutral", "swings": [],
        "last_swing_high": None, "last_swing_low": None,
        "bos": None, "choch": None, "displacement": None, "retest": None,
        "atr": 0.0,
    }
    if df is None or len(df) < cfg.STRUCT_PIVOT_LEFT + cfg.STRUCT_PIVOT_RIGHT + 2:
        return empty

    window = df.tail(cfg.STRUCT_LOOKBACK)
    high = window["high"].to_numpy(dtype="float64")
    low = window["low"].to_numpy(dtype="float64")
    open_ = window["open"].to_numpy(dtype="float64")
    close = window["close"].to_numpy(dtype="float64")
    atr = ta_utils.atr(window, cfg.STRUCT_ATR_LENGTH)

    highs, lows = swing_pivots(high, low, cfg.STRUCT_PIVOT_LEFT, cfg.STRUCT_PIVOT_RIGHT)
    swings = _labelled_swings(highs, lows, window.index)
    if len(swings) < cfg.STRUCT_MIN_SWINGS:
        out = dict(empty)
        out["swings"] = swings
        out["atr"] = atr
        return out

    trend = _classify_trend(swings[-cfg.STRUCT_TREND_SWINGS * 2:], atr)
    last_high = _last_of(swings, "H")
    last_low = _last_of(swings, "L")
    close_now = float(close[-1])

    # --- BOS (continuation) and CHoCH (first counter-trend break) ---
    bos = None
    choch = None
    if last_high and close_now > last_high["price"]:
        if trend == "downtrend":
            choch = {"dir": "bullish", "level": last_high["price"], "pos": last_high["pos"]}
        else:
            bos = {"dir": "bullish", "level": last_high["price"], "pos": last_high["pos"]}
    elif last_low and close_now < last_low["price"]:
        if trend == "uptrend":
            choch = {"dir": "bearish", "level": last_low["price"], "pos": last_low["pos"]}
        else:
            bos = {"dir": "bearish", "level": last_low["price"], "pos": last_low["pos"]}

    # --- displacement: last candle body dominates ATR ---
    displacement = None
    body = ta_utils.body(open_[-1], close[-1])
    if atr > 0 and body > cfg.STRUCT_DISPLACEMENT_ATR * atr:
        displacement = {
            "dir": "bullish" if close[-1] > open_[-1] else "bearish",
            "size_atr": round(body / atr, 2),
        }

    # --- retest: current price back near the most recently broken level ---
    retest = None
    broken = bos or choch
    if broken and atr > 0 and abs(close_now - broken["level"]) <= cfg.STRUCT_RETEST_ATR * atr:
        retest = {"level": broken["level"], "dir": broken["dir"]}

    # --- bias: a fresh CHoCH (character change) is the newest information and
    #     overrides the standing trend bias; otherwise trend drives bias ---
    if choch is not None:
        bias = choch["dir"]
    elif trend == "uptrend":
        bias = "bullish"
    elif trend == "downtrend":
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "trend": trend,
        "bias": bias,
        "swings": swings,
        "last_swing_high": last_high,
        "last_swing_low": last_low,
        "bos": bos,
        "choch": choch,
        "displacement": displacement,
        "retest": retest,
        "atr": atr,
    }
