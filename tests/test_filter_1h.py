"""1H feature-extractor tests (structure-first rework) + the retained scoring
unit tests.

`filter_1h.analyze_1h` is no longer an indicator gate that *chooses* the trade
direction — it is a thin structure reader that returns a directional funnel hint
(BUY / SELL / None) from the 1H market structure. Indicators are attached as
context only and can never gate here; the authoritative verdict is
``decision.decide()``'s. The first block below asserts that behaviour.

`scoring.py` is KEPT intact as the bounded secondary indicator-confirmation
layer, so its component unit tests (zone / rsi / sweep / confluence / gates)
are preserved verbatim in the second block.
"""
import numpy as np
import pytest

import config
import filter_1h
import scoring
from conftest import make_candles


# --------------------------------------------------------------------------- helpers
def _zig(controls, seg=5):
    """Piecewise-linear close series through `controls` (each leg `seg` candles)."""
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# Proven structure fixtures (mirrors test_decision): clean HH/HL, LH/LL, and flat.
UP = [130, 126, 131, 101, 107, 104, 110, 107, 112, 111, 112.5]
DOWN = [100, 104, 99, 129, 123, 126, 120, 123, 118, 119, 117.5]
RANGE = [100, 101, 99, 100.5, 99.5, 100.5, 99.5, 100.5, 99.5, 100.5, 99.5]


# --------------------------------------------------- analyze_1h feature extractor
def test_analyze_1h_reports_bullish_direction():
    feat = filter_1h.analyze_1h(make_candles(_zig(UP), wick=0.3))
    assert feat is not None
    assert feat["direction"] == "BUY"      # funnel hint, driven by STRUCTURE
    assert feat["bias"] == "bullish"
    assert feat["structure"]["trend"] == "uptrend"
    # context attached, never gating
    assert "sweep" in feat and "indicators" in feat


def test_analyze_1h_reports_bearish_direction():
    feat = filter_1h.analyze_1h(make_candles(_zig(DOWN), wick=0.3))
    assert feat is not None
    assert feat["direction"] == "SELL"
    assert feat["bias"] == "bearish"


def test_analyze_1h_ranging_has_no_direction():
    """A structure-neutral frame yields no funnel hint — a skip, not a crash."""
    feat = filter_1h.analyze_1h(make_candles(_zig(RANGE), wick=0.2))
    assert feat is not None
    assert feat["direction"] is None


def test_analyze_1h_too_short_is_none():
    short = make_candles(_zig(UP), wick=0.3).head(
        config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT)
    assert filter_1h.analyze_1h(short) is None


def test_analyze_1h_indicators_do_not_gate(monkeypatch):
    """The old hard gates (RSI/EMA/VWAP/zone) are gone: even with the indicator
    snapshot degraded to None, a bullish STRUCTURE still produces the BUY funnel
    hint. Structure decides the hint; indicators are only attached context."""
    monkeypatch.setattr(filter_1h, "compute_indicators", lambda _df: None)
    feat = filter_1h.analyze_1h(make_candles(_zig(UP), wick=0.3))
    assert feat["direction"] == "BUY"      # structure alone drove the hint
    assert feat["indicators"] is None      # snapshot degraded, no gate fired


def test_analyze_1h_sweep_is_direction_aligned():
    """When a hint exists, the attached sweep (if any) is on that direction."""
    feat = filter_1h.analyze_1h(make_candles(_zig(UP), wick=0.3))
    if feat["sweep"] is not None:
        assert feat["sweep"]["direction"] == "BUY"


def test_detect_sweep_still_finds_a_manual_bullish_sweep():
    """detect_sweep is unchanged and still reused by liquidity.py + the core."""
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
    sweep = filter_1h.detect_sweep(df, "BUY")
    assert sweep is not None
    assert sweep["direction"] == "BUY"


# ------------------------------------------------------- zone (the note's core)
# "Bottom to inbetween" for BUY / "Top to inbetween" for SELL. This check did
# not exist before the reconstruction: a coin at the TOP of its range could
# alert as a BUY. Kept as a scoring unit test — scoring is the secondary layer.

