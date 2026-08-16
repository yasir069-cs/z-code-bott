"""Phase 6 — 5M entry filter.

Only candidates that passed 1H + 15M reach this stage.
BUY (all must hold): RSI 50-70, RSI trend UP, higher lows in recent RSI
candles, price > EMA21, price > VWAP, volume increasing, lower BB bounce.
SELL (all must hold): RSI 35-50, RSI trend DOWN, lower highs, price < EMA21,
price < VWAP, volume increasing, upper BB rejection.

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
        checks = {
            "rsi_50_70": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX,
            "rsi_trend_up": rsi_trend_up(history),
            "rsi_higher_lows": rsi_trend_up(history),
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "bb_lower_bounce": bounce,
        }
    elif direction == "SELL":
        rejection = (
            snap["high"] >= snap["bb_upper"] * (1 - config.BB_NEAR_PCT)
            and snap["close"] < snap["bb_upper"]
        )
        checks = {
            "rsi_35_50": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX,
            "rsi_trend_down": rsi_trend_down(history),
            "rsi_lower_highs": rsi_trend_down(history),
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "bb_upper_rejection": rejection,
        }
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    if not all(checks.values()):
        log.debug("5M %s rejected: %s", direction, {k: v for k, v in checks.items() if not v})
        return None
    return {"direction": direction, "checks": checks, "indicators": snap}
