"""Python-only fallbacks when the AI (OpenRouter/Nemotron) is unavailable.

Two independent fallbacks live here:

  * ``fallback_decision(bundle)`` — the legacy indicator-based decision, kept for
    the on-demand single-lookup path (``main._decide`` / ``scripts/e2e_demo.py``)
    and its test suite. It still emits a signal/levels.
  * ``explanation_fallback(decision)`` — the structure-first path. The
    deterministic core (``decision.decide``) has ALREADY decided; this only
    writes the natural-language explanation locally, tagged so the alert shows
    the prose was generated locally rather than by the model. It never returns a
    signal or levels — the decision can no longer be changed or dropped just
    because the LLM is down.
"""
import logging

import config
from indicators import rsi_trend_down, rsi_trend_up

log = logging.getLogger("fallback")

# Tag so the alert/reason text is honest about the prose being local, not the
# model's. Distinct from alerts._FALLBACK_TAG (which is about the whole signal).
EXPLANATION_LOCAL_TAG = "explanation generated locally"


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


_DIR_WORD = {"LONG": "long", "SHORT": "short"}

# NO_TRADE reason codes -> plain-English phrases for the local explanation.
_REASON_TEXT = {
    "insufficient_data": "not enough closed candles to read structure",
    "no_directional_bias": "market structure is ranging (no HH/HL or LH/LL bias)",
    "counter_htf": "the setup opposes the higher-timeframe bias",
    "insufficient_primary_evidence": "primary evidence (structure/S-R/liquidity) is too thin",
    "low_setup_quality": "multi-factor confluence is below the quality bar",
    "no_clear_target": "no realistic opposing zone to target",
    "stop_too_wide": "the structural stop is too wide for the volatility",
    "poor_rr": "reward:risk is below the minimum",
    "target_too_close": "the nearest opposing zone is too close",
    "into_opposing_zone": "price is running straight into an opposing zone",
    "excessive_spread": "the spread is too wide to trade cleanly",
}


def _phrase_reasons(reasons: list) -> str:
    return "; ".join(_REASON_TEXT.get(r, r) for r in reasons) or "insufficient confluence"


def explanation_fallback(decision: dict) -> str:
    """Deterministic, local natural-language explanation of a finished decision.

    The decision has already been made by ``decision.decide``; this only turns
    its structured evidence into prose so an alert is readable even when the LLM
    is unavailable. Tagged with EXPLANATION_LOCAL_TAG so the reader knows the
    wording is local, not the model's. Never raises, never touches the verdict.
    """
    verdict = decision.get("decision", "NO_TRADE")
    struct = decision.get("structure") or {}
    mtf = decision.get("mtf") or {}
    liq = decision.get("liquidity") or {}
    pa = decision.get("price_action") or {}
    futures = decision.get("futures") or {}

    if verdict == "NO_TRADE":
        reasons = decision.get("no_trade_reasons") or []
        warn = decision.get("data_warnings") or []
        body = (f"NO_TRADE — {_phrase_reasons(reasons)}. "
                f"Structure: {struct.get('trend', 'n/a')} / bias {struct.get('bias', 'n/a')}; "
                f"HTF bias {decision.get('htf_bias', 'n/a')}.")
        if warn:
            body += f" Data notes: {', '.join(warn)}."
        return f"{body} ({EXPLANATION_LOCAL_TAG})"

    word = _DIR_WORD.get(verdict, verdict.lower())
    parts = [f"{verdict} ({word}) — structure {struct.get('trend', 'n/a')}, "
             f"bias {struct.get('bias', 'n/a')}, HTF {decision.get('htf_bias', 'n/a')}"]

    # highest-priority structural event
    event = struct.get("choch") or struct.get("bos")
    if event:
        kind = "CHoCH" if struct.get("choch") else "BOS"
        parts.append(f"{kind} {event.get('dir', '')} @ {event.get('level')}")

    # liquidity: the mandatory sweep + confirmation state
    sweep = liq.get("buy_sweep") if verdict == "LONG" else liq.get("sell_sweep")
    if sweep:
        parts.append(f"{sweep.get('side', '')} sweep "
                     f"{'confirmed' if sweep.get('confirmed') else 'unconfirmed'}")

    # price action / volume flag
    if pa.get("volume_state") == "weak":
        parts.append("weak-volume warning")
    elif pa.get("displacement") or pa.get("engulfing"):
        parts.append("momentum candle present")

    # indicators are secondary — reported, never leading
    ind = decision.get("indicators") or {}
    if ind:
        parts.append(f"indicators {'agree' if ind.get('agrees') else 'neutral/against'} (secondary)")

    # futures context, if available
    if futures.get("available"):
        parts.append(f"futures bias {futures.get('bias', 'n/a')}")

    parts.append(f"quality {decision.get('setup_quality', 0):.0f}/100, "
                 f"R {decision.get('rr')}")
    return " | ".join(parts) + f" ({EXPLANATION_LOCAL_TAG})"
