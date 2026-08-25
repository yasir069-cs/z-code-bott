"""Phase 3 + Phase 4 — Liquidation Sweep detection and 1H context filter.

Liquidation sweep (all 4 conditions must hold, rules.md):
  BULLISH (BUY): lower wick pierces below recent swing low (last 20 candles),
                 body closes back ABOVE that swing low, lower wick > 2x body,
                 volume > SWEEP_VOL_RATIO x avg volume of the last 20 candles.
  BEARISH (SELL): upper wick pierces above recent swing high, body closes
                 back BELOW it, upper wick > 2x body, volume spike.

1H context implements the handwritten note (strategy_spec.md): the coin must
be in the favourable half of its range ("Bottom to inbetween" for BUY, "Top to
inbetween" for SELL), with RSI moving through its band, price on the correct
side of EMA21 and VWAP, rising volume, and Bollinger proximity — graded into a
0-100 score by scoring.py rather than judged pass/fail.

Context fail -> the coin is skipped before 15M/5M are ever fetched.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
import scoring
from indicators import compute_indicators

log = logging.getLogger("filter_1h")


def detect_sweep(df: pd.DataFrame, direction: str) -> Optional[dict]:
    """Find the most recent valid liquidation sweep (direction: 'BUY'|'SELL').

    Searches the last SWEEP_SEARCH_CANDLES closed candles; the swing level
    for each candidate is the min low / max high of the SWEEP_WINDOW candles
    immediately before it. Returns None when no valid sweep exists.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    n = len(df)
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    open_ = df["open"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    volume = df["volume"].to_numpy(dtype="float64")

    for pos_from_end in range(1, config.SWEEP_SEARCH_CANDLES + 1):
        p = n - pos_from_end
        if p - config.SWEEP_WINDOW < 0:
            break
        body = abs(close[p] - open_[p])
        if direction == "BUY":
            swing = float(np.min(low[p - config.SWEEP_WINDOW:p]))
            wick = min(open_[p], close[p]) - low[p]
            pierced = low[p] < swing
            closed_back = close[p] > swing
        else:
            swing = float(np.max(high[p - config.SWEEP_WINDOW:p]))
            wick = high[p] - max(open_[p], close[p])
            pierced = high[p] > swing
            closed_back = close[p] < swing
        vol_avg = float(np.mean(volume[p - config.SWEEP_WINDOW:p]))
        wick_ok = wick > config.SWEEP_WICK_BODY_RATIO * body
        volume_ok = volume[p] > config.SWEEP_VOL_RATIO * vol_avg and vol_avg > 0

        if pierced and closed_back and wick_ok and volume_ok:
            return {
                "direction": direction,
                "age_candles": pos_from_end - 1,   # 0 = last closed candle
                "timestamp": df.index[p],
                "level": swing,
                "wick": float(wick),
                "body": float(body),
                "wick_body_ratio": float(wick / body) if body > 0 else float("inf"),
                "volume_ratio": float(volume[p] / vol_avg) if vol_avg > 0 else float("inf"),
                "candle_low": float(low[p]),
                "candle_high": float(high[p]),
            }
    return None


def _near_lower_band(snap: dict, near_pct: float) -> bool:
    """Candle touched / is near the lower band: its range overlaps the
    band's `near_pct` neighborhood (excludes being far BELOW the band too)."""
    return (snap["low"] <= snap["bb_lower"] * (1 + near_pct)
            and snap["high"] >= snap["bb_lower"] * (1 - near_pct))


def _near_upper_band(snap: dict, near_pct: float) -> bool:
    return (snap["high"] >= snap["bb_upper"] * (1 - near_pct)
            and snap["low"] <= snap["bb_upper"] * (1 + near_pct))


def analyze_1h(df: pd.DataFrame) -> Optional[dict]:
    """Classify 1H context. Returns a context dict when the coin passes
    (direction BUY or SELL), otherwise None (skip the coin entirely).

    Delegates all grading to scoring.score_1h(). Hard gates (EMA21 side,
    VWAP side, RSI band, overbought/oversold, BB squeeze, and the zone being
    in the favourable half of the range) drop the coin. Everything else is
    graded into a 0-100 score which must clear config.MIN_SCORE_1H.

    The zone check is the note's "Bottom to inbetween" requirement, which the
    previous version omitted entirely.
    """
    snap = compute_indicators(df)
    if snap is None:
        return None

    rejections: dict[str, str] = {}
    for direction in ("BUY", "SELL"):
        # Sweep is graded (heavy weight) but never gates: detect it first so
        # its score is available, and so the AI bundle always carries it.
        sweep = detect_sweep(df, direction)
        try:
            scored = scoring.score_1h(snap, sweep, direction)
        except scoring.Rejected as rej:
            rejections[direction] = str(rej)
            continue

        if scored["score"] < config.MIN_SCORE_1H:
            rejections[direction] = (f"score {scored['score']:.1f} < {config.MIN_SCORE_1H} "
                                     f"({scored['breakdown']})")
            continue

        if sweep is not None:
            log.info("1H %s PASSED score=%.1f %s | sweep age=%d wick=%.1fx vol=%.1fx",
                     direction, scored["score"], scored["breakdown"],
                     sweep["age_candles"], sweep["wick_body_ratio"], sweep["volume_ratio"])
        else:
            log.info("1H %s PASSED score=%.1f %s | NO SWEEP (score reduced, alert labelled)",
                     direction, scored["score"], scored["breakdown"])

        return {
            "direction": direction,
            "indicators": snap,
            "sweep": sweep,          # None when no sweep; always forwarded to the AI
            "score": scored["score"],
            "score_breakdown": scored["breakdown"],
            "checks": _explain_checks(snap, sweep, direction, scored["breakdown"]),
            "bb_bandwidth": scored["bb_bandwidth"],
        }

    log.debug("1H rejected both directions: %s", rejections)
    return None


def _explain_checks(snap: dict, sweep: Optional[dict], direction: str,
                    breakdown: dict) -> dict:
    """Human-readable boolean view of the graded components.

    Kept because the AI prompt, the Telegram alert and the tests all read a
    `checks` mapping. Booleans are derived from the scores rather than
    recomputed, so they can never disagree with the score.
    """
    if direction == "BUY":
        return {
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "rsi_in_range_rising": breakdown["rsi"] > 0,
            "rsi_in_note_band": breakdown["rsi"] >= config.W_1H_RSI,
            "in_bottom_zone": breakdown["zone"] >= config.W_1H_ZONE,
            "in_zone_at_all": breakdown["zone"] > 0,
            "volume_increasing": breakdown["volume"] > 0,
            "near_lower_bb": breakdown["bb"] >= config.W_1H_BB,
            "not_overbought": snap["rsi"] <= config.RSI_OVERBOUGHT,
            "sweep_detected": sweep is not None,
        }
    return {
        "price_below_ema21": snap["close"] < snap["ema21"],
        "price_below_vwap": snap["close"] < snap["vwap"],
        "rsi_in_range_falling": breakdown["rsi"] > 0,
        "rsi_in_note_band": breakdown["rsi"] >= config.W_1H_RSI,
        "in_top_zone": breakdown["zone"] >= config.W_1H_ZONE,
        "in_zone_at_all": breakdown["zone"] > 0,
        "volume_increasing": breakdown["volume"] > 0,
        "near_upper_bb": breakdown["bb"] >= config.W_1H_BB,
        "not_oversold": snap["rsi"] >= config.RSI_OVERSOLD,
        "sweep_detected": sweep is not None,
    }
