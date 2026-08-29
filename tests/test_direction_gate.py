"""Directional-confirmation gate: a structural bias alone is not a direction.

The SPCX shape: structure proposed SELL while price sat above EMA21/VWAP on
every timeframe with 5M RSI 63.9 rising. These tests pin the gate that
catches that contradiction BEFORE the setup is scored or ranked.
"""
import config
import decision
import direction_gate as dg
import setup_quality as sq


# --------------------------------------------------------------- snapshots

def _snap(close=100.0, ema=99.0, vwap=99.5, rsi=55.0, rsi_prev=52.0):
    return {"close": close, "ema21": ema, "vwap": vwap, "rsi": rsi, "rsi_prev": rsi_prev}


BULL_POS = _snap(close=103, ema=99, vwap=99.5, rsi=64, rsi_prev=58)   # above EMA+VWAP, rising
BEAR_POS = _snap(close=97, ema=99, vwap=99.5, rsi=38, rsi_prev=44)    # below both, falling
# mixed positioning (between EMA and VWAP) with rising bullish RSI — SPCX 5M shape
MIXED_RISING = _snap(close=100, ema=99, vwap=100.5, rsi=64, rsi_prev=58)

STRUCT_BEAR_CONFIRMED = {"bias": "bearish", "trend": "downtrend",
                         "bos": {"dir": "bearish"}, "choch": None,
                         "displacement": {"dir": "bearish"}, "retest": None}
STRUCT_BULL_CONFIRMED = {"bias": "bullish", "trend": "uptrend",
                         "bos": {"dir": "bullish"}, "choch": None,
                         "displacement": {"dir": "bullish"}, "retest": None}
STRUCT_BEAR_BIAS_ONLY = {"bias": "bearish", "trend": "downtrend",
                         "bos": None, "choch": None, "displacement": None, "retest": None}
STRUCT_BULL_BIAS_ONLY = {"bias": "bullish", "trend": "uptrend",
                         "bos": None, "choch": None, "displacement": None, "retest": None}
LIQ_NONE = {"long_ready": False, "short_ready": False, "buy_sweep": None,
            "sell_sweep": None, "equal_lows": [], "equal_highs": []}
MTF_PLAIN = {"choch_reversal": False}
MTF_CHOCH = {"choch_reversal": True}


def _confirm(direction, setup, entry, structure, mtf=MTF_PLAIN, htf=None, liq=LIQ_NONE):
    return dg.confirm(direction, htf, setup, entry, structure, liq, mtf)


# ------------------------------------------------- positioning conflicts (req 1/2/6/7)

def test_sell_with_bullish_ema_vwap_on_both_tfs_is_conflict():
    """The SPCX shape: SHORT proposed, price above EMA21/VWAP on 15M AND 5M,
    rising RSI, and structure offers no confirming event."""
    out = _confirm("SHORT", BULL_POS, BULL_POS, STRUCT_BEAR_BIAS_ONLY)
    assert out["status"] == "conflict"
    assert out["factor"] == config.DIR_CONFLICT_FACTOR


def test_buy_with_bearish_ema_vwap_on_both_tfs_is_conflict():
    out = _confirm("LONG", BEAR_POS, BEAR_POS, STRUCT_BULL_BIAS_ONLY)
    assert out["status"] == "conflict"
    assert out["factor"] == config.DIR_CONFLICT_FACTOR


def test_confirmed_structure_downgrades_conflict_to_weak_not_free():
    """BOS+displacement genuinely confirm the short, but a price position
    being fought is still a weak entry — never full quality."""
    out = _confirm("SHORT", BULL_POS, BULL_POS, STRUCT_BEAR_CONFIRMED)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_CONFLICT_LATE_FACTOR


def test_sell_bullish_positioning_without_momentum_still_penalized():
    out = _confirm("SHORT", BULL_POS, BULL_POS, STRUCT_BEAR_BIAS_ONLY)  # has momentum too
    with_mom = out["factor"]
    # entry momentum neutral (falling from 64) but positioning still bullish both TFs
    entry_neutral = _snap(close=103, ema=99, vwap=99.5, rsi=62, rsi_prev=64)
    out2 = _confirm("SHORT", BULL_POS, entry_neutral, STRUCT_BEAR_BIAS_ONLY)
    assert out2["status"] == "weak"
    assert out2["factor"] == config.DIR_POSITIONING_CONFLICT_FACTOR
    assert out2["factor"] > with_mom or with_mom == config.DIR_CONFLICT_FACTOR


# ------------------------------------------------------ momentum conflicts (req 5/6)

def test_sell_with_rising_bullish_momentum_and_no_confirmation_penalized():
    """Mixed positioning but RSI 64 rising against a bias-only short."""
    out = _confirm("SHORT", MIXED_RISING, MIXED_RISING, STRUCT_BEAR_BIAS_ONLY)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_MOMENTUM_CONFLICT_FACTOR
    assert any("momentum" in r for r in out["reasons"])


