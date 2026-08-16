"""Phase 4 tests — 1H context classification (monkeypatched indicator
snapshot so the filter logic itself is under test; sweep uses real candles)."""
import pytest

import filter_1h
from conftest import make_candles


def _snap(**over):
    base = dict(
        timestamp=None, open=100.0, high=101.0, low=99.0, close=100.5,
        volume=1500.0, volume_prev=1000.0, volume_avg20=1000.0,
        volume_trend=[1000, 1100, 1200, 1300, 1500],
        rsi=55.0, rsi_prev=51.0, rsi_history=[48, 50, 55, 51, 56],
        ema21=99.0, vwap=99.5,
        bb_lower=99.2, bb_mid=100.0, bb_upper=101.5,
        atr=1.0,
        range_high=110.0, range_low=99.0, range_pos=0.14,  # bottom 30%
        swing_low_20=99.0, swing_high_20=110.0,
    )
    base.update(over)
    return base


def _frame_with_bullish_sweep():
    import numpy as np
    n = 40
    closes = 100 + np.sin(np.arange(n) * 0.7) * 0.5
    df = make_candles(closes, vol_base=1000.0, vol_spread=0.0, wick=0.2, seed=1,
                      volumes=np.full(n, 1000.0))
    swing = df["low"].iloc[-21:-1].min()
    df.iloc[-1, df.columns.get_loc("open")] = swing + 0.2
    df.iloc[-1, df.columns.get_loc("high")] = swing + 0.3
    df.iloc[-1, df.columns.get_loc("low")] = swing - 1.5
    df.iloc[-1, df.columns.get_loc("close")] = swing + 0.25
    df.iloc[-1, df.columns.get_loc("volume")] = 2500.0
    return df


@pytest.fixture
def patch_ind(monkeypatch):
    def _patch(snap):
        monkeypatch.setattr(filter_1h, "compute_indicators", lambda df: snap)
    return _patch


def test_buy_context_passes(patch_ind):
    patch_ind(_snap())
    ctx = filter_1h.analyze_1h(_frame_with_bullish_sweep())
    assert ctx is not None
    assert ctx["direction"] == "BUY"
    assert ctx["sweep"]["direction"] == "BUY"
    assert all(ctx["checks"].values())


def test_context_fails_without_zone(patch_ind):
    patch_ind(_snap(range_pos=0.55))  # not in bottom 30%
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_when_rsi_out_of_range(patch_ind):
    patch_ind(_snap(rsi=45.0))  # below 50
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_when_rsi_falling(patch_ind):
    patch_ind(_snap(rsi=55.0, rsi_prev=60.0))  # in range but dropping
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_below_ema(patch_ind):
    patch_ind(_snap(ema21=101.0))
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_below_vwap(patch_ind):
    patch_ind(_snap(vwap=101.0))
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_when_volume_falling(patch_ind):
    patch_ind(_snap(volume=900.0, volume_prev=1000.0))
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_when_far_from_lower_bb(patch_ind):
    patch_ind(_snap(bb_lower=95.0))
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_without_sweep(patch_ind):
    import numpy as np
    n = 40
    closes = 100 + np.sin(np.arange(n) * 0.7) * 0.5
    frame_no_sweep = make_candles(closes, vol_base=1000.0, vol_spread=0.0, seed=1,
                                  volumes=np.full(n, 1000.0))
    patch_ind(_snap())
    assert filter_1h.analyze_1h(frame_no_sweep) is None


def test_sell_context_passes(patch_ind, monkeypatch):
    import numpy as np
    n = 40
    closes = 100 + np.sin(np.arange(n) * 0.7) * 0.5
    frame = make_candles(closes, vol_base=1000.0, vol_spread=0.0, wick=0.2, seed=1,
                         volumes=np.full(n, 1000.0))
    swing_high = frame["high"].iloc[-21:-1].max()
    frame.iloc[-1, frame.columns.get_loc("open")] = swing_high - 0.2
    frame.iloc[-1, frame.columns.get_loc("low")] = swing_high - 0.3
    frame.iloc[-1, frame.columns.get_loc("high")] = swing_high + 1.5
    frame.iloc[-1, frame.columns.get_loc("close")] = swing_high - 0.25
    frame.iloc[-1, frame.columns.get_loc("volume")] = 2500.0

    patch_ind(_snap(range_pos=0.88, rsi=42.0, rsi_prev=47.0, rsi_history=[55, 50, 42, 46, 41],
                    ema21=102.0, vwap=101.5, bb_upper=101.0, bb_lower=98.0, bb_mid=99.5,
                    low=100.2, high=101.0, close=100.8, open=100.85))
    ctx = filter_1h.analyze_1h(frame)
    assert ctx is not None
    assert ctx["direction"] == "SELL"
    assert ctx["sweep"]["direction"] == "SELL"
