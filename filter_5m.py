"""Phase 6 — 5M entry filter (core + bonus scoring).

Only candidates that passed 1H + 15M reach this stage.

Core conditions (RSI, EMA21, VWAP, Volume) must ALL independently pass.
Bonus conditions (RSI trend, RSI pattern, BB bounce) — need at least 1/3.
Total score must be >= ENTRY_MIN_SCORE (5/7).

RSI is analysed as a TREND, not a static value: 50 -> 55 -> 51 -> 56 is
bullish because the RSI dip holds a higher low (spec example).
"""
import logging
from typing import Optional

import config
from indicators import compute_indicators, rsi_trend_down, rsi_trend_up

log = logging.getLogger("filter_5m")


def entry_5m(df, direction: str) -> Optional[dict]:
    snap = compute_indicators(df)
    if snap is None:
        return None

    history = snap["rsi_history"]

    if direction == "BUY":
        bounce = (  # touched lower band within last 3 candles and closed back above it
            snap["low"] <= snap["bb_lower"] * (1 + config.BB_NEAR_PCT)
            and snap["close"] > snap["bb_lower"]
        )
        # higher_lows: check the last 3 RSI values form an ascending pattern
        recent3 = history[-3:] if len(history) >= 3 else history
        has_higher_lows = len(recent3) >= 3 and recent3[-1] > recent3[-3]
        core = {
            "rsi_in_range": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX,
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
        }
        bonus = {
            "rsi_trend_up": rsi_trend_up(history),
            "rsi_higher_lows": has_higher_lows,
            "bb_lower_bounce": bounce,
        }
    elif direction == "SELL":
        rejection = (
            snap["high"] >= snap["bb_upper"] * (1 - config.BB_NEAR_PCT)
            and snap["close"] < snap["bb_upper"]
        )
        # lower_highs: check the last 3 RSI values form a descending pattern
        recent3 = history[-3:] if len(history) >= 3 else history
        has_lower_highs = len(recent3) >= 3 and recent3[-1] < recent3[-3]
        core = {
            "rsi_in_range": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX,
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
        }
        bonus = {
            "rsi_trend_down": rsi_trend_down(history),
            "rsi_lower_highs": has_lower_highs,
            "bb_upper_rejection": rejection,
        }
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    # Core conditions must ALL pass — RSI, EMA, VWAP, Volume independently verify
    if not all(core.values()):
        failed = {k: v for k, v in core.items() if not v}
        log.debug("5M %s REJECTED — core failed: %s", direction, failed)
        return None

    # Bonus: need at least 1/3 (RSI trend, RSI pattern, BB bounce)
    bonus_passed = sum(bonus.values())
    if bonus_passed < 1:
        log.debug("5M %s REJECTED — core OK but no bonus passed (0/3): %s", direction,
                  {k: v for k, v in bonus.items() if not v})
        return None

    checks = {**core, **bonus}
    score = sum(checks.values())
    log.debug("5M %s PASSED (score %d/7, core=4/4 ✓, bonus=%d/3): %s",
              direction, score, bonus_passed, checks)
    return {"direction": direction, "score": score, "checks": checks, "indicators": snap}
