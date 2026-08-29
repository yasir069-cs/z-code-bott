"""Deterministic tests for the crypto-futures context layer."""
import numpy as np
import pandas as pd

import futures_context as fc


def oi_frame(values):
    idx = pd.date_range("2026-08-14 10:00", periods=len(values), freq="5min", tz="UTC")
    return pd.DataFrame({"oi": np.asarray(values, dtype="float64")}, index=idx)


def price_frame(closes):
    idx = pd.date_range("2026-08-14 10:00", periods=len(closes), freq="5min", tz="UTC")
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c,
                         "volume": np.full(len(c), 1000.0)}, index=idx)


def test_price_up_oi_up_is_bullish_conviction():
    out = fc.interpret(oi_frame([100, 110, 120]), 0.0001, price_frame([100, 105, 110]))
    assert out["available"] is True
    assert out["bias"] == "bullish"
    assert out["conviction"] == "high"


def test_price_down_oi_up_is_bearish_conviction():
    out = fc.interpret(oi_frame([100, 110, 120]), 0.0001, price_frame([110, 105, 100]))
    assert out["bias"] == "bearish"
    assert out["conviction"] == "high"


def test_missing_oi_degrades_with_warning():
    out = fc.interpret(None, 0.0001, price_frame([100, 101, 102]))
    assert "open_interest_unavailable" in out["warnings"]
    assert out["bias"] == "neutral"           # no OI -> no OI-driven bias
    assert out["available"] is True           # funding still present


def test_missing_all_data_is_unavailable():
    out = fc.interpret(None, None, price_frame([100, 101, 102]))
    assert out["available"] is False
    assert "open_interest_unavailable" in out["warnings"]
    assert "funding_unavailable" in out["warnings"]


def test_extreme_funding_is_noted():
    out = fc.interpret(oi_frame([100, 101]), 0.01, price_frame([100, 101]))
    assert any("crowded longs" in n for n in out["notes"])
