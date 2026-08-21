"""Phase 3 + Phase 4 — Liquidation Sweep detection and 1H context filter.

Liquidation sweep (all 4 conditions must hold, rules.md):
  BULLISH (BUY): lower wick pierces below recent swing low (last 20 candles),
                 body closes back ABOVE that swing low, lower wick > 2x body,
                 volume > 1.5x avg volume of the last 20 candles.
  BEARISH (SELL): upper wick pierces above recent swing high, body closes
                 back BELOW it, upper wick > 2x body, volume spike.

1H context (BUY): bottom 30% zone, RSI moving up through 50-70, price above
EMA21, price above VWAP, volume increasing, near/touching lower BB, valid
bullish sweep. SELL is the mirror. Context fail -> the coin is skipped
before 15M/5M are ever fetched.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
from indicators import compute_indicators

log = logging.getLogger("filter_1h")


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


def _near_lower_band(snap: dict, near_pct: float) -> bool:
    """Candle touched / is near the lower band: its range overlaps the
    band's `near_pct` neighborhood (excludes being far BELOW the band too)."""
    return (snap["low"] <= snap["bb_lower"] * (1 + near_pct)
            and snap["high"] >= snap["bb_lower"] * (1 - near_pct))


def _near_upper_band(snap: dict, near_pct: float) -> bool:
    return (snap["high"] >= snap["bb_upper"] * (1 - near_pct)
            and snap["low"] <= snap["bb_upper"] * (1 + near_pct))


def analyze_1h(df: pd.DataFrame) -> Optional[dict]:
    """Classify 1H context. Returns a context dict when the coin passes
    (direction BUY or SELL), otherwise None (skip the coin entirely).

    3 Core Pillars: RSI + EMA21 + VWAP (plus volume, BB context, and guards).
    Guards: overbought/oversold rejection, sideways/squeeze detection.
    Sweep detection is OPTIONAL — when found it adds high confidence for the AI,
    and all sweep metrics are collected and forwarded to the AI decision bundle.
    """
    snap = compute_indicators(df)
    if snap is None:
        return None

    # --- Sideways market detection: skip when BB bandwidth is too narrow ---
    bb_bandwidth = (snap["bb_upper"] - snap["bb_lower"]) / snap["bb_mid"] if snap["bb_mid"] > 0 else 0
    if bb_bandwidth < config.BB_BANDWIDTH_MIN:
        log.debug("1H rejected: BB bandwidth %.4f < %.4f (sideways/squeeze, no signal)",
                  bb_bandwidth, config.BB_BANDWIDTH_MIN)
        return None

    checks = {
        "BUY": {
            "not_overbought": snap["rsi"] <= config.RSI_OVERBOUGHT,
            "rsi_45_80_rising": config.RSI_BUY_MIN <= snap["rsi"] <= config.RSI_BUY_MAX
                                and snap["rsi"] > snap["rsi_prev"],
            "price_above_ema21": snap["close"] > snap["ema21"],
            "price_above_vwap": snap["close"] > snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "near_lower_bb": _near_lower_band(snap, config.BB_NEAR_PCT_1H),
        },
        "SELL": {
            "not_oversold": snap["rsi"] >= config.RSI_OVERSOLD,
            "rsi_22_52_falling": config.RSI_SELL_MIN <= snap["rsi"] <= config.RSI_SELL_MAX
                                 and snap["rsi"] < snap["rsi_prev"],
            "price_below_ema21": snap["close"] < snap["ema21"],
            "price_below_vwap": snap["close"] < snap["vwap"],
            "volume_increasing": snap["volume"] > snap["volume_prev"],
            "near_upper_bb": _near_upper_band(snap, config.BB_NEAR_PCT_1H),
        },
    }

    for direction in ("BUY", "SELL"):
        if not all(checks[direction].values()):
            if any(checks[direction].values()):
                log.debug("1H %s context failed: %s",
                          direction, {k: v for k, v in checks[direction].items() if not v})
            continue

        # Sweep is optional for passing 1H, but ALWAYS collected for AI decision
        sweep = detect_sweep(df, direction)
        if sweep is not None:
            log.info("1H %s sweep detected (age=%d candles, wick_ratio=%.1f, vol_ratio=%.1f) — extra AI confidence",
                     direction, sweep["age_candles"], sweep["wick_body_ratio"], sweep["volume_ratio"])
        else:
            log.debug("1H %s context passed (no recent sweep detected) — signal forwarded to AI", direction)

        return {
            "direction": direction,
            "indicators": snap,
            "sweep": sweep,  # Collected and passed to AI bundle (None if no sweep)
            "checks": checks[direction],
            "bb_bandwidth": bb_bandwidth,
        }
    return None
