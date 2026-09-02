"""Setup quality — primary evidence scored 0-100, indicators bounded & secondary.

This is where Yasir's inversion is enforced numerically. The seven PRIMARY
layers (structure, S/R, liquidity, price-action, MTF, trendline, futures) each
contribute a fraction of their configured weight; the weights sum to 100, so the
primary subtotal is itself a 0-100 score. Indicators are then applied ONLY as a
bounded adjustment (+IND_CONFIRM_BONUS_MAX / -IND_CONFLICT_PENALTY_MAX).

Two gates keep indicators in their place:
  * QUALITY_PRIMARY_FLOOR — if the primary subtotal is below the floor, no
    amount of indicator agreement can rescue the setup (indicators can't trigger
    a trade on their own),
  * QUALITY_MIN — the final gate the decision core checks.

Every `_support` helper reads a detector's own output dict and returns a [0,1]
alignment with the proposed direction. Pure.
"""
from typing import Optional

import logging

import config

log = logging.getLogger("setup_quality")


def _want(direction: str) -> str:
    return "bullish" if direction == "LONG" else "bearish"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _structure_support(structure: dict, want: str) -> float:
    bias = structure.get("bias", "neutral")
    trend = structure.get("trend", "range")
    opp_trend = "downtrend" if want == "bullish" else "uptrend"
    if bias == want and trend == opp_trend:
        # The bias is a fresh CHoCH against the standing trend: an early,
        # unconfirmed reversal — reduced base conviction (events still add).
        frac = 0.35
    elif bias == want:
        frac = 0.5
    elif bias == "neutral":
        frac = 0.15
    else:
        frac = 0.0
    bos = structure.get("bos")
    choch = structure.get("choch")
    disp = structure.get("displacement")
    retest = structure.get("retest")
    if bos and bos["dir"] == want:
        frac += 0.2
    if choch and choch["dir"] == want:
        frac += 0.25
    if disp and disp["dir"] == want:
        frac += 0.15
    if retest and retest.get("dir") == want:
        frac += 0.1
    return _clamp01(frac)


def _sr_support(sr: dict, direction: str, want: str) -> float:
    want_side = "support" if direction == "LONG" else "resistance"
    opp_side = "resistance" if direction == "LONG" else "support"
    frac = 0.3
    at = sr.get("at_zone")
    if at and at["side"] == want_side:
        frac += 0.4 + (0.15 if at.get("major") else 0.0)   # bouncing off favorable zone
    elif at and at["side"] == opp_side:
        frac -= 0.3                                          # pressing into opposing zone
    return _clamp01(frac)


def _liquidity_support(liq: dict, direction: str) -> float:
    ready = liq.get("long_ready") if direction == "LONG" else liq.get("short_ready")
    sweep = liq.get("buy_sweep") if direction == "LONG" else liq.get("sell_sweep")
    pool = liq.get("equal_lows") if direction == "LONG" else liq.get("equal_highs")
    if ready:
        frac = 0.9
    elif sweep is not None:
        frac = 0.4                                           # swept but unconfirmed
    else:
        frac = 0.2
    if pool:
        frac += 0.1
    return _clamp01(frac)


def _pa_support(pa: dict, want: str) -> float:
    sig = pa.get("signals", {"bullish": 0, "bearish": 0})
    net = sig.get("bullish", 0) - sig.get("bearish", 0)
    if want == "bearish":
        net = -net
    frac = _clamp01(0.4 + 0.2 * net)
    # a favorable breakout that lacks volume is explicitly "weak"
    bo = pa.get("breakout")
    if bo and bo["dir"] == want and not bo.get("volume_ok"):
        frac = min(frac, 0.6)
    if pa.get("absorption"):
        frac = min(frac, 0.7)
    return frac


def _mtf_support(mtf: dict) -> float:
    if mtf.get("counter_htf"):
        return 0.0
    if mtf.get("choch_reversal"):
        return 0.5      # fresh CHoCH against the standing HTF trend: early reversal
    if mtf.get("neutral_htf"):
        return 0.5
    return 1.0 if mtf.get("aligned") else 0.3


def _trendline_support(tl: dict, want: str) -> float:
    brk = tl.get("break")
    if brk and brk["dir"] == want:
        return 0.8
    for line in (tl.get("support_line"), tl.get("resistance_line")):
        if line and line.get("retest"):
            return 0.6
    if tl.get("channel"):
        return 0.5
    return 0.3


def _futures_support(fut: dict, want: str) -> float:
    if not fut.get("available"):
        return 0.5                                           # safe-degrade: neutral
    bias = fut.get("bias", "neutral")
    if bias == want:
        return 0.8 if fut.get("conviction") == "high" else 0.65
    if bias == "neutral":
        return 0.5
    return 0.2


