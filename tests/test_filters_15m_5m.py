"""15M setup + 5M entry **feature-extractor** tests (structure-first rework).

`confirm_15m` / `entry_5m` no longer hard-gate on the indicator checklist. They
read that timeframe's market structure and report whether it *aligns* with the
direction the funnel is considering, attach the indicator snapshot as bounded
secondary context, and (5M) report the prospective entry price + the RSI-bounce
badge. They return None ONLY when the frame is too short — a counter-structure
or ranging frame still returns a dict (with ``aligned=False``); the authoritative
accept/reject is ``decision.decide()``'s job across all three timeframes.

The two weight-sum invariants at the bottom still guard the (now secondary)
scoring weights in config and are kept verbatim.
"""
import numpy as np
import pytest

import config
import filter_15m
import filter_5m
from conftest import make_candles


# --------------------------------------------------------------------------- helpers
def _zig(controls, seg=5):
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# Proven structure fixtures (shared with test_decision / test_filter_1h).
UP = [130, 126, 131, 101, 107, 104, 110, 107, 112, 111, 112.5]
DOWN = [100, 104, 99, 129, 123, 126, 120, 123, 118, 119, 117.5]
RANGE = [100, 101, 99, 100.5, 99.5, 100.5, 99.5, 100.5, 99.5, 100.5, 99.5]

_up = lambda: make_candles(_zig(UP), wick=0.3)
_down = lambda: make_candles(_zig(DOWN), wick=0.3)
_range = lambda: make_candles(_zig(RANGE), wick=0.2)
_TOO_SHORT = config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT


# ============================================================= 15M setup TF
def test_15m_buy_aligned_on_bullish_structure():
    res = filter_15m.confirm_15m(_up(), "BUY")
    assert res is not None
    assert res["direction"] == "BUY"
    assert res["bias"] == "bullish"
    assert res["aligned"] is True
    assert "structure" in res and "indicators" in res


def test_15m_sell_aligned_on_bearish_structure():
    res = filter_15m.confirm_15m(_down(), "SELL")
    assert res is not None
    assert res["bias"] == "bearish"
    assert res["aligned"] is True


def test_15m_counter_structure_is_not_rejected():
    """A 15M that opposes the funnel direction is reported unaligned, NOT dropped
    — the decision core weighs MTF conflict; the extractor never vetoes."""
    res = filter_15m.confirm_15m(_up(), "SELL")
    assert res is not None
    assert res["bias"] == "bullish"
    assert res["aligned"] is False


def test_15m_ranging_returns_unaligned():
    res = filter_15m.confirm_15m(_range(), "BUY")
    assert res is not None
    assert res["aligned"] is False


def test_15m_too_short_is_none():
    assert filter_15m.confirm_15m(_up().head(_TOO_SHORT), "BUY") is None


def test_15m_rejects_bad_direction_argument():
    with pytest.raises(ValueError):
        filter_15m.confirm_15m(_up(), "HOLD")


def test_15m_indicators_do_not_gate(monkeypatch):
    """A degraded indicator snapshot never changes alignment — structure alone
    drives it, and the (None) snapshot is passed through as context."""
    monkeypatch.setattr(filter_15m, "compute_indicators", lambda _df: None)
    res = filter_15m.confirm_15m(_up(), "BUY")
    assert res["aligned"] is True
    assert res["indicators"] is None


# ============================================================== 5M entry TF
def test_5m_buy_aligned_on_bullish_structure():
    df = _up()
    res = filter_5m.entry_5m(df, "BUY")
    assert res is not None
    assert res["direction"] == "BUY"
    assert res["bias"] == "bullish"
    assert res["aligned"] is True
    assert res["entry_price"] == pytest.approx(float(df["close"].iloc[-1]))
    assert "rsi_bounce_detected" in res and "rsi_pattern" in res


def test_5m_sell_aligned_on_bearish_structure():
    res = filter_5m.entry_5m(_down(), "SELL")
    assert res is not None
    assert res["bias"] == "bearish"
    assert res["aligned"] is True


def test_5m_counter_structure_is_not_rejected():
    res = filter_5m.entry_5m(_up(), "SELL")
    assert res is not None
    assert res["aligned"] is False


def test_5m_too_short_is_none():
    assert filter_5m.entry_5m(_up().head(_TOO_SHORT), "BUY") is None


def test_5m_rejects_bad_direction_argument():
    with pytest.raises(ValueError):
        filter_5m.entry_5m(_up(), "MAYBE")


def test_5m_rsi_bounce_detected_is_metadata(monkeypatch):
    """The note's "RSI 50->70" bounce (higher-low around 50) is reported as a
    badge, never as extra score — structure decides, this is metadata only."""
    snap = {"rsi_history": [42, 44, 46, 47, 50, 55, 51, 53, 54, 56]}
    monkeypatch.setattr(filter_5m, "compute_indicators", lambda _df: snap)
    res = filter_5m.entry_5m(_up(), "BUY")
    assert res is not None
    assert res["rsi_bounce_detected"] is True


def test_5m_flat_rsi_is_not_a_bounce(monkeypatch):
    """Flat RSI is not a bounce — but under the new contract it is NOT a reject
    either: the dict still comes back, only the badge is False."""
    monkeypatch.setattr(filter_5m, "compute_indicators", lambda _df: {"rsi_history": [55] * 10})
    res = filter_5m.entry_5m(_up(), "BUY")
    assert res is not None
    assert res["rsi_bounce_detected"] is False


def test_5m_indicators_none_still_returns(monkeypatch):
    """No indicator snapshot at all -> still a valid feature dict, bounce False,
    alignment driven purely by structure."""
    monkeypatch.setattr(filter_5m, "compute_indicators", lambda _df: None)
    res = filter_5m.entry_5m(_up(), "BUY")
    assert res is not None
    assert res["indicators"] is None
    assert res["rsi_bounce_detected"] is False
    assert res["aligned"] is True


# -------------------------------------------------------- scale invariants
def test_ltf_weights_sum_to_100():
    assert config.W_LTF_RSI + config.W_LTF_VOLUME + config.W_LTF_BB == 100


def test_1h_weights_sum_to_100():
    assert (config.W_1H_ZONE + config.W_1H_RSI + config.W_1H_VOLUME
            + config.W_1H_BB + config.W_1H_SWEEP) == 100
