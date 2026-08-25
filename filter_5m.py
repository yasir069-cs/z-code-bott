"""Phase 6 — 5M entry filter.

Only candidates that passed 1H + 15M reach this stage. The 5M close becomes
the alert's entry price.

Grading of the note's three shared conditions is delegated to
scoring.score_ltf() (EMA21/VWAP hard, RSI/volume/Bollinger graded) and the
resulting 0-100 score must clear config.MIN_SCORE_5M.

On top of the score this stage detects the RSI *pattern* that the note's
"RSI 50 above to 70" wording implies — RSI dipping but holding a higher low
before turning back up (50 -> 55 -> 51 -> 56 is bullish). That is reported as
`rsi_bounce_detected` metadata rather than extra points, so the 0-100 scale
stays intact; it drives the RSI-bounce badge in the Telegram alert and is a
strong input to the AI decision.
"""
import logging
from typing import Optional

import config
import scoring
from indicators import compute_indicators, rsi_trend_down, rsi_trend_up

log = logging.getLogger("filter_5m")


def _rsi_pattern(snap: dict, direction: str) -> dict:
    """Detect the RSI bounce/rejection pattern around the 50 level."""
    history = snap["rsi_history"]
    recent3 = history[-3:] if len(history) >= 3 else history

    if direction == "BUY":
        trend = rsi_trend_up(history)
        stair = len(recent3) >= 3 and recent3[-1] > recent3[-3]
        # A "bounce at 50" means RSI dipped toward 50 and turned up from it.
        near_50 = min(recent3) <= 55.0 if recent3 else False
    else:
        trend = rsi_trend_down(history)
        stair = len(recent3) >= 3 and recent3[-1] < recent3[-3]
        near_50 = max(recent3) >= 45.0 if recent3 else False

    return {
        "rsi_trend": trend,
        "rsi_stair": stair,
        "rsi_bounce_detected": bool(trend and stair and near_50),
    }


def entry_5m(df, direction: str) -> Optional[dict]:
    """Final entry trigger on the 5M timeframe.

    Returns a dict with the graded `score` (0-100), its `breakdown`, and the
    RSI-pattern metadata, or None when a hard gate fails or the score is
    below MIN_SCORE_5M.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    snap = compute_indicators(df)
    if snap is None:
        return None

    try:
        scored = scoring.score_ltf(snap, direction)
    except scoring.Rejected as rej:
        log.debug("5M %s REJECTED — %s", direction, rej)
        return None

    if scored["score"] < config.MIN_SCORE_5M:
        log.debug("5M %s REJECTED — score %.1f < %d %s",
                  direction, scored["score"], config.MIN_SCORE_5M, scored["breakdown"])
        return None

    pattern = _rsi_pattern(snap, direction)
    log.debug("5M %s PASSED score=%.1f %s bounce=%s",
              direction, scored["score"], scored["breakdown"], pattern["rsi_bounce_detected"])

    above = direction == "BUY"
    checks = {
        ("price_above_ema21" if above else "price_below_ema21"):
            snap["close"] > snap["ema21"] if above else snap["close"] < snap["ema21"],
        ("price_above_vwap" if above else "price_below_vwap"):
            snap["close"] > snap["vwap"] if above else snap["close"] < snap["vwap"],
        "rsi_in_range": scored["breakdown"]["rsi"] > 0,
        "rsi_in_note_band": scored["breakdown"]["rsi"] >= config.W_LTF_RSI,
        "volume_increasing": scored["breakdown"]["volume"] > 0,
        ("near_lower_bb" if above else "near_upper_bb"):
            scored["breakdown"]["bb"] >= config.W_LTF_BB,
        **pattern,
    }

    return {
        "direction": direction,
        "score": scored["score"],
        "score_breakdown": scored["breakdown"],
        "checks": checks,
        "rsi_bounce_detected": pattern["rsi_bounce_detected"],
        "indicators": snap,
    }
