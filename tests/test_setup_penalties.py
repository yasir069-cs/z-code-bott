"""Exhaustion / location / RR penalties in setup-quality scoring.

The PENGU-class bug: a strongly bearish coin sitting ON the 1H low (oversold,
below the lower band, no volume, no room left) scored ~87 because every
primary layer "agreed" with SELL. These tests pin the fix — quality must
reflect a tradable setup, not how bearish/bullish the tape looks.
"""
import config
import mtf
import setup_quality as sq
from setup_quality import _exhaustion_factor, _rr_penalty, _structure_support

# ---------------------------------------------------------- evidence fixtures

STRUCT_BEAR = {"bias": "bearish", "trend": "downtrend",
               "bos": {"dir": "bearish"}, "choch": None,
               "displacement": {"dir": "bearish"}, "retest": None}
STRUCT_BULL = {"bias": "bullish", "trend": "uptrend",
               "bos": {"dir": "bullish"}, "choch": None,
               "displacement": {"dir": "bullish"}, "retest": None}
# bos WITHOUT displacement = directional but NOT strong continuation, so the
# RSI/BB exhaustion exemptions must NOT fire for these.
STRUCT_BEAR_EARLY = {"bias": "bearish", "trend": "downtrend",
                     "bos": {"dir": "bearish"}, "choch": None,
                     "displacement": None, "retest": None}
STRUCT_BULL_EARLY = {"bias": "bullish", "trend": "uptrend",
                     "bos": {"dir": "bullish"}, "choch": None,
                     "displacement": None, "retest": None}
SR_SHORT = {"at_zone": {"side": "resistance", "major": True},
            "nearest_support": {"lo": 80.0, "hi": 82.0},
            "nearest_resistance": {"lo": 120.0, "hi": 122.0}}
SR_LONG = {"at_zone": {"side": "support", "major": True},
           "nearest_support": {"lo": 78.0, "hi": 80.0},
           "nearest_resistance": {"lo": 118.0, "hi": 120.0}}
LIQ_NONE = {"long_ready": False, "short_ready": False, "buy_sweep": None,
            "sell_sweep": None, "equal_lows": [], "equal_highs": []}
PA_BEAR = {"signals": {"bullish": 0, "bearish": 3}, "breakout": None, "absorption": False}
PA_BULL = {"signals": {"bullish": 3, "bearish": 0}, "breakout": None, "absorption": False}
MTF_ALIGNED = {"aligned": True, "counter_htf": False, "choch_reversal": False,
               "neutral_htf": False}
TL_BEAR = {"break": {"dir": "bearish"}, "support_line": None,
           "resistance_line": None, "channel": False}
TL_BULL = {"break": {"dir": "bullish"}, "support_line": None,
           "resistance_line": None, "channel": False}
FUT_BEAR = {"available": True, "bias": "bearish", "conviction": "high"}
FUT_BULL = {"available": True, "bias": "bullish", "conviction": "high"}


def _htf(range_pos, rsi=50.0, close=100.0, bb_lower=90.0, bb_upper=110.0,
         volume=1200.0, vol_prev=1000.0, vol_avg=1000.0):
    """1H indicator snapshot with only the exhaustion-relevant fields."""
    return {"range_pos": range_pos, "rsi": rsi, "close": close,
            "bb_lower": bb_lower, "bb_upper": bb_upper,
            "volume": volume, "volume_prev": vol_prev, "volume_avg20": vol_avg}


def _risk(rr):
    return {"ok": rr is None or rr >= config.MIN_RR, "rr": rr,
            "reasons": [] if rr is None or rr >= config.MIN_RR else ["poor_rr"]}


def _sell(**overrides):
    args = dict(direction="SHORT", structure=STRUCT_BEAR, sr=SR_SHORT,
                liquidity=LIQ_NONE, price_action=PA_BEAR, mtf=MTF_ALIGNED,
                trendlines=TL_BEAR, futures=FUT_BEAR,
                indicator_conf={"score": 0.5},
                htf_snap=_htf(range_pos=0.90), risk=_risk(2.5))
    args.update(overrides)
    return sq.score(**args)


def _buy(**overrides):
    args = dict(direction="LONG", structure=STRUCT_BULL, sr=SR_LONG,
                liquidity=LIQ_NONE, price_action=PA_BULL, mtf=MTF_ALIGNED,
                trendlines=TL_BULL, futures=FUT_BULL,
                indicator_conf={"score": 0.5},
                htf_snap=_htf(range_pos=0.10), risk=_risk(2.5))
    args.update(overrides)
    return sq.score(**args)


# ---------------------------------------------------------- location (req 3)

def test_bearish_setup_near_1h_low_is_heavily_penalized():
    """The PENGU/ONDO shape: SELL with price already at the range floor."""
    good = _sell(htf_snap=_htf(range_pos=0.90))          # short from the top
    exhausted = _sell(htf_snap=_htf(range_pos=0.05))     # short at the floor
    assert exhausted["quality"] < config.QUALITY_MIN
    assert exhausted["quality"] < good["quality"] - 25   # substantial, not cosmetic
    assert any("range_pos=0.05" in p for p in exhausted["penalties"])
    assert exhausted["exhaustion_factor"] < 0.7


