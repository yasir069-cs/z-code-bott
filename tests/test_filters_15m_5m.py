"""Phase 5 + 6 tests — 15M 4/5 scoring and 5M entry checks."""
import pytest

import filter_15m
import filter_5m
from conftest import make_candles


def _snap(**over):
    base = dict(
        timestamp=None, open=100.0, high=100.8, low=99.6, close=100.5,
        volume=1500.0, volume_prev=1000.0, volume_avg20=1000.0,
        volume_trend=[1000, 1100, 1200, 1300, 1500],
        rsi=55.0, rsi_prev=51.0, rsi_history=[42, 44, 46, 48, 47, 50, 55, 51, 53, 56],
        ema21=99.5, vwap=99.8,
        bb_lower=99.7, bb_mid=100.2, bb_upper=101.0,
        atr=0.5,
        range_high=101.0, range_low=99.0, range_pos=0.75,
        swing_low_20=99.0, swing_high_20=101.0,
    )
    base.update(over)
    return base


@pytest.fixture
def patch15(monkeypatch):
    def _p(snap):
        monkeypatch.setattr(filter_15m, "compute_indicators", lambda df: snap)
    return _p


@pytest.fixture
def patch5(monkeypatch):
    def _p(snap):
        monkeypatch.setattr(filter_5m, "compute_indicators", lambda df: snap)
    return _p


_frame = lambda: make_candles([100.0] * 40)


# ------------------------------------------------------------------ 15M BUY
def test_15m_buy_5_of_5_passes(patch15):
    patch15(_snap())
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is not None and res["score"] == 5


def test_15m_buy_4_of_5_passes(patch15):
    patch15(_snap(ema21=100.9))  # only EMA condition fails
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is not None and res["score"] == 4


def test_15m_buy_3_of_5_rejects(patch15):
    patch15(_snap(ema21=100.9, vwap=100.9))  # EMA + VWAP fail -> 3/5
    assert filter_15m.confirm_15m(_frame(), "BUY") is None


def test_15m_buy_far_from_bb_still_4_of_5(patch15):
    # BB is the 5th condition; missing it alone must not reject
    patch15(_snap(bb_lower=95.0))
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is not None and res["score"] == 4


# ----------------------------------------------------------------- 15M SELL
def test_15m_sell_4_of_5_passes(patch15):
    patch15(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[62, 60, 58, 56, 55, 50, 42, 46, 43, 41],
                  ema21=101.2, vwap=101.0, bb_upper=100.7, high=101.0, low=100.2,
                  close=100.4, open=100.6, range_pos=0.8))
    res = filter_15m.confirm_15m(_frame(), "SELL")
    assert res is not None and res["direction"] == "SELL" and res["score"] >= 4


def test_15m_sell_3_of_5_rejects(patch15):
    # rsi ✓, below-ema ✓, below-vwap ✓ | volume falling ✗, price far above upper band ✗
    patch15(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[62, 60, 58, 56, 55, 50, 42, 46, 43, 41],
                  ema21=101.2, vwap=101.0, bb_upper=90.0,
                  high=101.0, low=100.2, close=100.4, open=100.6,
                  volume=900.0, volume_prev=1000.0))
    assert filter_15m.confirm_15m(_frame(), "SELL") is None


# ------------------------------------------------------------------ 5M BUY
def test_5m_buy_all_conditions_pass(patch5):
    patch5(_snap())
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is not None and res["score"] == 7


def test_5m_buy_5_of_7_passes(patch5):
    # Only 2 conditions fail (volume below avg + far from BB) -> 5/7 still passes
    patch5(_snap(volume=900.0, volume_avg20=1000.0, bb_lower=90.0, close=100.0, low=99.0))
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is not None and res["score"] == 5


def test_5m_buy_4_of_7_rejects(patch5):
    # 3 conditions fail -> 4/7 rejects (< 5)
    patch5(_snap(volume=900.0, volume_avg20=1000.0,
                 close=99.6, bb_lower=99.7, low=99.6,
                 ema21=101.0))
    assert filter_5m.entry_5m(_frame(), "BUY") is None


def test_5m_buy_rsi_higher_low_pattern_passes(patch5):
    # spec example: 50 -> 55 -> 51 -> 56 forms a higher low -> bullish
    patch5(_snap(rsi=56.0, rsi_prev=51.0, rsi_history=[42, 44, 46, 47, 50, 55, 51, 53, 54, 56]))
    assert filter_5m.entry_5m(_frame(), "BUY") is not None


def test_5m_buy_multiple_failures_reject(patch5):
    # flat trend + price below EMA + price below VWAP + volume low -> rejected
    patch5(_snap(rsi_history=[55, 55, 55, 55, 55, 55, 55, 55, 55, 55],
                 ema21=101.0, vwap=101.0, volume=800.0, volume_avg20=1000.0))
    assert filter_5m.entry_5m(_frame(), "BUY") is None


# ----------------------------------------------------------------- 5M SELL
def test_5m_sell_all_conditions_pass(patch5):
    patch5(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[60, 58, 56, 55, 52, 47, 50, 48, 45, 42],
                 ema21=101.2, vwap=101.0, bb_upper=100.9, high=100.9, low=100.2,
                 close=100.4, open=100.6))
    res = filter_5m.entry_5m(_frame(), "SELL")
    assert res is not None and res["direction"] == "SELL"


def test_5m_sell_under_threshold_rejects(patch5):
    # 3+ conditions fail for SELL
    patch5(_snap(rsi=60.0, rsi_prev=47.0, rsi_history=[40, 42, 45, 48, 52, 55, 58, 60, 62, 65],
                 ema21=99.0, vwap=99.0, bb_upper=110.0, high=100.5, low=100.2,
                 close=100.4, open=100.3, volume=800.0, volume_avg20=1000.0))
    assert filter_5m.entry_5m(_frame(), "SELL") is None
