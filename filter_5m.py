"""5M entry-timeframe **feature extractor** (structure-first rework).

`entry_5m` used to hard-gate the direction on the 5M indicator checklist and
only then report the entry price. Under the structure-first design it is now a
thin feature extractor: it reports the 5M structural bias, whether it aligns
with the direction under consideration, the prospective entry price (the last
5M close), the indicator snapshot for the bounded secondary layer, and the RSI
bounce/rejection pattern (kept as metadata for the alert badge). It never hard-
rejects — the authoritative verdict is made by ``decision.decide()``.

Kept name/signature so `main.py` and the scripts stay recognizable.
"""
import logging
from typing import Optional

import config
import market_structure
from indicators import compute_indicators, rsi_trend_down, rsi_trend_up

log = logging.getLogger("filter_5m")

_MIN_FRAME = config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT + 2

_WANT_BIAS = {"BUY": "bullish", "SELL": "bearish"}


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
    """Read the 5M entry-timeframe structure for `direction` (BUY|SELL).

    Returns a feature dict (never a pass/fail gate):

        direction            — the direction being evaluated
        bias                 — 5M structural bias
        aligned              — True when the 5M structure supports `direction`
        entry_price          — last 5M close (the prospective entry)
        structure            — full market_structure.analyze output
        indicators           — secondary-layer snapshot (may be None)
        rsi_bounce_detected  — RSI bounce/rejection pattern metadata

    Returns None only when the frame is too short to analyze.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")
    if df is None or len(df) < _MIN_FRAME:
        return None

    struct = market_structure.analyze(df)
    snap = compute_indicators(df)
    pattern = _rsi_pattern(snap, direction) if snap else {
        "rsi_trend": False, "rsi_stair": False, "rsi_bounce_detected": False}
    aligned = struct["bias"] == _WANT_BIAS[direction]
    entry_price = float(df["close"].iloc[-1])

    log.debug("5M %s bias=%s aligned=%s entry=%.6g bounce=%s",
              direction, struct["bias"], aligned, entry_price,
              pattern["rsi_bounce_detected"])

    return {
        "direction": direction,
        "bias": struct["bias"],
        "aligned": aligned,
        "entry_price": entry_price,
        "structure": struct,
        "indicators": snap,
        "rsi_bounce_detected": pattern["rsi_bounce_detected"],
        "rsi_pattern": pattern,
    }