def test_bullish_setup_near_1h_high_is_heavily_penalized():
    good = _buy(htf_snap=_htf(range_pos=0.10))           # long from the base
    exhausted = _buy(htf_snap=_htf(range_pos=0.95))      # long at the top
    assert exhausted["quality"] < config.QUALITY_MIN
    assert exhausted["quality"] < good["quality"] - 25
    assert any("range_pos=0.95" in p for p in exhausted["penalties"])


def test_confirmed_sweep_halves_but_does_not_erase_location_penalty():
    """A confirmed sweep + reclaim + confirmation is a VALID reversal setup
    at the extreme — its location penalty is halved, not removed."""
    no_sweep = _sell(htf_snap=_htf(range_pos=0.05))
    swept = _sell(htf_snap=_htf(range_pos=0.05),
                 liquidity={"short_ready": True, "long_ready": False,
                            "buy_sweep": None, "sell_sweep": {"side": "buy-side"},
                            "equal_lows": [], "equal_highs": []})
    assert no_sweep["quality"] < swept["quality"] < _sell()["quality"]
    assert any("sweep halves" in p for p in swept["penalties"])


# ------------------------------------------------------ RSI exhaustion (req 4)

def test_oversold_short_penalized():
    fresh = _sell(structure=STRUCT_BEAR_EARLY, htf_snap=_htf(range_pos=0.90, rsi=50))
    exhausted = _sell(structure=STRUCT_BEAR_EARLY, htf_snap=_htf(range_pos=0.90, rsi=25))
    assert exhausted["quality"] < fresh["quality"]
    assert any("oversold" in p for p in exhausted["penalties"])


def test_overbought_long_penalized():
    fresh = _buy(structure=STRUCT_BULL_EARLY, htf_snap=_htf(range_pos=0.10, rsi=50))
    exhausted = _buy(structure=STRUCT_BULL_EARLY, htf_snap=_htf(range_pos=0.10, rsi=78))
    assert exhausted["quality"] < fresh["quality"]
    assert any("overbought" in p for p in exhausted["penalties"])


def test_oversold_short_survives_strong_continuation():
    """RSI 26 with a fresh BOS *and* displacement is a genuine breakdown,
    not exhaustion — the RSI penalty is exempt."""
    factor, reasons = _exhaustion_factor("SHORT", _htf(range_pos=0.90, rsi=26),
                                         LIQ_NONE, STRUCT_BEAR)
    assert factor == 1.0 and reasons == []


# --------------------------------------------------------- Bollinger (req 5)

def test_below_lower_band_reduces_short_quality():
    inside = _sell(structure=STRUCT_BEAR_EARLY,
                   htf_snap=_htf(range_pos=0.90, close=100, bb_lower=90))
    stretched = _sell(structure=STRUCT_BEAR_EARLY,
                      htf_snap=_htf(range_pos=0.90, close=89, bb_lower=90))
    assert stretched["quality"] < inside["quality"]
    assert any("lower Bollinger" in p for p in stretched["penalties"])


def test_above_upper_band_reduces_long_quality():
    inside = _buy(structure=STRUCT_BULL_EARLY,
                  htf_snap=_htf(range_pos=0.10, close=100, bb_upper=110))
    stretched = _buy(structure=STRUCT_BULL_EARLY,
                     htf_snap=_htf(range_pos=0.10, close=111, bb_upper=110))
    assert stretched["quality"] < inside["quality"]
    assert any("upper Bollinger" in p for p in stretched["penalties"])


# ------------------------------------------------------------ volume (req 6)

def test_weak_volume_penalized():
    strong = _sell(htf_snap=_htf(range_pos=0.90, volume=1500, vol_prev=1000))
    weak = _sell(htf_snap=_htf(range_pos=0.90, volume=500, vol_prev=1000))
    assert weak["quality"] < strong["quality"]
    assert any("weak 1H volume" in p for p in weak["penalties"])


def test_declining_volume_penalized():
    rising = _sell(htf_snap=_htf(range_pos=0.90, volume=1100, vol_prev=1000))
    declining = _sell(htf_snap=_htf(range_pos=0.90, volume=950, vol_prev=1000))
    assert declining["quality"] < rising["quality"]
    assert any("declining 1H volume" in p for p in declining["penalties"])


# -------------------------------------------------------- RR into score (req 8)

def test_insufficient_rr_penalized():
    good_rr = _sell(risk=_risk(2.5))
    poor_rr = _sell(risk=_risk(1.0))
    no_target = _sell(risk=_risk(None))
    assert poor_rr["quality"] < good_rr["quality"]
    assert no_target["quality"] < poor_rr["quality"]      # no target is worst
    assert any("RR=1.00 below" in p for p in poor_rr["penalties"])
    assert any("no achievable target" in p for p in no_target["penalties"])


def _rr_points(rr):
    pts, _ = _rr_penalty(_risk(rr))
    return pts


