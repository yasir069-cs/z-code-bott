"""Phase 8 — Python-only fallback when Claude is unavailable/fails.

Uses the Python filter direction, SL at the recent swing low (BUY) /
swing high (SELL), TP = 2x SL distance -> fixed 1:2 RR (rules.md).
The alert must be tagged 'AI Unavailable - Indicator based signal'
(never pretend the signal came from Claude).
"""
import logging

import config
from indicators import rsi_trend_down, rsi_trend_up

log = logging.getLogger("fallback")


def fallback_decision(bundle: dict) -> dict:
    direction = bundle["direction"]
    entry = bundle["entry_price"]
    snap_1h = bundle["ind_1h"]
    sweep = bundle.get("sweep") or {}
    atr = snap_1h["atr"]

    if direction == "BUY":
        sl = sweep.get("level", snap_1h["swing_low_20"])
        if not sl or sl >= entry:  # degenerate: swing above entry -> ATR buffer
            sl = entry - 1.5 * atr
            sl_note = "ATR buffer (swing invalid)"
        else:
            sl_note = "recent swing low"
        risk = entry - sl
        tp = entry + 2 * risk
        trend = "up" if rsi_trend_up(bundle["ind_5m"]["rsi_history"]) else "flat"
        reason = (f"AI Unavailable - Indicator based signal: {direction} on {bundle['symbol']}; "
                  f"1H {direction} context + 15M {bundle['confirm_score']}/5 + 5M entry passed; "
                  f"RSI trend {trend}; SL at {sl_note}")
    elif direction == "SELL":
        sl = sweep.get("level", snap_1h["swing_high_20"])
        if not sl or sl <= entry:
            sl = entry + 1.5 * atr
            sl_note = "ATR buffer (swing invalid)"
        else:
            sl_note = "recent swing high"
        risk = sl - entry
        tp = entry - 2 * risk
        trend = "down" if rsi_trend_down(bundle["ind_5m"]["rsi_history"]) else "flat"
        reason = (f"AI Unavailable - Indicator based signal: {direction} on {bundle['symbol']}; "
                  f"1H {direction} context + 15M {bundle['confirm_score']}/5 + 5M entry passed; "
                  f"RSI trend {trend}; SL at {sl_note}")
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    return {
        "signal": direction,
        "entry": entry,
        "sl": float(sl),
        "tp": float(tp),
        "rr": 2.0,
        "reason": reason,
        "ai_used": False,
    }