def test_buy_with_falling_bearish_momentum_and_no_confirmation_penalized():
    mixed_falling = _snap(close=100, ema=101, vwap=99.5, rsi=36, rsi_prev=42)
    out = _confirm("LONG", mixed_falling, mixed_falling, STRUCT_BULL_BIAS_ONLY)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_MOMENTUM_CONFLICT_FACTOR


def test_momentum_conflict_is_mild_when_structure_confirmed():
    out = _confirm("SHORT", MIXED_RISING, MIXED_RISING, STRUCT_BEAR_CONFIRMED)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR


def test_single_indicator_is_never_sufficient():
    """Price above EMA but below VWAP (mixed) with neutral RSI neither
    conflicts nor confirms — direction stands or falls on structure alone."""
    mixed_neutral = _snap(close=100, ema=99, vwap=100.5, rsi=50, rsi_prev=50)
    out = _confirm("SHORT", mixed_neutral, mixed_neutral, STRUCT_BEAR_BIAS_ONLY)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0


# ----------------------------------------------------- valid continuations (req 4)

def test_valid_bearish_continuation_unchanged():
    out = _confirm("SHORT", BEAR_POS, BEAR_POS, STRUCT_BEAR_CONFIRMED)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0
    assert out["reasons"] == []


def test_valid_bullish_continuation_unchanged():
    out = _confirm("LONG", BULL_POS, BULL_POS, STRUCT_BULL_CONFIRMED)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0
    assert out["reasons"] == []


# ------------------------------------------------------------- CHoCH (req 8)

def test_choch_without_confirmation_is_penalized():
    """Direction driven by a fresh CHoCH against the standing trend, with no
    displacement / retest / positioning flip after the break."""
    mixed_neutral = _snap(close=100, ema=99, vwap=100.5, rsi=50, rsi_prev=50)
    choch_struct = {"bias": "bearish", "trend": "uptrend", "bos": None,
                    "choch": {"dir": "bearish"}, "displacement": None, "retest": None}
    out = _confirm("SHORT", mixed_neutral, mixed_neutral, choch_struct, mtf=MTF_CHOCH)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_UNCONFIRMED_CHOCH_FACTOR


def test_choch_with_confirmation_can_qualify():
    """Same CHoCH but WITH a retest + displacement after the break: the
    reversal is confirmed and the gate does not add its own penalty."""
    mixed_neutral = _snap(close=100, ema=99, vwap=100.5, rsi=50, rsi_prev=50)
    choch_struct = {"bias": "bearish", "trend": "uptrend", "bos": None,
                    "choch": {"dir": "bearish"},
                    "displacement": {"dir": "bearish"},
                    "retest": {"dir": "bearish"}}
    out = _confirm("SHORT", mixed_neutral, mixed_neutral, choch_struct, mtf=MTF_CHOCH)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0


def test_choch_confirmed_by_positioning_flip():
    """A CHoCH whose direction price has actually flipped to (both tradeable
    TFs on the CHoCH's side of EMA/VWAP) counts as confirmed."""
    choch_struct = {"bias": "bearish", "trend": "uptrend", "bos": None,
                    "choch": {"dir": "bearish"}, "displacement": None, "retest": None}
    out = _confirm("SHORT", BEAR_POS, BEAR_POS, choch_struct, mtf=MTF_CHOCH)
    assert out["factor"] == 1.0


# ----------------------------------------------- scoring integration (req 3/9)

SR_SHORT = {"at_zone": {"side": "resistance", "major": True},
            "nearest_support": {"lo": 80.0, "hi": 82.0},
            "nearest_resistance": {"lo": 120.0, "hi": 122.0}}
PA_BEAR = {"signals": {"bullish": 0, "bearish": 3}, "breakout": None, "absorption": False}
MTF_ALIGNED = {"aligned": True, "counter_htf": False, "choch_reversal": False,
               "neutral_htf": False}
TL_BEAR = {"break": {"dir": "bearish"}, "support_line": None,
           "resistance_line": None, "channel": False}
FUT_BEAR = {"available": True, "bias": "bearish", "conviction": "high"}


