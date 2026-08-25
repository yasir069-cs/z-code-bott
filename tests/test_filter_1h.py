"""Phase 4 tests — 1H context classification (monkeypatched indicator
snapshot so the filter logic itself is under test; sweep uses real candles).

Grading model (strategy_spec.md): EMA21 / VWAP / RSI-band / overbought /
squeeze / zone are HARD gates that drop the coin; zone, RSI, volume, BB and
sweep are graded into a 0-100 score that must clear MIN_SCORE_1H.

Note on the fixture bandwidth: (101.5 - 99.2) / 100 = 0.023. Until the
server's BB_BANDWIDTH_MIN = 0.015 was adopted, the shipped 0.025 rejected
EVERY frame here as a squeeze, so the "fails when X" tests below passed
without ever exercising X. They assert the real reason now.
"""
import pytest

import config
import filter_1h
import scoring
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
        range_high=110.0, range_low=99.0, range_pos=0.14,
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
    assert all(ctx["checks"].values())


def test_buy_context_passes_with_sweep(patch_ind):
    patch_ind(_snap())
    ctx = filter_1h.analyze_1h(_frame_with_bullish_sweep())
    assert ctx is not None
    assert ctx["sweep"] is not None
    assert ctx["sweep"]["direction"] == "BUY"


def test_context_fails_when_rsi_below_min(patch_ind):
    patch_ind(_snap(rsi=38.0))  # below the BUY tolerance min (45)
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_when_rsi_above_max(patch_ind):
    """RSI past RSI_OVERBOUGHT is a hard gate — reversal-trap risk."""
    patch_ind(_snap(rsi=79.0, rsi_prev=72.0))  # above RSI_OVERBOUGHT (78)
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


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


def test_context_fails_when_rsi_falling(patch_ind):
    patch_ind(_snap(rsi=55.0, rsi_prev=60.0))  # dropping
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_below_ema(patch_ind):
    patch_ind(_snap(ema21=101.0))  # price 100.5 < ema21 101.0
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_context_fails_below_vwap(patch_ind):
    patch_ind(_snap(vwap=101.0))  # price 100.5 < vwap 101.0
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_falling_volume_scores_zero_but_does_not_reject(patch_ind):
    """Sanctioned deviation: volume is graded, not a gate. Below the previous
    candle AND below the 20-candle average scores 0 — the coin survives on the
    strength of the other conditions, at a reduced score."""
    patch_ind(_snap(volume=900.0, volume_prev=1000.0, volume_avg20=1000.0))
    ctx = filter_1h.analyze_1h(_frame_with_bullish_sweep())
    assert ctx is not None
    assert ctx["score_breakdown"]["volume"] == 0.0
    assert ctx["checks"]["volume_increasing"] is False

    # Same frame, full-volume snapshot: the only delta must be the volume weight.
    patch_ind(_snap())
    full = filter_1h.analyze_1h(_frame_with_bullish_sweep())
    assert ctx["score"] == full["score"] - config.W_1H_VOLUME


def test_far_from_lower_bb_scores_zero_but_does_not_reject(patch_ind):
    """Sanctioned deviation: Bollinger is one graded voice, not a gate.
    Price above the mid-line scores 0 for BB."""
    patch_ind(_snap(bb_lower=95.0, bb_mid=96.0, bb_upper=101.5))
    ctx = filter_1h.analyze_1h(_frame_with_bullish_sweep())
    assert ctx is not None
    assert ctx["score_breakdown"]["bb"] == 0.0
    assert ctx["checks"]["near_lower_bb"] is False


def test_context_passes_without_sweep(patch_ind):
    """Sweep is optional — context passes and sweep is None."""
    import numpy as np
    n = 40
    closes = 100 + np.sin(np.arange(n) * 0.7) * 0.5
    frame_no_sweep = make_candles(closes, vol_base=1000.0, vol_spread=0.0, seed=1,
                                  volumes=np.full(n, 1000.0))
    patch_ind(_snap())
    ctx = filter_1h.analyze_1h(frame_no_sweep)
    assert ctx is not None
    assert ctx["direction"] == "BUY"
    assert ctx["sweep"] is None


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


# ------------------------------------------------------- zone (the note's core)
# "Bottom to inbetween" for BUY / "Top to inbetween" for SELL. This check did
# not exist before the reconstruction: a coin at the TOP of its range could
# alert as a BUY.

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


def test_buy_at_top_of_range_is_rejected(patch_ind):
    """The regression this whole check exists to prevent."""
    patch_ind(_snap(range_pos=0.95))
    assert filter_1h.analyze_1h(_frame_with_bullish_sweep()) is None


def test_zone_taper_is_monotonic():
    """No jump in the "inbetween" band — deeper is always worth at least as much."""
    scores = [scoring.zone_score(p / 100.0, "BUY") for p in range(0, 61)]
    assert all(a >= b for a, b in zip(scores, scores[1:]))


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
