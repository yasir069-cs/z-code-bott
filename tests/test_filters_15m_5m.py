"""Phase 5 + 6 tests — 15M confirmation and 5M entry, graded 0-100.

These replace the old "4 of 5" / "5 of 7" count-of-conditions assertions.
The scoring model (strategy_spec.md) reweights the three shared conditions
onto a 0-100 scale — RSI 40, volume 30, BB 30 — with EMA21 and VWAP as hard
gates, so a failing EMA returns None no matter how good the rest is.
"""
import pytest

import config
import filter_15m
import filter_5m
import scoring
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
def test_15m_buy_all_conditions_score_full(patch15):
    """RSI in the note's band + volume rising + touching the lower band = 100."""
    patch15(_snap())
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is not None
    assert res["score"] == pytest.approx(100.0)
    assert res["direction"] == "BUY"
    assert all(res["checks"].values())


def test_15m_buy_below_ema_is_hard_gate(patch15):
    """EMA21 defines the direction, so it can never be traded off against score."""
    patch15(_snap(ema21=100.9))  # close 100.5 < ema21
    assert filter_15m.confirm_15m(_frame(), "BUY") is None


def test_15m_buy_below_vwap_is_hard_gate(patch15):
    patch15(_snap(vwap=100.9))
    assert filter_15m.confirm_15m(_frame(), "BUY") is None


def test_15m_buy_far_from_bb_loses_only_the_bb_weight(patch15):
    """BB is graded: missing it costs its weight but does not reject."""
    patch15(_snap(bb_lower=95.0, bb_mid=96.0))
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is not None
    assert res["score_breakdown"]["bb"] == 0.0
    assert res["score"] == pytest.approx(100.0 - config.W_LTF_BB)


def test_15m_buy_rejects_when_score_below_floor(patch15):
    """Losing BB *and* volume drops below MIN_SCORE_15M -> rejected."""
    patch15(_snap(bb_lower=95.0, bb_mid=96.0,
                  volume=900.0, volume_prev=1000.0, volume_avg20=1000.0))
    res = filter_15m.confirm_15m(_frame(), "BUY")
    assert res is None  # 40 < MIN_SCORE_15M (50)


# ----------------------------------------------------------------- 15M SELL
def test_15m_sell_passes(patch15):
    patch15(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[62, 60, 58, 56, 55, 50, 42, 46, 43, 41],
                  ema21=101.2, vwap=101.0, bb_upper=100.7, bb_mid=100.9,
                  high=101.0, low=100.2, close=100.4, open=100.6, range_pos=0.8))
    res = filter_15m.confirm_15m(_frame(), "SELL")
    assert res is not None
    assert res["direction"] == "SELL"
    assert res["score"] >= config.MIN_SCORE_15M


def test_15m_sell_rejects_when_score_below_floor(patch15):
    patch15(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[62, 60, 58, 56, 55, 50, 42, 46, 43, 41],
                  ema21=101.2, vwap=101.0, bb_upper=90.0, bb_mid=89.0,
                  high=101.0, low=100.2, close=100.4, open=100.6,
                  volume=900.0, volume_prev=1000.0, volume_avg20=1000.0))
    assert filter_15m.confirm_15m(_frame(), "SELL") is None


def test_15m_rejects_bad_direction_argument():
    with pytest.raises(ValueError):
        filter_15m.confirm_15m(_frame(), "HOLD")


# ------------------------------------------------------------------ 5M BUY
def test_5m_buy_all_conditions_score_full(patch5):
    patch5(_snap())
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is not None
    assert res["score"] == pytest.approx(100.0)


def test_5m_buy_partial_volume_scores_fraction(patch5):
    """Flat vs the previous candle but above the 20-candle average -> partial
    credit, per the sanctioned "volume is graded" deviation."""
    patch5(_snap(volume=1100.0, volume_prev=1200.0, volume_avg20=1000.0))
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is not None
    assert res["score_breakdown"]["volume"] == pytest.approx(
        config.W_LTF_VOLUME * config.VOLUME_AVG_FRACTION, abs=0.01)


def test_5m_buy_below_ema_is_hard_gate(patch5):
    patch5(_snap(ema21=101.0))
    assert filter_5m.entry_5m(_frame(), "BUY") is None


def test_5m_buy_rejects_when_score_below_floor(patch5):
    patch5(_snap(volume=900.0, volume_prev=1000.0, volume_avg20=1000.0,
                 bb_lower=90.0, bb_mid=91.0))
    assert filter_5m.entry_5m(_frame(), "BUY") is None


# --------------------------------------------------------- 5M RSI bounce flag
def test_5m_rsi_higher_low_pattern_is_detected(patch5):
    """The note's "RSI 50 above to 70" implies a bounce: 50 -> 55 -> 51 -> 56
    holds a higher low. Reported as metadata, never as extra points."""
    patch5(_snap(rsi=56.0, rsi_prev=51.0,
                 rsi_history=[42, 44, 46, 47, 50, 55, 51, 53, 54, 56]))
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is not None
    assert res["rsi_bounce_detected"] is True


def test_5m_rsi_bounce_does_not_inflate_the_score(patch5):
    """The 0-100 scale must stay intact — the badge is metadata only."""
    patch5(_snap(rsi=56.0, rsi_prev=51.0,
                 rsi_history=[42, 44, 46, 47, 50, 55, 51, 53, 54, 56]))
    bounce = filter_5m.entry_5m(_frame(), "BUY")
    assert bounce["score"] <= 100.0
    assert sum(bounce["score_breakdown"].values()) == pytest.approx(bounce["score"], abs=0.01)


def test_5m_flat_rsi_is_not_a_bounce(patch5):
    patch5(_snap(rsi=55.0, rsi_prev=55.0, rsi_history=[55] * 10))
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert res is None  # flat RSI fails the trend gate outright


# ----------------------------------------------------------------- 5M SELL
def test_5m_sell_passes(patch5):
    patch5(_snap(rsi=42.0, rsi_prev=47.0, rsi_history=[60, 58, 56, 55, 52, 47, 50, 48, 45, 42],
                 ema21=101.2, vwap=101.0, bb_upper=100.9, bb_mid=100.95,
                 high=100.9, low=100.2, close=100.4, open=100.6))
    res = filter_5m.entry_5m(_frame(), "SELL")
    assert res is not None
    assert res["direction"] == "SELL"


def test_5m_sell_rejects_when_rsi_rising(patch5):
    """RSI must be *moving through* the band, not merely sitting in it."""
    patch5(_snap(rsi=48.0, rsi_prev=44.0, rsi_history=[40, 42, 45, 46, 47, 48],
                 ema21=101.2, vwap=101.0, bb_upper=100.9, bb_mid=100.95,
                 high=100.9, low=100.2, close=100.4, open=100.6))
    assert filter_5m.entry_5m(_frame(), "SELL") is None


def test_5m_rejects_bad_direction_argument():
    with pytest.raises(ValueError):
        filter_5m.entry_5m(_frame(), "MAYBE")


# -------------------------------------------------------- scale invariants
def test_ltf_weights_sum_to_100():
    assert config.W_LTF_RSI + config.W_LTF_VOLUME + config.W_LTF_BB == 100


def test_1h_weights_sum_to_100():
    assert (config.W_1H_ZONE + config.W_1H_RSI + config.W_1H_VOLUME
            + config.W_1H_BB + config.W_1H_SWEEP) == 100


def test_ltf_score_never_exceeds_100(patch5):
    patch5(_snap())
    res = filter_5m.entry_5m(_frame(), "BUY")
    assert 0.0 <= res["score"] <= 100.0
