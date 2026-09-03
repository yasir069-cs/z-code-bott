"""Phase 3 tests — liquidation sweep detection on crafted candles.

All 4 documented conditions must hold; each negative case violates exactly
one of them.
"""
import numpy as np
import pandas as pd

import filter_1h
from conftest import make_candles


def _base_frame(n=40, level=100.0):
    """Flat candles oscillating around `level` (swing low ~= level - wick)."""
    closes = level + np.sin(np.arange(n) * 0.7) * 0.5
    vols = np.full(n, 1000.0)
    return make_candles(closes, vol_base=1000.0, vol_spread=0.0, wick=0.2, seed=1, volumes=vols)


def _set_last_candle(df, o, h, l, c, v):
    df.iloc[-1, df.columns.get_loc("open")] = o
    df.iloc[-1, df.columns.get_loc("high")] = h
    df.iloc[-1, df.columns.get_loc("low")] = l
    df.iloc[-1, df.columns.get_loc("close")] = c
    df.iloc[-1, df.columns.get_loc("volume")] = v
    return df


def _swing_low(df):
    return df["low"].iloc[-21:-1].min()


def test_bullish_sweep_detected():
    df = _base_frame()
    swing = _swing_low(df)
    # small body, long lower wick piercing swing low, close back above, volume spike
    df = _set_last_candle(df, o=swing + 0.2, h=swing + 0.3, l=swing - 1.5, c=swing + 0.25, v=2500.0)
    s = filter_1h.detect_sweep(df, "BUY")
    assert s is not None
    assert s["age_candles"] == 0
    assert s["level"] == swing
    assert s["wick_body_ratio"] > 2.0
    assert s["volume_ratio"] > 1.5


def test_bullish_sweep_rejects_close_below_level():
    df = _base_frame()
    swing = _swing_low(df)
    df = _set_last_candle(df, o=swing + 0.2, h=swing + 0.3, l=swing - 1.5, c=swing - 0.5, v=2500.0)
    assert filter_1h.detect_sweep(df, "BUY") is None


def test_bullish_sweep_rejects_short_wick():
    df = _base_frame()
    swing = _swing_low(df)
    # wick 0.15 vs body 0.5 -> wick < 2x body
    df = _set_last_candle(df, o=swing + 0.3, h=swing + 0.4, l=swing - 0.15, c=swing + 0.8, v=2500.0)
    assert filter_1h.detect_sweep(df, "BUY") is None


def test_bullish_sweep_rejects_no_volume_spike():
    df = _base_frame()
    swing = _swing_low(df)
    df = _set_last_candle(df, o=swing + 0.2, h=swing + 0.3, l=swing - 1.5, c=swing + 0.25, v=1000.0)
    assert filter_1h.detect_sweep(df, "BUY") is None


def test_bullish_sweep_rejects_no_pierce():
    df = _base_frame()
    swing = _swing_low(df)
    # wick stays above the swing low -> nothing was swept
    df = _set_last_candle(df, o=swing + 0.2, h=swing + 0.3, l=swing + 0.05, c=swing + 0.25, v=2500.0)
    assert filter_1h.detect_sweep(df, "BUY") is None


def test_bearish_sweep_detected():
    df = _base_frame()
    swing_high = df["high"].iloc[-21:-1].max()
    df = _set_last_candle(df, o=swing_high - 0.2, l=swing_high - 0.3, h=swing_high + 1.5,
                          c=swing_high - 0.25, v=2500.0)
    s = filter_1h.detect_sweep(df, "SELL")
    assert s is not None
    assert s["level"] == swing_high
    assert s["volume_ratio"] > 1.5


def test_bearish_sweep_rejects_close_above_level():
    df = _base_frame()
    swing_high = df["high"].iloc[-21:-1].max()
    df = _set_last_candle(df, o=swing_high - 0.2, l=swing_high - 0.3, h=swing_high + 1.5,
                          c=swing_high + 0.4, v=2500.0)
    assert filter_1h.detect_sweep(df, "SELL") is None


def test_sweep_age_counts_back():
    """A sweep 3 candles ago is found with age_candles == 3."""
    df = _base_frame(n=45)
    swing = _swing_low(df)  # relative to last candle; craft sweep at -4
    p = len(df) - 4
    df.iloc[p, df.columns.get_loc("open")] = swing + 0.2
    df.iloc[p, df.columns.get_loc("high")] = swing + 0.3
    df.iloc[p, df.columns.get_loc("low")] = swing - 1.5
    df.iloc[p, df.columns.get_loc("close")] = swing + 0.25
    df.iloc[p, df.columns.get_loc("volume")] = 2500.0
    s = filter_1h.detect_sweep(df, "BUY")
    assert s is not None and s["age_candles"] == 3