@pytest.mark.parametrize("range_pos,expected", [
    (0.00, 25.00),   # very bottom          -> full
    (0.29, 25.00),   # inside ZONE_FULL_PCT -> full
    (0.30, 25.00),   # boundary, inclusive  -> full
    (0.45, 17.50),   # halfway through "inbetween" -> midpoint of the taper
    (0.60, 10.00),   # ZONE_MAX_PCT         -> taper floor (25 * 0.40)
])
def test_zone_score_buy_grades(range_pos, expected):
    assert scoring.zone_score(range_pos, "BUY") == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("range_pos", [0.61, 0.75, 1.00])
def test_zone_score_buy_rejects_wrong_half(range_pos):
    with pytest.raises(scoring.Rejected) as err:
        scoring.zone_score(range_pos, "BUY")
    assert err.value.code == scoring.REJECT_ZONE


@pytest.mark.parametrize("range_pos,expected", [
    (1.00, 25.00),   # very top -> full for a SELL
    (0.70, 25.00),
    (0.55, 17.50),
    (0.40, 10.00),
])
def test_zone_score_sell_mirrors_buy(range_pos, expected):
    assert scoring.zone_score(range_pos, "SELL") == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("range_pos", [0.39, 0.20, 0.00])
def test_zone_score_sell_rejects_wrong_half(range_pos):
    with pytest.raises(scoring.Rejected) as err:
        scoring.zone_score(range_pos, "SELL")
    assert err.value.code == scoring.REJECT_ZONE


def test_zone_taper_is_monotonic():
    """No jump in the "inbetween" band — deeper is always worth at least as much."""
    scores = [scoring.zone_score(p / 100.0, "BUY") for p in range(0, 61)]
    assert all(a >= b for a, b in zip(scores, scores[1:]))


# ------------------------------------------------------------------- rsi scoring
def test_rsi_outside_tolerance_band_is_rejected():
    """Outside the tolerance band is a hard gate, not a reduced score."""
    with pytest.raises(scoring.Rejected) as err:
        scoring.rsi_score(81.0, 78.0, "BUY", config.W_1H_RSI)
    assert err.value.code == scoring.REJECT_RSI_BAND


def test_rsi_in_note_band_scores_more_than_tolerance_band():
    """The note's 50-70 scores full; the wider band that production ran scores half."""
    note = scoring.rsi_score(55.0, 51.0, "BUY", config.W_1H_RSI)
    tol = scoring.rsi_score(47.0, 46.0, "BUY", config.W_1H_RSI)
    assert note == config.W_1H_RSI
    assert tol == pytest.approx(config.W_1H_RSI * config.RSI_TOL_FRACTION)
    assert note > tol


# ------------------------------------------------------------- sweep weighting
@pytest.mark.parametrize("age,expected", [
    (0, 25.00),   # last closed candle    -> full
    (2, 25.00),   # SWEEP_AGE_FULL        -> full
    (3, 15.00),   # partial band          -> 25 * 0.60
    (5, 15.00),   # SWEEP_AGE_PARTIAL
    (6, 8.00),    # stale band            -> 25 * 0.32
    (10, 8.00),   # SWEEP_AGE_STALE
    (11, 0.00),   # older than stale      -> not actionable
])
def test_sweep_score_decays_with_age(age, expected):
    assert scoring.sweep_score({"age_candles": age}) == pytest.approx(expected, abs=0.01)


def test_sweep_score_none_is_zero_not_rejection():
    """Owner's decision: no sweep reduces the score, it never drops the coin."""
    assert scoring.sweep_score(None) == 0.0


def test_no_sweep_caps_confidence_below_high():
    """A no-sweep setup can alert, but can never present as high confidence."""
    assert scoring.apply_sweep_confidence_cap(95.0, None) == config.NO_SWEEP_CONFIDENCE_CAP
    assert scoring.apply_sweep_confidence_cap(95.0, {"age_candles": 0}) == 95.0
    assert scoring.apply_sweep_confidence_cap(50.0, None) == 50.0  # already below the cap


def test_confluence_weights_sum_to_one():
    assert config.CONFLUENCE_W_1H + config.CONFLUENCE_W_15M + config.CONFLUENCE_W_5M == 1.0
    assert scoring.confluence(100.0, 100.0, 100.0) == 100.0


def test_one_strong_timeframe_cannot_carry_two_weak():
    total = scoring.confluence(100.0, 40.0, 40.0)
    assert total >= config.MIN_CONFLUENCE          # the weighted total looks fine...
    assert not scoring.passes_gates(100.0, 40.0, 40.0, total)  # ...but per-TF floors still bite
