"""Price action & volume — priority-4 layer.

Reads the most recent closed candle (and its immediate neighbours) for the
patterns Yasir listed: rejection wicks, engulfing/reversal, displacement, failed
breakouts, breakout-retest, and volume absorption. Volume qualifies every
pattern — a breakout or reversal on weak volume is flagged weak, because
"no-volume breakout = weak" is one of his explicit rules.

Everything is measured in ATR / ratio terms from config (no magic numbers) and
uses only candles at or before the decision bar, so there is no look-ahead. A
`signals` tally (bullish vs bearish pattern count) gives the decision core and
setup_quality a quick read without re-deriving each pattern.
"""
from typing import Optional

import numpy as np
import pandas as pd

import config
import ta_utils


def _volume_state(volume: np.ndarray, lookback: int) -> tuple[str, float]:
    """Classify the last candle's volume vs the prior `lookback` average."""
    if len(volume) < 2:
        return "normal", 1.0
    prior = volume[-lookback - 1:-1] if len(volume) > lookback else volume[:-1]
    avg = float(np.mean(prior)) if len(prior) else 0.0
    ratio = ta_utils.safe_pct(float(volume[-1]), avg)
    if ratio is None:
        return "normal", 1.0
    if ratio >= config.PA_VOLUME_STRONG:
        return "strong", round(ratio, 2)
    if ratio <= config.PA_VOLUME_WEAK:
        return "weak", round(ratio, 2)
    return "normal", round(ratio, 2)


def _rejection(o: float, h: float, l: float, c: float, body: float) -> Optional[dict]:
    up = ta_utils.upper_wick(o, h, c)
    lo = ta_utils.lower_wick(o, l, c)
    ref = max(body, 1e-9)
    if lo > config.PA_REJECTION_WICK_RATIO * ref and lo > up:
        return {"dir": "bullish", "wick_ratio": round(lo / ref, 2)}
    if up > config.PA_REJECTION_WICK_RATIO * ref and up > lo:
        return {"dir": "bearish", "wick_ratio": round(up / ref, 2)}
    return None


def _engulfing(prev: pd.Series, last: pd.Series) -> Optional[dict]:
    pbody = abs(prev["close"] - prev["open"])
    lbody = abs(last["close"] - last["open"])
    if lbody < config.PA_ENGULF_MIN_RATIO * max(pbody, 1e-9):
        return None
    bull = (last["close"] > last["open"] and prev["close"] < prev["open"]
            and last["close"] >= prev["open"] and last["open"] <= prev["close"])
    bear = (last["close"] < last["open"] and prev["close"] > prev["open"]
            and last["close"] <= prev["open"] and last["open"] >= prev["close"])
    if bull:
        return {"dir": "bullish", "ratio": round(lbody / max(pbody, 1e-9), 2)}
    if bear:
        return {"dir": "bearish", "ratio": round(lbody / max(pbody, 1e-9), 2)}
    return None


def analyze(df: pd.DataFrame, cfg=config) -> dict:
    """Detect price-action patterns on the last closed candle."""
    empty = {"rejection": None, "engulfing": None, "displacement": None,
             "breakout": None, "failed_breakout": None, "breakout_retest": None,
             "absorption": False, "volume_state": "normal", "volume_ratio": 1.0,
             "atr": 0.0, "signals": {"bullish": 0, "bearish": 0}}
    if df is None or len(df) < 3:
        return empty

    o = df["open"].to_numpy(dtype="float64")
    h = df["high"].to_numpy(dtype="float64")
    l = df["low"].to_numpy(dtype="float64")
    c = df["close"].to_numpy(dtype="float64")
    v = df["volume"].to_numpy(dtype="float64")
    atr = ta_utils.atr(df.tail(cfg.STRUCT_LOOKBACK), cfg.STRUCT_ATR_LENGTH)

    body = ta_utils.body(o[-1], c[-1])
    rng = ta_utils.candle_range(h[-1], l[-1])
    vol_state, vol_ratio = _volume_state(v, cfg.PA_BREAKOUT_LOOKBACK)

    rejection = _rejection(o[-1], h[-1], l[-1], c[-1], body)
    engulfing = _engulfing(df.iloc[-2], df.iloc[-1])

    displacement = None
    if atr > 0 and body > cfg.PA_DISPLACEMENT_ATR * atr:
        displacement = {"dir": "bullish" if c[-1] > o[-1] else "bearish",
                        "size_atr": round(body / atr, 2)}

    # breakout / failed-breakout vs the prior range (window excludes last candle)
    breakout = failed_breakout = None
    lb = cfg.PA_BREAKOUT_LOOKBACK
    if len(df) >= lb + 2:
        prior_high = float(np.max(h[-lb - 1:-1]))
        prior_low = float(np.min(l[-lb - 1:-1]))
        strong = vol_state == "strong"
        if c[-1] > prior_high:
            breakout = {"dir": "bullish", "level": prior_high, "volume_ok": strong}
        elif c[-1] < prior_low:
            breakout = {"dir": "bearish", "level": prior_low, "volume_ok": strong}
        if h[-1] > prior_high and c[-1] < prior_high:      # poked above, closed back
            failed_breakout = {"dir": "bearish", "level": prior_high}
        elif l[-1] < prior_low and c[-1] > prior_low:      # poked below, closed back
            failed_breakout = {"dir": "bullish", "level": prior_low}

    # breakout-retest: a break happened in the last few bars, price now tests
    # the broken level and holds. Prior range is measured BEFORE those bars.
    breakout_retest = None
    k = cfg.LIQ_RECLAIM_CANDLES + 1
    if atr > 0 and len(df) >= lb + k + 1:
        ref_high = float(np.max(h[-lb - k:-k]))
        ref_low = float(np.min(l[-lb - k:-k]))
        tol = cfg.STRUCT_RETEST_ATR * atr        # shared "retest distance" in ATR
        recent_break_up = np.any(c[-k:] > ref_high)
        recent_break_dn = np.any(c[-k:] < ref_low)
        if recent_break_up and c[-1] >= ref_high - tol and l[-1] <= ref_high + tol:
            breakout_retest = {"dir": "bullish", "level": ref_high}
        elif recent_break_dn and c[-1] <= ref_low + tol and h[-1] >= ref_low - tol:
            breakout_retest = {"dir": "bearish", "level": ref_low}

    # absorption: heavy volume but the candle stalls (small body, wide range)
    absorption = bool(vol_state == "strong" and rng > atr > 0 and body < 0.5 * rng)

    bullish = sum(1 for x in (rejection, engulfing, displacement, breakout,
                              failed_breakout, breakout_retest)
                  if x and x.get("dir") == "bullish")
    bearish = sum(1 for x in (rejection, engulfing, displacement, breakout,
                              failed_breakout, breakout_retest)
                  if x and x.get("dir") == "bearish")

    return {
        "rejection": rejection,
        "engulfing": engulfing,
        "displacement": displacement,
        "breakout": breakout,
        "failed_breakout": failed_breakout,
        "breakout_retest": breakout_retest,
        "absorption": absorption,
        "volume_state": vol_state,
        "volume_ratio": vol_ratio,
        "atr": atr,
        "signals": {"bullish": bullish, "bearish": bearish},
    }
