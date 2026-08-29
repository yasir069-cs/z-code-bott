"""Liquidity & sweeps — priority-3 layer.

Yasir's rule: a long is only valid after price sweeps **sell-side** liquidity
(runs the stops below a low / equal-lows pool), *reclaims* the level, and then
prints a **bullish confirmation** candle. A short mirrors it on the buy side.
The touch alone is never an entry — post-sweep confirmation is MANDATORY
(config.LIQ_CONFIRM_REQUIRED).

The sweep+reclaim in a single candle is already detected by
`filter_1h.detect_sweep` (wick pierces the swing, body closes back, long wick,
volume spike). This module reuses it verbatim and adds the two pieces that layer
needs for a *decision*:
  * equal highs / lows — clustered swing extremes that mark stop pools,
  * confirmation — a follow-through candle in the LIQ_RECLAIM_CANDLES bars after
    the sweep that closes in the trade's direction and holds the reclaim.

`ready` on a sweep means all of {sweep, reclaim, confirmation} hold, so the
decision core can treat an unconfirmed sweep as "setup forming, not tradable"
rather than forcing an entry. Pure and look-ahead-safe.
"""
from typing import Optional

import numpy as np
import pandas as pd

import config
import market_structure as ms
import ta_utils
from filter_1h import detect_sweep


def _cluster_levels(pivots: list[tuple[int, float]], tol: float,
                    min_count: int) -> list[dict]:
    """Cluster near-equal pivot prices into stop pools (equal highs/lows)."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda pv: pv[1])
    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for pv in ordered[1:]:
        if pv[1] - clusters[-1][-1][1] <= tol:
            clusters[-1].append(pv)
        else:
            clusters.append([pv])
    pools = []
    for c in clusters:
        if len(c) >= min_count:
            pools.append({
                "level": round(float(np.mean([p for _, p in c])), 8),
                "count": len(c),
                "positions": [pos for pos, _ in c],
            })
    return pools


def _confirm(df: pd.DataFrame, p: int, direction: str, level: float,
             atr: float, cfg) -> Optional[dict]:
    """Look for a post-sweep confirmation candle in the next LIQ_RECLAIM_CANDLES.

    BUY: a bullish candle (close > open) that holds above the reclaimed level.
    SELL: a bearish candle that holds below it. Returns the confirming candle's
    info, or None if none appears in the window.
    """
    n = len(df)
    open_ = df["open"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    buf = cfg.LIQ_CONFIRM_CLOSE_ATR * atr
    end = min(n - 1, p + cfg.LIQ_RECLAIM_CANDLES)
    for c in range(p + 1, end + 1):
        if direction == "BUY" and close[c] > open_[c] and close[c] >= level + buf:
            return {"pos": c, "age_candles": n - 1 - c, "close": float(close[c])}
        if direction == "SELL" and close[c] < open_[c] and close[c] <= level - buf:
            return {"pos": c, "age_candles": n - 1 - c, "close": float(close[c])}
    return None


def _evaluate(df: pd.DataFrame, direction: str, atr: float, cfg) -> Optional[dict]:
    """Detect the most recent sweep for `direction` and test its confirmation."""
    s = detect_sweep(df, direction)
    if s is None:
        return None
    p = len(df) - 1 - s["age_candles"]
    confirmation = _confirm(df, p, direction, s["level"], atr, cfg)
    confirmed = confirmation is not None
    ready = confirmed if cfg.LIQ_CONFIRM_REQUIRED else True
    return {
        **s,
        "side": "sell-side" if direction == "BUY" else "buy-side",
        "reclaimed": True,            # detect_sweep already requires close-back
        "confirmed": confirmed,
        "confirmation": confirmation,
        "ready": ready,
    }


def analyze(df: pd.DataFrame, cfg=config) -> dict:
    """Detect stop pools and confirmed sweeps in both directions."""
    empty = {"equal_highs": [], "equal_lows": [], "buy_sweep": None,
             "sell_sweep": None, "sweep": None, "long_ready": False,
             "short_ready": False, "atr": 0.0}
    if df is None or len(df) < cfg.SWEEP_WINDOW + 1:
        return empty

    window = df.tail(cfg.STRUCT_LOOKBACK)
    high = window["high"].to_numpy(dtype="float64")
    low = window["low"].to_numpy(dtype="float64")
    atr = ta_utils.atr(window, cfg.STRUCT_ATR_LENGTH)
    if atr <= 0:
        return empty

    highs, lows = ms.swing_pivots(high, low, cfg.STRUCT_PIVOT_LEFT, cfg.STRUCT_PIVOT_RIGHT)
    tol = cfg.LIQ_EQUAL_TOL_ATR * atr
    equal_highs = _cluster_levels(highs, tol, cfg.LIQ_MIN_EQUAL)
    equal_lows = _cluster_levels(lows, tol, cfg.LIQ_MIN_EQUAL)

    buy_sweep = _evaluate(df, "BUY", atr, cfg)     # sell-side sweep -> long setup
    sell_sweep = _evaluate(df, "SELL", atr, cfg)   # buy-side sweep -> short setup

    candidates = [s for s in (buy_sweep, sell_sweep) if s is not None]
    sweep = min(candidates, key=lambda s: s["age_candles"], default=None)

    return {
        "equal_highs": equal_highs,
        "equal_lows": equal_lows,
        "buy_sweep": buy_sweep,
        "sell_sweep": sell_sweep,
        "sweep": sweep,
        "long_ready": bool(buy_sweep and buy_sweep["ready"]),
        "short_ready": bool(sell_sweep and sell_sweep["ready"]),
        "atr": atr,
    }
