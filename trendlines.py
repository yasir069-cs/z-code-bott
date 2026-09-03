"""Trendlines & channels — priority-5 layer (confluence only).

Fits a support line through swing lows and a resistance line through swing highs
by least-squares, keeping a line only when at least config.TL_MIN_TOUCHES swings
sit within TL_TOLERANCE_ATR*ATR of it and its slope is not implausibly steep
(TL_MAX_SLOPE_PCT of price per candle). Reports a line break (close beyond the
projected line by TL_BREAK_ATR*ATR) and whether price is retesting the line, and
flags a channel when both lines run roughly parallel.

Per Yasir's hierarchy this layer is **confluence only, never standalone**: the
decision core adds small weight for a trendline break/retest that agrees with
structure, but a trendline event never triggers a trade by itself. Pure and
look-ahead-safe (swings need right-side confirmation; only closed candles used).
"""
from typing import Optional

import numpy as np
import pandas as pd

import config
import market_structure as ms
import ta_utils


def _fit_line(points: list[tuple[int, float]], price: float, atr: float,
              cfg) -> Optional[dict]:
    """Least-squares line through pivot points; kept only if enough touch it
    and the slope is valid. Returns {slope, intercept, touches} or None."""
    if len(points) < cfg.TL_MIN_TOUCHES or atr <= 0:
        return None
    xs = np.array([p for p, _ in points], dtype="float64")
    ys = np.array([pr for _, pr in points], dtype="float64")
    slope, intercept = np.polyfit(xs, ys, 1)
    if price > 0 and abs(slope) / price > cfg.TL_MAX_SLOPE_PCT:
        return None                                   # too steep to be a trendline
    resid = np.abs(ys - (slope * xs + intercept))
    touches = int(np.count_nonzero(resid <= cfg.TL_TOLERANCE_ATR * atr))
    if touches < cfg.TL_MIN_TOUCHES:
        return None
    return {"slope": float(slope), "intercept": float(intercept), "touches": touches}


def analyze(df: pd.DataFrame, cfg=config) -> dict:
    """Fit support/resistance trendlines and detect break / retest / channel."""
    empty = {"support_line": None, "resistance_line": None, "channel": False,
             "break": None, "atr": 0.0}
    if df is None or len(df) < cfg.STRUCT_PIVOT_LEFT + cfg.STRUCT_PIVOT_RIGHT + 2:
        return empty

    window = df.tail(cfg.TL_LOOKBACK)
    high = window["high"].to_numpy(dtype="float64")
    low = window["low"].to_numpy(dtype="float64")
    close = window["close"].to_numpy(dtype="float64")
    atr = ta_utils.atr(window, cfg.STRUCT_ATR_LENGTH)
    if atr <= 0:
        return empty
    price = float(close[-1])
    last_x = len(window) - 1

    highs, lows = ms.swing_pivots(high, low, cfg.STRUCT_PIVOT_LEFT, cfg.STRUCT_PIVOT_RIGHT)
    support_line = _fit_line(lows, price, atr, cfg)
    resistance_line = _fit_line(highs, price, atr, cfg)

    btol = cfg.TL_BREAK_ATR * atr
    rtol = cfg.TL_TOLERANCE_ATR * atr
    break_ev = None
    if support_line is not None:
        val = support_line["slope"] * last_x + support_line["intercept"]
        support_line["value_now"] = round(val, 8)
        support_line["break"] = price < val - btol
        support_line["retest"] = abs(price - val) <= rtol
        if support_line["break"]:
            break_ev = {"dir": "bearish", "line": "support"}
    if resistance_line is not None:
        val = resistance_line["slope"] * last_x + resistance_line["intercept"]
        resistance_line["value_now"] = round(val, 8)
        resistance_line["break"] = price > val + btol
        resistance_line["retest"] = abs(price - val) <= rtol
        if resistance_line["break"]:
            break_ev = {"dir": "bullish", "line": "resistance"}

    channel = False
    if support_line is not None and resistance_line is not None:
        s1, s2 = support_line["slope"], resistance_line["slope"]
        denom = max(abs(s1), abs(s2), 1e-9)
        channel = ((s1 >= 0) == (s2 >= 0)) and abs(s1 - s2) / denom <= 0.5

    return {"support_line": support_line, "resistance_line": resistance_line,
            "channel": channel, "break": break_ev, "atr": atr}