def _rr_penalty(risk: Optional[dict], cfg=config) -> tuple[float, list[str]]:
    """Subtractive points when the reward side of the setup is not real.

    An unreachable target (RR undefined) or an RR below MIN_RR means the
    'quality' of the evidence is irrelevant — there is no tradable trade
    here — so the score is dragged down before anything ranks it."""
    if not risk:
        return 0.0, []
    reasons: list[str] = []
    pts = 0.0
    rr = risk.get("rr")
    if rr is None:
        pts += cfg.QUALITY_RR_NONE_PENALTY
        reasons.append("no achievable target (RR undefined)")
    elif rr < cfg.MIN_RR:
        miss = (cfg.MIN_RR - rr) / cfg.MIN_RR
        pts += cfg.QUALITY_RR_MISS_PENALTY * (0.5 + 0.5 * miss)
        reasons.append(f"RR={rr:.2f} below MIN_RR={cfg.MIN_RR:.1f}")
    return round(pts, 2), reasons


def _exhaustion_factor(direction: str, htf_snap: Optional[dict],
                       liquidity: dict, structure: dict,
                       cfg=config) -> tuple[float, list[str]]:
    """Multiplicative penalties for setups that look directional but have no
    room left: price at the wrong 1H range extreme, RSI exhausted INTO the
    move, beyond the Bollinger extreme, and a move with no volume behind it.

    Exemptions, per the strategy:
      * a CONFIRMED sweep (sweep + reclaim + confirmation candle) is a valid
        reversal setup at the extreme — its location penalty is halved;
      * strong continuation (fresh BOS *and* displacement in the direction)
        survives RSI/BB exhaustion — a genuine breakout can be extended.
    
    IMPROVED: penalties are accumulated separately, then combined with a floor
    (min 0.5) to prevent excessive stacking (0.55 * 0.90 * 0.70 = 0.35 was
    killing 65% of the score). This way one severe penalty doesn't cascade.
    """
    reasons: list[str] = []
    penalties: list[float] = []  # will combine with min floor
    
    if not htf_snap:
        return 1.0, reasons                      # safe-degrade: no 1H context

    want = _want(direction)
    strong_continuation = (
        (structure.get("bos") or {}).get("dir") == want
        and (structure.get("displacement") or {}).get("dir") == want
    )
    # a confirmed sweep in the trade's direction = valid reversal at the extreme
    ready = liquidity.get("long_ready") if direction == "LONG" else liquidity.get("short_ready")

    # --- 1. price location in the 1H range: the dominant exhaustion signal
    range_pos = htf_snap.get("range_pos")
    if range_pos is not None:
        # favourable location = room left in the trade's direction:
        # a LONG wants room ABOVE (price low in range), a SHORT room BELOW.
        loc = (1.0 - range_pos) if direction == "LONG" else range_pos
        if loc <= cfg.QUALITY_LOCATION_SEVERE_PCT:
            tier, label = cfg.QUALITY_LOCATION_SEVERE_FACTOR, "at the wrong 1H extreme"
        elif loc <= cfg.QUALITY_LOCATION_MODERATE_PCT:
            tier, label = cfg.QUALITY_LOCATION_MODERATE_FACTOR, "near the wrong 1H extreme"
        elif loc <= cfg.QUALITY_LOCATION_MILD_PCT:
            tier, label = cfg.QUALITY_LOCATION_MILD_FACTOR, "early in the 1H range"
        else:
            tier, label = None, ""
        if tier is not None:
            effective = 1.0 - (1.0 - tier) * (cfg.QUALITY_SWEEP_EXEMPT_FACTOR if ready else 1.0)
            penalties.append(effective)
            reasons.append(f"range_pos={range_pos:.2f} {label}"
                           + (" (confirmed sweep halves the penalty)" if ready and effective > tier else ""))

    # --- 2. RSI exhausted into the move (level, not slope: falling RSI at 25
    #        is exhaustion, not fresh bearishness)
    rsi = htf_snap.get("rsi")
    if rsi is not None and not strong_continuation:
        if direction == "SHORT" and rsi <= cfg.QUALITY_RSI_OVERSOLD:
            penalties.append(cfg.QUALITY_RSI_EXHAUSTION_FACTOR)
            reasons.append(f"RSI={rsi:.0f} oversold into the short")
        elif direction == "LONG" and rsi >= cfg.QUALITY_RSI_OVERBOUGHT:
            penalties.append(cfg.QUALITY_RSI_EXHAUSTION_FACTOR)
            reasons.append(f"RSI={rsi:.0f} overbought into the long")

    # --- 3. Bollinger extreme: context, never a signal by itself
    close = htf_snap.get("close")
    bb_lower, bb_upper = htf_snap.get("bb_lower"), htf_snap.get("bb_upper")
    if close is not None and not strong_continuation:
        if direction == "SHORT" and bb_lower is not None and close <= bb_lower:
            penalties.append(cfg.QUALITY_BB_EXTREME_FACTOR)
            reasons.append("at/below the lower Bollinger band")
        elif direction == "LONG" and bb_upper is not None and close >= bb_upper:
            penalties.append(cfg.QUALITY_BB_EXTREME_FACTOR)
            reasons.append("at/above the upper Bollinger band")

    # --- 4. volume behind the intended move
    vol = htf_snap.get("volume")
    vol_prev = htf_snap.get("volume_prev")
    vol_avg = htf_snap.get("volume_avg20")
    if vol is not None and vol_avg:
        if vol < cfg.PA_VOLUME_WEAK * vol_avg:
            penalties.append(cfg.QUALITY_WEAK_VOLUME_FACTOR)
            reasons.append("weak 1H volume vs avg20")
        elif vol_prev is not None and vol < vol_prev:
            penalties.append(cfg.QUALITY_DECLINING_VOLUME_FACTOR)
            reasons.append("declining 1H volume")

    # --- Combine penalties: instead of pure multiplication (0.55*0.90*0.70=0.35),
    #     use the minimum of all penalties with a floor at 0.5 to prevent
    #     excessive cascading. A single severe penalty doesn't kill everything.
    if not penalties:
        factor = 1.0
    else:
        # Take the worst (minimum) penalty, but never cut more than 50% total
        factor = max(0.5, min(penalties))

    return round(factor, 4), reasons


