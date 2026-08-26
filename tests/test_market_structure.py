"""Deterministic tests for the market-structure detector."""
import numpy as np

import config
import market_structure as ms
from conftest import make_candles


def ramp(controls, seg=6):
    """Piecewise-linear close series through the given control points.

    Control points spaced `seg` candles apart become the swing pivots, so a
    rising/falling staircase of controls yields clean HH/HL or LH/LL structure.
    """
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# ----------------------------------------------------------------- pure helpers

def test_swing_pivots_are_strict_fractals():
    # a single clean peak at index 4 and trough at index 10
    high = np.array([1, 2, 3, 4, 9, 4, 3, 2, 3, 4, 1, 4, 5, 6, 7], dtype="float64")
    low = high - 1.0
    highs, lows = ms.swing_pivots(high, low, left=3, right=3)
    peak_positions = [p for p, _ in highs]
    assert 4 in peak_positions
    # look-ahead guard: no pivot within `right` candles of the end
    assert all(p <= len(high) - 1 - 3 for p, _ in highs)
    assert all(p <= len(high) - 1 - 3 for p, _ in lows)


def test_classify_consolidation_when_span_below_atr():
    swings = [
        {"kind": "H", "price": 101.0}, {"kind": "L", "price": 100.0},
        {"kind": "H", "price": 101.2}, {"kind": "L", "price": 100.1},
    ]
    # span = 1.2, atr = 5 -> span < STRUCT_RANGE_ATR * atr -> consolidation
    assert ms._classify_trend(swings, atr=5.0) == "consolidation"


# ------------------------------------------------------------- trend + bias

def test_uptrend_is_bullish_with_hh_hl():
    closes = ramp([100, 112, 107, 120, 114, 128, 121, 136])
    st = ms.analyze(make_candles(closes))
    assert st["trend"] == "uptrend"
    assert st["bias"] == "bullish"
    labels = {s["label"] for s in st["swings"]}
    assert "HH" in labels and "HL" in labels


def test_downtrend_is_bearish_with_lh_ll():
    closes = ramp([136, 124, 129, 116, 121, 108, 113, 100])
    st = ms.analyze(make_candles(closes))
    assert st["trend"] == "downtrend"
    assert st["bias"] == "bearish"
    labels = {s["label"] for s in st["swings"]}
    assert "LH" in labels and "LL" in labels


def test_range_is_neutral():
    closes = ramp([100, 105, 100, 105, 100, 105, 100, 105])
    st = ms.analyze(make_candles(closes))
    assert st["bias"] == "neutral"


# ----------------------------------------------------------------- BOS / CHoCH

def test_bos_bullish_on_break_of_last_swing_high():
    # clean uptrend, then a final rally whose close clears the last swing high
    closes = ramp([100, 112, 107, 120, 114, 128, 121]) + [130, 138, 145]
    st = ms.analyze(make_candles(closes))
    assert st["bos"] is not None
    assert st["bos"]["dir"] == "bullish"
    assert st["choch"] is None


def test_choch_bearish_when_uptrend_breaks_down():
    # uptrend, then the final candle collapses below the last swing low
    closes = ramp([100, 112, 107, 120, 114, 128, 121]) + [110, 100, 96]
    st = ms.analyze(make_candles(closes))
    assert st["choch"] is not None
    assert st["choch"]["dir"] == "bearish"
    assert st["bias"] == "bearish"


def test_displacement_detected_on_impulse_candle():
    closes = ramp([100, 103, 101, 104, 102, 105, 103]) + [104]
    df = make_candles(closes)
    # force a large-body final candle (well beyond STRUCT_DISPLACEMENT_ATR * ATR)
    atr = __import__("ta_utils").atr(df, config.STRUCT_ATR_LENGTH)
    last = df.index[-1]
    df.loc[last, "open"] = 104.0
    df.loc[last, "close"] = 104.0 + config.STRUCT_DISPLACEMENT_ATR * atr * 3
    df.loc[last, "high"] = df.loc[last, "close"] + 0.01
    df.loc[last, "low"] = 103.9
    st = ms.analyze(df)
    assert st["displacement"] is not None
    assert st["displacement"]["dir"] == "bullish"


# --------------------------------------------------------------- safe degrade

def test_thin_data_returns_safe_neutral():
    df = make_candles([100, 101, 102, 103])  # far fewer than a full structure needs
    st = ms.analyze(df)
    assert st["trend"] == "range"
    assert st["bias"] == "neutral"
    assert st["bos"] is None and st["choch"] is None
