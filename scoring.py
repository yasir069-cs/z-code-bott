"""Confluence scoring — the handwritten strategy note, graded.

`strategy_spec.md` is the authority; this module is its executable form.

The note lists six conditions (RSI, EMA21, VWAP, volume, Bollinger, and
"Bottom/Top to inbetween + liquidation sweep"). Two of them define the
direction and so cannot be partial:

    HARD GATES : EMA21 side, VWAP side, RSI inside the tolerance band,
                 not overbought/oversold, BB bandwidth (squeeze), and
                 zone in the favourable half of the range.

Everything else is GRADED 0..full so that "bottom" scores more than
"inbetween" instead of a pass/fail cliff at an arbitrary percentage.

Each timeframe returns a 0-100 score. 1H carries zone + sweep (which only
exist as context); 15M/5M reweight the three shared indicator conditions
onto the same 0-100 scale so the weighted total is meaningful:

    confluence = 0.40*score_1h + 0.30*score_15m + 0.30*score_5m

All indicator math is delegated to indicators.compute_indicators() and
filter_1h.detect_sweep() (rules.md: never recompute indicators by hand).
"""
import logging
from typing import Optional

import config

log = logging.getLogger("scoring")

# Reason codes for a hard rejection, surfaced in the scan funnel counters.
REJECT_NO_SNAPSHOT = "no_indicator_snapshot"
REJECT_SQUEEZE = "bb_squeeze"
REJECT_OVEREXTENDED = "rsi_overextended"
REJECT_RSI_BAND = "rsi_out_of_band"
REJECT_RSI_TREND = "rsi_wrong_trend"
REJECT_EMA = "wrong_side_of_ema21"
REJECT_VWAP = "wrong_side_of_vwap"
REJECT_ZONE = "wrong_half_of_range"


