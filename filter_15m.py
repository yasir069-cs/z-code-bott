"""Phase 5 — 15M confirmation filter.

Only coins that passed the 1H context reach this stage.

Core conditions (RSI, EMA21, VWAP, Volume) must ALL independently pass.
BB touch is a bonus condition — adds confidence but not required.

BUY core: RSI in range | price > EMA21 | price > VWAP | volume > 20-avg
SELL core: RSI in range | price < EMA21 | price < VWAP | volume > 20-avg
"""
import logging
from typing import Optional

import config
from indicators import compute_indicators

log = logging.getLogger("filter_15m")


def confirm_15m(df, direction: str) -> Optional[dict]:
    """Score the confirmation conditions.

    Core conditions (RSI, EMA, VWAP, Volume) must ALL pass independently —
    no indicator can compensate for another. BB is an optional bonus.
    """
    snap = compute_indicators(df)
    if snap is None:
        return None

    if direction == "BUY":
        core = {
            "rsi_in_range": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX,
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
        }
        bonus = {
            "near_lower_bb": (snap["low"] <= snap["bb_lower"] * (1 + config.BB_NEAR_PCT)
                              and snap["high"] >= snap["bb_lower"] * (1 - config.BB_NEAR_PCT)),
        }
    elif direction == "SELL":
        core = {
            "rsi_in_range": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX,
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
        }
        bonus = {
            "near_upper_bb": (snap["high"] >= snap["bb_upper"] * (1 - config.BB_NEAR_PCT)
                              and snap["low"] <= snap["bb_upper"] * (1 + config.BB_NEAR_PCT)),
        }
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    # Core conditions must ALL pass — RSI, EMA, VWAP, Volume independently verify
    if not all(core.values()):
        failed = {k: v for k, v in core.items() if not v}
        log.debug("15M %s REJECTED — core failed: %s", direction, failed)
        return None

    checks = {**core, **bonus}
    score = sum(checks.values())
    log.debug("15M %s PASSED (score %d/5, core=4/4 ✓): %s", direction, score, checks)
    return {"direction": direction, "score": score, "checks": checks, "indicators": snap}
