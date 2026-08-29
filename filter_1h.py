"""Liquidation-sweep detection + 1H **feature extractor** (structure-first rework).

Liquidation sweep (all 4 conditions must hold, rules.md):
  BULLISH (BUY): lower wick pierces below recent swing low (last 20 candles),
                 body closes back ABOVE that swing low, lower wick > 2x body,
                 volume > SWEEP_VOL_RATIO x avg volume of the last 20 candles.
  BEARISH (SELL): upper wick pierces above recent swing high, body closes
                 back BELOW it, upper wick > 2x body, volume spike.

`analyze_1h` used to be a hard indicator gate that also *chose the direction*
from the RSI/EMA/VWAP/BB checklist. Under the structure-first design it is now a
thin **feature extractor**: it reads the 1H market structure and returns a
directional *hint* (BUY / SELL / None) plus the aligned sweep. That hint is used
only as the scan funnel — "is this symbol worth fetching 15M/5M for" — while the
authoritative LONG / SHORT / NO_TRADE verdict is made later by
`decision.decide()` across all three timeframes. Indicators are no longer a gate
here; they survive only as the bounded secondary layer inside `setup_quality`.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
import market_structure
from indicators import compute_indicators

log = logging.getLogger("filter_1h")

# Shortest 1H frame market_structure can analyze (needs left+right pivot context).
_MIN_FRAME = config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT + 2

# Structural bias -> the trade-direction hint used only for the scan funnel.
_BIAS_TO_DIR = {"bullish": "BUY", "bearish": "SELL"}


def detect_sweep(df: pd.DataFrame, direction: str) -> Optional[dict]:
    """Find the most recent valid liquidation sweep (direction: 'BUY'|'SELL').

    Searches the last SWEEP_SEARCH_CANDLES closed candles; the swing level
    for each candidate is the min low / max high of the SWEEP_WINDOW candles
    immediately before it. Returns None when no valid sweep exists.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")

    n = len(df)
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    open_ = df["open"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    volume = df["volume"].to_numpy(dtype="float64")

    for pos_from_end in range(1, config.SWEEP_SEARCH_CANDLES + 1):
        p = n - pos_from_end
        if p - config.SWEEP_WINDOW < 0:
            break
        body = abs(close[p] - open_[p])
        if direction == "BUY":
            swing = float(np.min(low[p - config.SWEEP_WINDOW:p]))
            wick = min(open_[p], close[p]) - low[p]
            pierced = low[p] < swing
            closed_back = close[p] > swing
        else:
            swing = float(np.max(high[p - config.SWEEP_WINDOW:p]))
            wick = high[p] - max(open_[p], close[p])
            pierced = high[p] > swing
            closed_back = close[p] < swing
        vol_avg = float(np.mean(volume[p - config.SWEEP_WINDOW:p]))
        wick_ok = wick > config.SWEEP_WICK_BODY_RATIO * body
        volume_ok = volume[p] > config.SWEEP_VOL_RATIO * vol_avg and vol_avg > 0

        if pierced and closed_back and wick_ok and volume_ok:
            return {
                "direction": direction,
                "age_candles": pos_from_end - 1,   # 0 = last closed candle
                "timestamp": df.index[p],
                "level": swing,
                "wick": float(wick),
                "body": float(body),
                "wick_body_ratio": float(wick / body) if body > 0 else float("inf"),
                "volume_ratio": float(volume[p] / vol_avg) if vol_avg > 0 else float("inf"),
                "candle_low": float(low[p]),
                "candle_high": float(high[p]),
            }
    return None


def analyze_1h(df: pd.DataFrame) -> Optional[dict]:
    """Extract the 1H structural read used as the scan funnel.

    This is **not** a trade decision and no longer hard-rejects on indicators.
    It reports the HTF market structure and a directional *hint*:

        direction = BUY   when structure is bullish  (worth checking for a long)
                    SELL  when structure is bearish  (worth checking for a short)
                    None  when structure is ranging / undecided

    A None direction simply means "don't spend 15M/5M fetches on this symbol
    right now" — the authoritative LONG / SHORT / NO_TRADE verdict is still made
    by ``decision.decide()`` over all three timeframes. Returns None only when
    the frame is too short to analyze at all.

    The aligned liquidation sweep and the indicator snapshot are attached as
    context (for the alert / logging / the secondary confirmation layer); they
    never gate here.
    """
    if df is None or len(df) < _MIN_FRAME:
        return None

    struct = market_structure.analyze(df)
    bias = struct["bias"]
    direction = _BIAS_TO_DIR.get(bias)

    sweep = detect_sweep(df, direction) if direction else None
    snap = compute_indicators(df)

    if direction:
        trend = struct.get("trend")
        # A CHoCH-driven bias against the standing trend is intentional
        # (the newest structural information), but it is an EARLY reversal —
        # the scoring core penalizes it until the trend itself confirms.
        conflict = ((bias == "bullish" and trend == "downtrend")
                    or (bias == "bearish" and trend == "uptrend"))
        log.info("1H %s bias=%s trend=%s%s | sweep=%s",
                 direction, bias, trend,
                 " [CHoCH reversal — reduced conviction]" if conflict else "",
                 "yes" if sweep else "no")
    else:
        log.debug("1H no directional bias (bias=%s) — funnel skip", bias)

    return {
        "direction": direction,     # BUY / SELL / None (funnel hint only)
        "bias": bias,               # bullish / bearish / ranging
        "structure": struct,        # full market_structure.analyze output
        "sweep": sweep,             # aligned sweep or None (context, never gates)
        "indicators": snap,         # secondary-layer snapshot (may be None)
    }
