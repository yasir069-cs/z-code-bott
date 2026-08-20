"""Phase 6 — 5M entry filter (scoring system).

Only candidates that passed 1H + 15M reach this stage.
BUY conditions (need 5/7):
  RSI 40-75, RSI trend UP, higher lows in recent RSI, price > EMA21,
  price > VWAP, volume above average, lower BB bounce.
SELL conditions (need 5/7):
  RSI 28-55, RSI trend DOWN, lower highs, price < EMA21,
  price < VWAP, volume above average, upper BB rejection.

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
        checks = {
            "rsi_40_75": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX,
            "rsi_trend_up": rsi_trend_up(history),
            "rsi_higher_lows": has_higher_lows,
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
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
        checks = {
            "rsi_28_55": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX,
            "rsi_trend_down": rsi_trend_down(history),
            "rsi_lower_highs": has_lower_highs,
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_above_avg": snap["volume"] > snap["volume_avg20"],
            "bb_upper_rejection": rejection,
        }
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    score = sum(checks.values())
    if score < config.ENTRY_MIN_SCORE:
        log.debug("5M %s rejected (score %d/%d): %s", direction, score, len(checks),
                  {k: v for k, v in checks.items() if not v})
        return None
    log.debug("5M %s passed (score %d/%d): %s", direction, score, len(checks), checks)
    return {"direction": direction, "score": score, "checks": checks, "indicators": snap}
