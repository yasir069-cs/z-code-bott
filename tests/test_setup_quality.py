"""Deterministic tests for setup_quality and the secondary indicator layer."""
import config
import scoring
import setup_quality as sq


# ------------------------------------------------- favorable LONG evidence

STRUCT_BULL = {"bias": "bullish", "bos": {"dir": "bullish"}, "choch": None,
               "displacement": {"dir": "bullish"}, "retest": None}
SR_BOUNCE = {"at_zone": {"side": "support", "major": True},
             "nearest_resistance": {"lo": 120.0, "hi": 122.0},
             "nearest_support": {"lo": 98.0, "hi": 100.0}}
LIQ_READY = {"long_ready": True, "short_ready": False,
             "buy_sweep": {"side": "sell-side"}, "sell_sweep": None,
             "equal_lows": [{"level": 100.0, "count": 2}], "equal_highs": []}
PA_BULL = {"signals": {"bullish": 3, "bearish": 0}, "breakout": None, "absorption": False}
MTF_ALIGNED = {"aligned": True, "counter_htf": False, "neutral_htf": False}
TL_BULL = {"break": {"dir": "bullish"}, "support_line": None,
           "resistance_line": None, "channel": False}
FUT_BULL = {"available": True, "bias": "bullish", "conviction": "high"}


def _score(indicator_net, **overrides):
    args = dict(structure=STRUCT_BULL, sr=SR_BOUNCE, liquidity=LIQ_READY,
                price_action=PA_BULL, mtf=MTF_ALIGNED, trendlines=TL_BULL,
                futures=FUT_BULL, indicator_conf={"score": indicator_net})
    args.update(overrides)
    return sq.score("LONG", **args)


def test_strong_primary_passes():
    out = _score(0.5)
    assert out["primary"] >= config.QUALITY_PRIMARY_FLOOR
    assert out["passes"] is True
    assert out["quality"] > out["primary"]        # positive indicators add a bounded bonus


def test_indicators_cannot_rescue_weak_primary():
    weak_struct = {"bias": "neutral"}
    weak_sr = {"at_zone": None, "nearest_resistance": None, "nearest_support": None}
    weak_liq = {"long_ready": False, "short_ready": False, "buy_sweep": None,
                "sell_sweep": None, "equal_lows": [], "equal_highs": []}
    weak_pa = {"signals": {"bullish": 0, "bearish": 0}, "breakout": None, "absorption": False}
    neutral_mtf = {"aligned": False, "counter_htf": False, "neutral_htf": True}
    no_tl = {"break": None, "support_line": None, "resistance_line": None, "channel": False}
    no_fut = {"available": False}
    out = _score(1.0, structure=weak_struct, sr=weak_sr, liquidity=weak_liq,
                 price_action=weak_pa, mtf=neutral_mtf, trendlines=no_tl, futures=no_fut)
    assert out["primary"] < config.QUALITY_PRIMARY_FLOOR
    assert out["primary_floor_ok"] is False
    assert out["passes"] is False                 # max indicator agreement still can't pass


def test_indicator_conflict_drags_quality_down():
    strong = _score(0.0)
    conflicted = _score(-1.0)
    assert conflicted["quality"] < strong["quality"]
    assert conflicted["indicator"] < 0


def test_opposing_structure_scores_low_on_structure():
    bear_struct = {"bias": "bearish", "bos": {"dir": "bearish"}, "choch": None,
                   "displacement": None, "retest": None}
    out = _score(0.0, structure=bear_struct)
    assert out["fractions"]["structure"] == 0.0


# ------------------------------------------------- indicator_confirmation

def _snap(close, ema, vwap, rsi, rsi_prev, bb_lower, bb_mid, bb_upper, vol, vol_prev):
    return {"close": close, "ema21": ema, "vwap": vwap, "rsi": rsi,
            "rsi_prev": rsi_prev, "bb_lower": bb_lower, "bb_mid": bb_mid,
            "bb_upper": bb_upper, "volume": vol, "volume_prev": vol_prev}


def test_indicator_confirmation_agrees_with_bullish():
    snap = _snap(105, 100, 101, 58, 55, 95, 106, 118, 1200, 1000)
    out = scoring.indicator_confirmation(snap, "LONG")
    assert out["score"] > 0 and out["agrees"] is True


def test_indicator_confirmation_conflicts_when_price_against_direction():
    # long proposed but price below EMA/VWAP, RSI falling and overbought-side low
    snap = _snap(95, 100, 101, 42, 45, 90, 100, 112, 900, 1000)
    out = scoring.indicator_confirmation(snap, "LONG")
    assert out["score"] < 0 and out["agrees"] is False


def test_indicator_confirmation_no_snapshot_is_zero():
    out = scoring.indicator_confirmation(None, "LONG")
    assert out["score"] == 0.0 and out["agrees"] is False
