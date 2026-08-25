"""Phase 5 — 15M confirmation filter.

Only coins that passed the 1H context reach this stage. The 1H direction must
still hold here on the note's three shared conditions (RSI, volume, Bollinger),
with EMA21 and VWAP as hard gates because they define the direction.

Grading is delegated to scoring.score_ltf(); the resulting 0-100 score must
clear config.MIN_SCORE_15M.
"""
import logging
from typing import Optional

import config
import scoring
from indicators import compute_indicators

log = logging.getLogger("filter_15m")


def confirm_15m(df, direction: str) -> Optional[dict]:
    """Confirm the 1H direction on the 15M timeframe.

    Returns a dict with the graded `score` (0-100) and its `breakdown`, or
    None when a hard gate fails or the score is below MIN_SCORE_15M.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    snap = compute_indicators(df)
    if snap is None:
        return None

    try:
        scored = scoring.score_ltf(snap, direction)
    except scoring.Rejected as rej:
        log.debug("15M %s REJECTED — %s", direction, rej)
        return None

    if scored["score"] < config.MIN_SCORE_15M:
        log.debug("15M %s REJECTED — score %.1f < %d %s",
                  direction, scored["score"], config.MIN_SCORE_15M, scored["breakdown"])
        return None

    log.debug("15M %s PASSED score=%.1f %s", direction, scored["score"], scored["breakdown"])
    return {
        "direction": direction,
        "score": scored["score"],
        "score_breakdown": scored["breakdown"],
        "checks": _explain_checks(snap, direction, scored["breakdown"]),
        "indicators": snap,
    }


def _explain_checks(snap: dict, direction: str, breakdown: dict) -> dict:
    """Boolean view of the graded components, derived from the scores so the
    two can never disagree. Read by the AI prompt and the tests."""
    above = direction == "BUY"
    return {
        ("price_above_ema21" if above else "price_below_ema21"):
            snap["close"] > snap["ema21"] if above else snap["close"] < snap["ema21"],
        ("price_above_vwap" if above else "price_below_vwap"):
            snap["close"] > snap["vwap"] if above else snap["close"] < snap["vwap"],
        "rsi_in_range": breakdown["rsi"] > 0,
        "rsi_in_note_band": breakdown["rsi"] >= config.W_LTF_RSI,
        "volume_increasing": breakdown["volume"] > 0,
        ("near_lower_bb" if above else "near_upper_bb"): breakdown["bb"] >= config.W_LTF_BB,
    }