def score(direction: str, structure: dict, sr: dict, liquidity: dict,
          price_action: dict, mtf: dict, trendlines: dict, futures: dict,
          indicator_conf: dict, cfg=config,
          htf_snap: Optional[dict] = None,
          risk: Optional[dict] = None,
          direction_factor: float = 1.0,
          direction_reasons: Optional[list] = None) -> dict:
    """Combine primary layers (0-100) with a bounded secondary indicator term.

    The primary subtotal measures how strongly the evidence agrees with the
    direction; the direction factor (directional-confirmation gate) then
    discounts a direction that the tradeable evidence contradicts, and the
    exhaustion factor discounts it by WHERE the setup sits (1H range
    location, RSI level, band stretch, volume, achievable RR) — so neither a
    contradictory nor an exhausted setup can out-rank a valid one.
    """
    want = _want(direction)
    fracs = {
        "structure": _structure_support(structure, want),
        "sr": _sr_support(sr, direction, want),
        "liquidity": _liquidity_support(liquidity, direction),
        "price_action": _pa_support(price_action, want),
        "mtf": _mtf_support(mtf),
        "trendline": _trendline_support(trendlines, want),
        "futures": _futures_support(futures, want),
    }
    weights = {
        "structure": cfg.QUALITY_W_STRUCTURE,
        "sr": cfg.QUALITY_W_SR,
        "liquidity": cfg.QUALITY_W_LIQUIDITY,
        "price_action": cfg.QUALITY_W_PRICE_ACTION,
        "mtf": cfg.QUALITY_W_MTF,
        "trendline": cfg.QUALITY_W_TRENDLINE,
        "futures": cfg.QUALITY_W_FUTURES,
    }
    contributions = {k: round(weights[k] * fracs[k], 2) for k in weights}
    raw_primary = round(sum(contributions.values()), 2)

    # --- direction contradiction + exhaustion + RR: tradability, applied to
    #     the primary score before anything ranks or gates on it
    dir_reasons = list(direction_reasons or [])
    factor, exhaustion_reasons = _exhaustion_factor(direction, htf_snap,
                                                    liquidity, structure, cfg)
    rr_pts, rr_reasons = _rr_penalty(risk, cfg)
    total_factor = round(direction_factor * factor, 4)
    primary = round(raw_primary * total_factor, 2) if total_factor < 1.0 else raw_primary
    penalties = dir_reasons + exhaustion_reasons + rr_reasons

    net = indicator_conf.get("score", 0.0) if indicator_conf else 0.0
    if net >= 0:
        # indicator agreement also counts less on a contradicted/exhausted setup
        indicator = round(net * cfg.IND_CONFIRM_BONUS_MAX * total_factor, 2)
    else:
        indicator = round(net * cfg.IND_CONFLICT_PENALTY_MAX, 2)

    quality = round(max(0.0, min(100.0, primary + indicator - rr_pts)), 2)
    primary_floor_ok = primary >= cfg.QUALITY_PRIMARY_FLOOR
    passes = primary_floor_ok and quality >= cfg.QUALITY_MIN

    return {
        "quality": quality,
        "primary": primary,
        "raw_primary": raw_primary,
        "indicator": indicator,
        "fractions": {k: round(v, 3) for k, v in fracs.items()},
        "contributions": contributions,
        "direction_factor": round(direction_factor, 3),
        "exhaustion_factor": round(factor, 3),
        "rr_penalty": rr_pts,
        "penalties": penalties,
        "primary_floor_ok": primary_floor_ok,
        "passes": passes,
    }
