"""Shared, dependency-light technical helpers for the price-action detectors.

Every detector (market_structure, support_resistance, liquidity, price_action,
trendlines) needs the same primitives: an ATR to size tolerances in price
terms, and candle-geometry measures (body, wicks). They live here so the
detectors carry no duplicated math and no magic numbers.

ATR here is a deliberately simple, fully deterministic mean of true range
(SMA-of-TR), not Wilder's smoothing. The detectors only use it to turn
"config * ATR" into a price tolerance (zone width, "near" distance); the exact
smoothing is irrelevant and a plain mean is trivial to reason about in tests.
The precise TradingView-matching ATR still lives in indicators.compute_indicators
for the secondary indicator layer.

All functions are pure and operate on the closed-candle frames scanner produces
(UTC-indexed OHLCV, float64, last row = most recent closed candle). Nothing here
looks past the rows it is given, so there is no look-ahead.
"""
from typing import Optional

import numpy as np
import pandas as pd


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Wilder true range per bar. TR[0] falls back to the bar's high-low."""
    tr = np.empty(len(high), dtype="float64")
    tr[0] = high[0] - low[0]
    if len(high) > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum.reduce([
            high[1:] - low[1:],
            np.abs(high[1:] - prev_close),
            np.abs(low[1:] - prev_close),
        ])
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> float:
    """Mean of the last `length` true-range values (0.0 on an empty frame)."""
    if df is None or len(df) == 0:
        return 0.0
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    tr = true_range(high, low, close)
    window = tr[-length:] if len(tr) >= length else tr
    return float(np.mean(window)) if len(window) else 0.0


def body(open_: float, close: float) -> float:
    """Absolute candle body size."""
    return abs(close - open_)


def upper_wick(open_: float, high: float, close: float) -> float:
    return high - max(open_, close)


def lower_wick(open_: float, low: float, close: float) -> float:
    return min(open_, close) - low


def candle_range(high: float, low: float) -> float:
    return high - low


def safe_pct(numerator: float, denominator: float) -> Optional[float]:
    """numerator/denominator, or None when the denominator is zero — used so a
    missing/degenerate value degrades safely instead of raising or fabricating."""
    if denominator == 0:
        return None
    return numerator / denominator
