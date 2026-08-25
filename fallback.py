"""Phase 8 — Python-only fallback when AI (OpenRouter/Nemotron) is unavailable/fails.

Uses the Python filter direction, SL at the recent swing low (BUY) /
swing high (SELL), TP = 2x SL distance -> fixed 1:2 RR (rules.md).
The alert must be tagged 'AI Unavailable - Indicator based signal'
(never pretend the signal came from the AI model).
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
    confirm = bundle.get("score_15m", bundle.get("confirm_score"))
    confirm_txt = f"{confirm:.0f}/100" if isinstance(confirm, (int, float)) else "n/a"

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
                  f"1H {direction} context + 15M {confirm_txt} + 5M entry passed; "
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
                  f"1H {direction} context + 15M {confirm_txt} + 5M entry passed; "
                  f"RSI trend {trend}; SL at {sl_note}")
    else:
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    # Confidence for a Python-only signal is the confluence the scoring engine
    # already computed; run_scan then applies the no-sweep cap uniformly.
    confluence = bundle.get("confluence")
    confidence = float(confluence) if isinstance(confluence, (int, float)) else 40.0

    return {
        "signal": direction,
        "entry": entry,
        "sl": float(sl),
        "tp": float(tp),
        "rr": 2.0,
        "confidence": confidence,
        "rsi_bounce_detected": bool(bundle.get("rsi_bounce_detected", False)),
        "reason": reason,
        "ai_used": False,
    }