class Rejected(Exception):
    """A hard gate failed — the coin is dropped, no score is produced."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------- components

def zone_score(range_pos: float, direction: str) -> float:
    """Grade the note's "Bottom to inbetween" (BUY) / "Top to inbetween" (SELL).

    `depth` is how far into the range the price sits, measured from the
    favourable extreme: 0.0 = at the very bottom for a BUY / very top for a
    SELL. Full points to ZONE_FULL_PCT, then a linear taper down to
    ZONE_TAPER_FLOOR at ZONE_MAX_PCT, then rejection.

    Raises Rejected when the price is in the wrong half of the range — this
    is the check that did not exist before (a coin at the top of its range
    could previously alert as a BUY).
    """
    depth = range_pos if direction == "BUY" else 1.0 - range_pos

    if depth <= config.ZONE_FULL_PCT:
        return float(config.W_1H_ZONE)
    if depth > config.ZONE_MAX_PCT:
        raise Rejected(REJECT_ZONE, f"depth {depth:.3f} > {config.ZONE_MAX_PCT}")

    # Linear taper across the "inbetween" band: full -> floor.
    span = config.ZONE_MAX_PCT - config.ZONE_FULL_PCT
    travelled = (depth - config.ZONE_FULL_PCT) / span if span > 0 else 1.0
    floor = config.W_1H_ZONE * config.ZONE_TAPER_FLOOR
    return float(config.W_1H_ZONE - travelled * (config.W_1H_ZONE - floor))


def rsi_score(rsi: float, rsi_prev: float, direction: str, weight: float) -> float:
    """Grade the note's "RSI 50 above to 70" / "RSI 50 below to 35".

    Inside the note's band with the right trend -> full weight.
    Inside the wider tolerance band -> RSI_TOL_FRACTION of it.
    Outside the tolerance band -> Rejected.

    The trend requirement (rising for BUY, falling for SELL) reflects the
    note's "50 above to 70" wording: RSI must be *moving through* the band,
    not merely sitting in it.
    """
    if direction == "BUY":
        full_lo, full_hi = config.RSI_BUY_FULL_MIN, config.RSI_BUY_FULL_MAX
        tol_lo, tol_hi = config.RSI_BUY_TOL_MIN, config.RSI_BUY_TOL_MAX
        trending = rsi > rsi_prev
    else:
        full_lo, full_hi = config.RSI_SELL_FULL_MIN, config.RSI_SELL_FULL_MAX
        tol_lo, tol_hi = config.RSI_SELL_TOL_MIN, config.RSI_SELL_TOL_MAX
        trending = rsi < rsi_prev

    if not tol_lo <= rsi <= tol_hi:
        raise Rejected(REJECT_RSI_BAND, f"rsi {rsi:.1f} outside {tol_lo}-{tol_hi}")
    if not trending:
        raise Rejected(REJECT_RSI_TREND, f"rsi {rsi_prev:.1f} -> {rsi:.1f} wrong way for {direction}")

    if full_lo <= rsi <= full_hi:
        return float(weight)
    return float(weight * config.RSI_TOL_FRACTION)


def volume_score(volume: float, volume_prev: float, volume_avg20: float, weight: float) -> float:
    """Grade the note's "volume increase".

    Rising against the previous candle is the literal reading and scores
    full. Sustained volume above the 20-candle average still counts —
    a coin trending on heavy volume should not be rejected because one
    candle happened to print flat.
    """
    if volume > volume_prev:
        return float(weight)
    if volume_avg20 > 0 and volume > volume_avg20:
        return float(weight * config.VOLUME_AVG_FRACTION)
    return 0.0


def bb_score(snap: dict, direction: str, near_pct: float, weight: float) -> float:
    """Grade the note's "Bollinger band".

    Touching / within near_pct of the band scores full; anywhere between the
    band and the mid-line scores a fraction; past the mid-line scores zero.
    Zero is NOT a rejection — BB is one graded voice among several.
    """
    band = snap["bb_lower"] if direction == "BUY" else snap["bb_upper"]
    mid = snap["bb_mid"]

    if direction == "BUY":
        touching = (snap["low"] <= band * (1 + near_pct)
                    and snap["high"] >= band * (1 - near_pct))
        between = band < snap["close"] <= mid
    else:
        touching = (snap["high"] >= band * (1 - near_pct)
                    and snap["low"] <= band * (1 + near_pct))
        between = mid <= snap["close"] < band

    if touching:
        return float(weight)
    if between:
        return float(weight * config.BB_MID_FRACTION)
    return 0.0


def sweep_score(sweep: Optional[dict]) -> float:
    """Grade the note's "liquidation sweep", decaying with age.

    Owner's decision: heavy weight, not a hard gate. A setup with everything
    else aligned but no sweep still alerts — at a reduced score, with its
    confidence capped and the alert labelled (see apply_sweep_confidence_cap).
    """
    if not sweep:
        return 0.0
    age = sweep.get("age_candles", 0)
    if age <= config.SWEEP_AGE_FULL:
        return float(config.W_1H_SWEEP)
    if age <= config.SWEEP_AGE_PARTIAL:
        return float(config.W_1H_SWEEP * config.SWEEP_PARTIAL_FRACTION)
    if age <= config.SWEEP_AGE_STALE:
        return float(config.W_1H_SWEEP * config.SWEEP_STALE_FRACTION)
    return 0.0  # older than SWEEP_AGE_STALE is not actionable


# --------------------------------------------------------------- hard gates

def _check_direction_gates(snap: dict, direction: str) -> None:
    """EMA21 and VWAP define the direction, so they can never be partial."""
    if direction == "BUY":
        if snap["close"] <= snap["ema21"]:
            raise Rejected(REJECT_EMA, f"close {snap['close']:.6g} <= ema21 {snap['ema21']:.6g}")
        if snap["close"] <= snap["vwap"]:
            raise Rejected(REJECT_VWAP, f"close {snap['close']:.6g} <= vwap {snap['vwap']:.6g}")
    else:
        if snap["close"] >= snap["ema21"]:
            raise Rejected(REJECT_EMA, f"close {snap['close']:.6g} >= ema21 {snap['ema21']:.6g}")
        if snap["close"] >= snap["vwap"]:
            raise Rejected(REJECT_VWAP, f"close {snap['close']:.6g} >= vwap {snap['vwap']:.6g}")


def bb_bandwidth(snap: dict) -> float:
    """Bollinger bandwidth as a fraction of the mid-line (squeeze detector)."""
    return (snap["bb_upper"] - snap["bb_lower"]) / snap["bb_mid"] if snap["bb_mid"] > 0 else 0.0


# --------------------------------------------------------------- timeframes

def score_1h(snap: dict, sweep: Optional[dict], direction: str) -> dict:
    """Score the 1H context: zone + RSI + volume + BB + sweep, 0-100.

    Raises Rejected when a hard gate fails.
    """
    width = bb_bandwidth(snap)
    if width < config.BB_BANDWIDTH_MIN:
        raise Rejected(REJECT_SQUEEZE, f"bandwidth {width:.4f} < {config.BB_BANDWIDTH_MIN}")

    if direction == "BUY" and snap["rsi"] > config.RSI_OVERBOUGHT:
        raise Rejected(REJECT_OVEREXTENDED, f"rsi {snap['rsi']:.1f} > {config.RSI_OVERBOUGHT}")
    if direction == "SELL" and snap["rsi"] < config.RSI_OVERSOLD:
        raise Rejected(REJECT_OVEREXTENDED, f"rsi {snap['rsi']:.1f} < {config.RSI_OVERSOLD}")

    _check_direction_gates(snap, direction)

    parts = {
        "zone": zone_score(snap["range_pos"], direction),
        "rsi": rsi_score(snap["rsi"], snap["rsi_prev"], direction, config.W_1H_RSI),
        "volume": volume_score(snap["volume"], snap["volume_prev"],
                               snap["volume_avg20"], config.W_1H_VOLUME),
        "bb": bb_score(snap, direction, config.BB_NEAR_PCT_1H, config.W_1H_BB),
        "sweep": sweep_score(sweep),
    }
    return {
        "score": round(sum(parts.values()), 2),
        "breakdown": {k: round(v, 2) for k, v in parts.items()},
        "bb_bandwidth": width,
        "direction": direction,
    }


def score_ltf(snap: dict, direction: str) -> dict:
    """Score a lower timeframe (15M or 5M): RSI + volume + BB, 0-100.

    Zone and sweep are 1H-only concepts, so the three shared conditions are
    reweighted (40/30/30) to keep every timeframe on the same 0-100 scale.
    """
    _check_direction_gates(snap, direction)

    parts = {
        "rsi": rsi_score(snap["rsi"], snap["rsi_prev"], direction, config.W_LTF_RSI),
        "volume": volume_score(snap["volume"], snap["volume_prev"],
                               snap["volume_avg20"], config.W_LTF_VOLUME),
        "bb": bb_score(snap, direction, config.BB_NEAR_PCT, config.W_LTF_BB),
    }
    return {
        "score": round(sum(parts.values()), 2),
        "breakdown": {k: round(v, 2) for k, v in parts.items()},
        "direction": direction,
    }


# --------------------------------------------------------------- confluence

def confluence(score_1h_val: float, score_15m_val: float, score_5m_val: float) -> float:
    """Weighted total across the note's three timeframes (1H - 15M - 5M)."""
    total = (config.CONFLUENCE_W_1H * score_1h_val
             + config.CONFLUENCE_W_15M * score_15m_val
             + config.CONFLUENCE_W_5M * score_5m_val)
    return round(total, 2)


