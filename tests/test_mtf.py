"""Deterministic tests for multi-timeframe combination."""
import mtf


def s(bias):
    return {"bias": bias}


def test_aligned_uptrend_is_long():
    out = mtf.combine(s("bullish"), s("bullish"), s("bullish"))
    assert out["direction"] == "LONG"
    assert out["aligned"] is True
    assert out["counter_htf"] is False and out["penalty"] == 0


def test_entry_against_htf_is_counter_and_penalised():
    out = mtf.combine(s("bullish"), s("bullish"), s("bearish"))
    assert out["direction"] == "LONG"
    assert out["counter_htf"] is True
    assert out["aligned"] is False
    assert out["penalty"] >= 1


def test_neutral_htf_defers_to_setup_with_penalty():
    out = mtf.combine(s("neutral"), s("bearish"), s("bearish"))
    assert out["direction"] == "SHORT"
    assert out["neutral_htf"] is True
    assert out["penalty"] >= 1


def test_no_bias_yields_no_direction():
    out = mtf.combine(s("neutral"), s("neutral"), s("neutral"))
    assert out["direction"] is None
    assert out["aligned"] is False
