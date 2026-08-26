"""Deterministic tests for the trendline / channel detector."""
import numpy as np

import config
import trendlines as tl
from conftest import make_candles


def zigzag(controls, seg=5):
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# a clean rising channel: swing lows 100->103, swing highs 106->109 (parallel)
RISING = [100, 106, 101, 107, 102, 108, 103, 109]


def test_rising_support_line_detected():
    out = tl.analyze(make_candles(zigzag(RISING), wick=0.15))
    assert out["support_line"] is not None
    assert out["support_line"]["slope"] > 0
    assert out["support_line"]["touches"] >= config.TL_MIN_TOUCHES


def test_channel_when_lines_parallel():
    out = tl.analyze(make_candles(zigzag(RISING), wick=0.15))
    assert out["support_line"] is not None
    assert out["resistance_line"] is not None
    assert out["channel"] is True


def test_support_break_is_bearish():
    closes = zigzag(RISING)
    closes.append(94.0)   # final candle collapses well below the rising support
    out = tl.analyze(make_candles(closes, wick=0.15))
    assert out["break"] is not None
    assert out["break"]["dir"] == "bearish"
    assert out["support_line"]["break"] is True


def test_thin_data_degrades_safely():
    out = tl.analyze(make_candles([100, 101, 102, 103]))
    assert out["support_line"] is None
    assert out["resistance_line"] is None
    assert out["break"] is None