def test_direction_factor_drives_quality_below_floor():
    """The gate's factor multiplies the primary score, so a conflicted
    direction cannot rank top-3 or pass gates even with perfect evidence."""
    clean = sq.score("SHORT", STRUCT_BEAR_CONFIRMED, SR_SHORT, LIQ_NONE, PA_BEAR,
                     MTF_ALIGNED, TL_BEAR, FUT_BEAR,
                     indicator_conf={"score": 0.5},
                     htf_snap={"range_pos": 0.90, "rsi": 55, "close": 100,
                               "bb_lower": 90, "bb_upper": 110,
                               "volume": 1200, "volume_prev": 1000,
                               "volume_avg20": 1000},
                     risk={"rr": 2.5, "ok": True, "reasons": []})
    conflicted = sq.score("SHORT", STRUCT_BEAR_CONFIRMED, SR_SHORT, LIQ_NONE, PA_BEAR,
                          MTF_ALIGNED, TL_BEAR, FUT_BEAR,
                          indicator_conf={"score": 0.5},
                          htf_snap={"range_pos": 0.90, "rsi": 55, "close": 100,
                                    "bb_lower": 90, "bb_upper": 110,
                                    "volume": 1200, "volume_prev": 1000,
                                    "volume_avg20": 1000},
                          risk={"rr": 2.5, "ok": True, "reasons": []},
                          direction_factor=0.30,
                          direction_reasons=["bull positioning contradicts SHORT"])
    assert conflicted["quality"] < config.QUALITY_PRIMARY_FLOOR < clean["quality"]
    assert conflicted["passes"] is False
    assert any("bull positioning" in p for p in conflicted["penalties"])
    assert conflicted["direction_factor"] == 0.30


# ------------------------------------------------- decision-core wiring (req 9)

def _candles(controls, **kw):
    from conftest import make_candles
    import numpy as np
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, 5, endpoint=False))
    closes.append(controls[-1])
    return make_candles(closes, wick=kw.pop("wick", 0.3), **kw)


UPTREND = [130, 126, 131, 101, 107, 104, 110, 107, 112, 111, 112.5]


def test_decision_wires_conflict_to_no_trade_and_low_quality(monkeypatch):
    """A conflict verdict from the gate must surface as its own NO_TRADE
    reason and drag the quality score (so --force-llm ranking skips it)."""
    conflict = {"status": "conflict", "factor": config.DIR_CONFLICT_FACTOR,
                "reasons": ["price on the bull side of EMA21/VWAP on BOTH tradeable TFs"],
                "setup_positioning": "bull", "entry_positioning": "bull",
                "entry_momentum": "bull", "structure_confirmed": False,
                "full_confirmation": False}
    monkeypatch.setattr(decision.direction_gate, "confirm", lambda *a, **k: conflict)
    df = _candles(UPTREND)
    frames = {config.TF_HTF: df, config.TF_SETUP: df, config.TF_ENTRY: df}
    d = decision.decide(frames, funding_rate=0.0001, symbol="TEST/USDT:USDT")
    assert d["decision"] == "NO_TRADE"
    assert "directional_conflict" in d["no_trade_reasons"]
    assert d["direction_check"]["status"] == "conflict"
    assert d["setup_quality"] < config.QUALITY_MIN
    assert d["quality"]["direction_factor"] == config.DIR_CONFLICT_FACTOR


def test_decision_valid_continuation_has_no_direction_reason():
    """The gate must stay silent on a clean continuation (no false conflicts):
    the existing decision-core scenario suite pins LONG here with no reasons."""
    df = _candles(UPTREND)
    frames = {config.TF_HTF: df, config.TF_SETUP: df, config.TF_ENTRY: df}
    d = decision.decide(frames, funding_rate=0.0001, symbol="TEST/USDT:USDT")
    assert d["decision"] == "LONG"
    assert d["direction_check"]["status"] == "confirmed"
    assert d["direction_check"]["factor"] == 1.0


def test_missing_snapshots_degrade_neutrally():
    out = _confirm("SHORT", None, None, STRUCT_BEAR_CONFIRMED)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0
    assert out["setup_positioning"] == "n/a"


# ------------------------------------------------------- HTF tape check

def test_long_while_1h_tape_bearish_without_confirmation_penalized():
    """The LIGHT/NVDA shape: 15M/5M look bullish but the 1H tape is still on
    the bear side of EMA21/VWAP — an early counter-HTF bounce with no
    confirming event."""
    out = _confirm("LONG", BULL_POS, BULL_POS, STRUCT_BULL_BIAS_ONLY, htf=BEAR_POS)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_POSITIONING_CONFLICT_FACTOR
    assert any("1H tape" in r for r in out["reasons"])


def test_short_while_1h_tape_bullish_penalized():
    out = _confirm("SHORT", BEAR_POS, BEAR_POS, STRUCT_BEAR_BIAS_ONLY, htf=BULL_POS)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_POSITIONING_CONFLICT_FACTOR


def test_htf_tape_conflict_mild_with_confirmation():
    out = _confirm("LONG", BULL_POS, BULL_POS, STRUCT_BULL_CONFIRMED, htf=BEAR_POS)
    assert out["status"] == "weak"
    assert out["factor"] == config.DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR


def test_htf_tape_agreeing_adds_nothing():
    out = _confirm("SHORT", BEAR_POS, BEAR_POS, STRUCT_BEAR_CONFIRMED, htf=BEAR_POS)
    assert out["status"] == "confirmed"
    assert out["factor"] == 1.0
    assert out["reasons"] == []
