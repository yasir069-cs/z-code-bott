"""Support / Resistance — priority-2 layer.

Auto-detects horizontal S/R as ZONES (price ranges), never single prices. Swing
pivots from market_structure become candidate levels; nearby levels cluster into
a zone whose width is the cluster span padded by a fraction of ATR. Each zone is
strength-scored by:
  * touches        — how many candles interacted with the zone,
  * rejection wicks — candles that pierced the zone and closed back out,
  * volume         — average volume of the touching candles vs the frame,
  * consolidation  — candles that closed inside the zone (time spent).

Zones with >= config.SR_MAJOR_TOUCHES touches are tagged major. Zones are
classified support/resistance relative to the current price, and the nearest
zone on each side is exposed so the risk gate can measure reward to the nearest
*opposing* zone and reject a long taken right under resistance.

Also derives previous-day / previous-week / session high-low from the frame's
UTC timestamps, degrading to None when the frame does not span that far.

Pure and look-ahead-safe: only the frame's own rows are used, and the "current"
price is its last closed candle.
"""
import numpy as np
import pandas as pd

import config
import market_structure as ms
import ta_utils


def _cluster(levels: list[float], tol: float) -> list[list[float]]:
    """Greedy-merge sorted levels whose gap is within `tol` into clusters."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters = [[levels[0]]]
    for p in levels[1:]:
        if p - clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters


def _session_levels(df: pd.DataFrame) -> dict:
    """Previous-day and previous-week high/low from the frame's UTC index.

    None for any level the frame does not reach back far enough to define.
    """
    out = {"prev_day_high": None, "prev_day_low": None,
           "prev_week_high": None, "prev_week_low": None}
    if df is None or df.empty:
        return out
    idx = df.index
    last_day = idx[-1].normalize()

    by_day_high = df["high"].groupby(idx.normalize())
    by_day_low = df["low"].groupby(idx.normalize())
    prev_days = [d for d in by_day_high.groups if d < last_day]
    if prev_days:
        pd_key = max(prev_days)
        out["prev_day_high"] = float(by_day_high.get_group(pd_key).max())
        out["prev_day_low"] = float(by_day_low.get_group(pd_key).min())

    iso_week = idx.isocalendar().week.to_numpy()
    last_week = iso_week[-1]
    prev_mask = iso_week < last_week
    if prev_mask.any():
        prev_week = iso_week[prev_mask].max()
        wmask = iso_week == prev_week
        out["prev_week_high"] = float(df["high"].to_numpy()[wmask].max())
        out["prev_week_low"] = float(df["low"].to_numpy()[wmask].min())
    return out


def _score_zone(df: pd.DataFrame, lo: float, hi: float, atr: float) -> dict:
    """Count touches / rejections / volume / consolidation for one zone."""
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    close = df["close"].to_numpy(dtype="float64")
    volume = df["volume"].to_numpy(dtype="float64")
    vol_avg = float(volume.mean()) if len(volume) else 0.0

    overlap = (low <= hi) & (high >= lo)          # candle range touches the zone
    touches = int(np.count_nonzero(overlap))
    inside = (close >= lo) & (close <= hi)        # closed inside -> consolidation/time
    consolidation = int(np.count_nonzero(inside))

    # rejection wick: pierced the zone but closed clear of it (either side)
    pierced_up = (high >= lo) & (close < lo)
    pierced_down = (low <= hi) & (close > hi)
    rejections = int(np.count_nonzero(pierced_up | pierced_down))

    touch_vol = float(volume[overlap].mean()) if touches else 0.0
    vol_factor = (touch_vol / vol_avg) if vol_avg > 0 else 0.0

    strength = (touches
                + config.SR_WICK_BONUS * rejections
                + vol_factor
                + 0.25 * consolidation)
    return {
        "touches": touches,
        "rejections": rejections,
        "consolidation": consolidation,
        "vol_factor": round(vol_factor, 2),
        "strength": round(float(strength), 2),
        "major": touches >= config.SR_MAJOR_TOUCHES,
    }


def analyze(df: pd.DataFrame, cfg=config) -> dict:
    """Detect S/R zones and the nearest zone on each side of the current price."""
    empty = {"zones": [], "nearest_support": None, "nearest_resistance": None,
             "at_zone": None, "session_levels": _session_levels(df), "atr": 0.0,
             "price": None}
    if df is None or len(df) < cfg.SR_MIN_TOUCHES + 2:
        return empty

    window = df.tail(cfg.SR_LOOKBACK)
    high = window["high"].to_numpy(dtype="float64")
    low = window["low"].to_numpy(dtype="float64")
    atr = ta_utils.atr(window, cfg.STRUCT_ATR_LENGTH)
    price = float(window["close"].iloc[-1])
    if atr <= 0:
        return {**empty, "price": price}

    highs, lows = ms.swing_pivots(high, low, cfg.STRUCT_PIVOT_LEFT, cfg.STRUCT_PIVOT_RIGHT)
    levels = [p for _, p in highs] + [p for _, p in lows]
    if not levels:
        return {**empty, "price": price}

    tol = cfg.SR_CLUSTER_ATR_MULT * atr
    pad = cfg.SR_ZONE_PAD_ATR * atr

    zones = []
    for cluster in _cluster(levels, tol):
        lo = min(cluster) - pad
        hi = max(cluster) + pad
        z = {"lo": round(lo, 8), "hi": round(hi, 8), "mid": round((lo + hi) / 2, 8),
             "levels": len(cluster)}
        z.update(_score_zone(window, lo, hi, atr))
        if z["touches"] < cfg.SR_MIN_TOUCHES:
            continue
        z["side"] = "resistance" if z["mid"] >= price else "support"
        zones.append(z)

    if not zones:
        return {**empty, "price": price}

    # keep the strongest N per side (noise guard)
    for side in ("support", "resistance"):
        side_zones = sorted([z for z in zones if z["side"] == side],
                            key=lambda z: z["strength"], reverse=True)
        for z in side_zones[cfg.SR_MAX_ZONES:]:
            zones.remove(z)

    supports = [z for z in zones if z["side"] == "support"]
    resistances = [z for z in zones if z["side"] == "resistance"]
    nearest_support = max(supports, key=lambda z: z["mid"], default=None)
    nearest_resistance = min(resistances, key=lambda z: z["mid"], default=None)

    prox = cfg.SR_PROXIMITY_ATR * atr
    at_zone = next((z for z in sorted(zones, key=lambda z: z["strength"], reverse=True)
                    if (z["lo"] - prox) <= price <= (z["hi"] + prox)), None)

    return {
        "zones": sorted(zones, key=lambda z: z["mid"]),
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "at_zone": at_zone,
        "session_levels": _session_levels(df),
        "atr": atr,
        "price": price,
    }
