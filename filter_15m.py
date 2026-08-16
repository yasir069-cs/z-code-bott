"""Phase 5 — 15M confirmation filter.

Only coins that passed the 1H context reach this stage.
BUY (need 4/5): RSI 50-70 | price > EMA21 | price > VWAP | volume increasing |
                lower BB touch/near.
SELL (need 4/5): RSI 35-50 | price < EMA21 | price < VWAP | volume increasing |
                upper BB touch/near.
Fewer than 4/5 -> reject.
"""
import logging
from typing import Optional

import config
from indicators import compute_indicators

log = logging.getLogger("filter_15m")


def confirm_15m(df, direction: str) -> Optional[dict]:
    """Score the 5 confirmation conditions; pass on >= 4/5."""
    snap = compute_indicators(df)
    if snap is None:
        return None

    if direction == "BUY":
        checks = {
            "rsi_50_70": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX,
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "near_lower_bb": (snap["low"] <= snap["bb_lower"] * (1 + config.BB_NEAR_PCT)
                              and snap["high"] >= snap["bb_lower"] * (1 - config.BB_NEAR_PCT)),
        }
    elif direction == "SELL":
        checks = {
            "rsi_35_50": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX,
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "near_upper_bb": (snap["high"] >= snap["bb_upper"] * (1 - config.BB_NEAR_PCT)
                              and snap["low"] <= snap["bb_upper"] * (1 + config.BB_NEAR_PCT)),
        }
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    score = sum(checks.values())
    passed = score >= config.CONFIRM_MIN_SCORE
    log.debug("15M %s score %d/5: %s", direction, score, checks)
    if not passed:
        return None
    return {"direction": direction, "score": score, "checks": checks, "indicators": snap}