def test_rr_penalty_scales_with_miss():
    assert _rr_points(1.4) < _rr_points(0.5)
    assert _rr_points(2.0) == 0.0                          # at/above MIN_RR: free


# --------------------------------------------- bias/trend contradiction (req 2)

def test_buy_bias_against_downtrend_is_flagged_and_penalized():
    """`BUY bias=bullish trend=downtrend` = fresh CHoCH in a downtrend. A
    valid reversal trigger, but reduced conviction until the trend confirms."""
    out = mtf.combine({"bias": "bullish", "trend": "downtrend"},
                      {"bias": "bullish", "trend": "downtrend"},
                      {"bias": "bullish", "trend": "downtrend"})
    assert out["direction"] == "LONG"
    assert out["choch_reversal"] is True
    assert out["penalty"] >= config.MTF_TREND_CONFLICT_PENALTY
    assert any("CHoCH" in n for n in out["notes"])


def test_sell_bias_against_uptrend_is_flagged_and_penalized():
    out = mtf.combine({"bias": "bearish", "trend": "uptrend"},
                      {"bias": "bearish", "trend": "uptrend"},
                      {"bias": "bearish", "trend": "uptrend"})
    assert out["direction"] == "SHORT"
    assert out["choch_reversal"] is True
    assert out["penalty"] >= config.MTF_TREND_CONFLICT_PENALTY


def test_choch_reversal_cuts_mtf_support_and_structure_base():
    # MTF: aligned-but-choch-driven reads 0.5, not 1.0
    assert sq._mtf_support({"aligned": True, "counter_htf": False,
                            "choch_reversal": True, "neutral_htf": False}) == 0.5
    # structure: bias-with-broken-trend starts at 0.35, not 0.5
    choch_struct = {"bias": "bullish", "trend": "downtrend", "bos": None,
                    "choch": {"dir": "bullish"}, "displacement": None, "retest": None}
    trend_struct = {"bias": "bullish", "trend": "uptrend", "bos": None,
                    "choch": {"dir": "bullish"}, "displacement": None, "retest": None}
    assert _structure_support(choch_struct, "bullish") < _structure_support(trend_struct, "bullish")


def test_no_conflict_when_trend_follows_bias():
    out = mtf.combine({"bias": "bearish", "trend": "downtrend"},
                      {"bias": "bearish", "trend": "downtrend"},
                      {"bias": "bearish", "trend": "downtrend"})
    assert out["choch_reversal"] is False
    assert out["penalty"] == 0


# ------------------------------------------------- valid setups still pass (req 9)

def test_valid_continuation_still_scores_high():
    """A fresh short from the range top: good location, mid RSI, inside the
    bands, strong volume, 2.5R available — must clear every gate untouched."""
    out = _sell(htf_snap=_htf(range_pos=0.90, rsi=55, close=100,
                              bb_lower=90, bb_upper=110,
                              volume=1500, vol_prev=1000))
    assert out["penalties"] == []
    assert out["exhaustion_factor"] == 1.0
    assert out["primary_floor_ok"] is True
    assert out["passes"] is True


def test_missing_htf_snapshot_degrades_neutrally():
    out = _sell(htf_snap=None)
    assert out["exhaustion_factor"] == 1.0
    assert out["penalties"] == []          # no data -> no fabricated penalties


# --------------------------------------- decision-core integration (PENGU shape)

def _candles(controls, **kw):
    from conftest import make_candles
    import numpy as np
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, 5, endpoint=False))
    closes.append(controls[-1])
    return make_candles(closes, wick=kw.pop("wick", 0.3), **kw)


def test_decision_rejects_exhausted_short_at_the_1h_low():
    """End-to-end PENGU/ONDO/XAUT shape: a real downtrend whose last candle
    closes at the absolute range low. Structure says SELL, the score must say
    'nothing left to short'."""
    import decision
    df = _candles([140, 135, 138, 130, 133, 128, 131, 126, 122, 116, 110, 104])
    frames = {config.TF_HTF: df, config.TF_SETUP: df, config.TF_ENTRY: df}
    d = decision.decide(frames, funding_rate=-0.0001, symbol="TEST/USDT:USDT")
    assert d["direction"] == "SHORT"                    # structure did its job...
    assert d["decision"] == "NO_TRADE"                  # ...but it is not tradable
    penalties = d["quality"]["penalties"]
    assert any("range_pos" in p for p in penalties)     # location was the tell
    assert d["setup_quality"] < config.QUALITY_MIN


def test_decision_still_takes_a_valid_downtrend_short():
    """Mirror of the PENGU test with room left: the same downtrend pulling
    back mid-range before the last leg down stays a tradable SHORT."""
    import decision
    df = _candles([100, 104, 99, 129, 123, 126, 120, 123, 118, 119, 117.5])
    frames = {config.TF_HTF: df, config.TF_SETUP: df, config.TF_ENTRY: df}
    d = decision.decide(frames, funding_rate=-0.0001, symbol="TEST/USDT:USDT")
    assert d["decision"] == "SHORT"
    assert d["no_trade_reasons"] == []