def apply_sweep_confidence_cap(confidence: float, sweep: Optional[dict]) -> float:
    """Sweep is required for the STRONGEST alert tier (owner's decision).

    Without a sweep the confidence is capped just below ALERT_TIER_STRONG_MIN,
    so a no-sweep setup can still alert (NORMAL/HIGH) but can never present
    itself as a STRONG one.
    """
    if sweep:
        return confidence
    return min(confidence, config.NO_SWEEP_CONFIDENCE_CAP)


def passes_gates(score_1h_val: float, score_15m_val: float, score_5m_val: float,
                 total: float) -> bool:
    """Every timeframe must clear its own floor AND the weighted total must
    clear MIN_CONFLUENCE — one very strong timeframe cannot carry two weak ones."""
    return (score_1h_val >= config.MIN_SCORE_1H
            and score_15m_val >= config.MIN_SCORE_15M
            and score_5m_val >= config.MIN_SCORE_5M
            and total >= config.MIN_CONFLUENCE)


# --------------------------------------------------- secondary indicator layer

def indicator_confirmation(snap: Optional[dict], direction: str, cfg=config) -> dict:
    """SECONDARY indicator agreement with an already-decided direction.

    In the price-action-first system indicators no longer gate or choose a
    trade. This function is the whole of their remaining role: given a direction
    the primary layers already produced, it reports how much the indicators
    *agree*, as a net score in [-1, +1] (positive = confirmation). It NEVER
    raises and NEVER decides — setup_quality maps the net onto a bounded
    confirmation bonus / conflict penalty, so indicators can neither trigger nor
    veto an entry on their own.

    `direction` accepts either the primary vocabulary (LONG/SHORT) or the legacy
    BUY/SELL.
    """
    if snap is None:
        return {"score": 0.0, "agrees": False, "checks": {},
                "notes": ["no_indicator_snapshot"]}

    bull = direction in ("BUY", "LONG", "bullish")
    checks: dict[str, int] = {}
    notes: list[str] = []

    def mark(name: str, agree: bool, disagree: bool, msg_ok: str, msg_no: str) -> None:
        if agree:
            checks[name] = 1
            notes.append(msg_ok)
        elif disagree:
            checks[name] = -1
            notes.append(msg_no)
        else:
            checks[name] = 0

    close = snap["close"]
    mark("ema21", close > snap["ema21"] if bull else close < snap["ema21"],
         close < snap["ema21"] if bull else close > snap["ema21"],
         "price on trend side of EMA21", "price against EMA21")
    mark("vwap", close > snap["vwap"] if bull else close < snap["vwap"],
         close < snap["vwap"] if bull else close > snap["vwap"],
         "price on trend side of VWAP", "price against VWAP")

    rising = snap["rsi"] > snap["rsi_prev"]
    mark("rsi_trend", rising if bull else not rising,
         (not rising) if bull else rising,
         "RSI momentum aligns", "RSI momentum opposes")

    if bull:
        mark("rsi_extreme", snap["rsi"] <= cfg.RSI_OVERBOUGHT, snap["rsi"] > cfg.RSI_OVERBOUGHT,
             "RSI not overbought", "RSI overbought")
        mark("bb", close <= snap["bb_mid"], close >= snap["bb_upper"],
             "price in lower half of Bollinger", "price at upper Bollinger band")
    else:
        mark("rsi_extreme", snap["rsi"] >= cfg.RSI_OVERSOLD, snap["rsi"] < cfg.RSI_OVERSOLD,
             "RSI not oversold", "RSI oversold")
        mark("bb", close >= snap["bb_mid"], close <= snap["bb_lower"],
             "price in upper half of Bollinger", "price at lower Bollinger band")

    if snap["volume"] > snap["volume_prev"]:
        checks["volume"] = 1
        notes.append("volume rising")
    else:
        checks["volume"] = 0

    net = sum(checks.values()) / len(checks) if checks else 0.0
    return {"score": round(net, 3), "agrees": net > 0, "checks": checks, "notes": notes}
