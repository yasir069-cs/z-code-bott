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
import config


def _want(direction: str) -> str:
    return "bullish" if direction == "LONG" else "bearish"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _structure_support(structure: dict, want: str) -> float:
    bias = structure.get("bias", "neutral")
    frac = 0.5 if bias == want else (0.15 if bias == "neutral" else 0.0)
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


def score(direction: str, structure: dict, sr: dict, liquidity: dict,
          price_action: dict, mtf: dict, trendlines: dict, futures: dict,
          indicator_conf: dict, cfg=config) -> dict:
    """Combine primary layers (0-100) with a bounded secondary indicator term."""
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
    primary = round(sum(contributions.values()), 2)

    net = indicator_conf.get("score", 0.0) if indicator_conf else 0.0
    if net >= 0:
        indicator = round(net * cfg.IND_CONFIRM_BONUS_MAX, 2)
    else:
        indicator = round(net * cfg.IND_CONFLICT_PENALTY_MAX, 2)

    quality = round(max(0.0, min(100.0, primary + indicator)), 2)
    primary_floor_ok = primary >= cfg.QUALITY_PRIMARY_FLOOR
    passes = primary_floor_ok and quality >= cfg.QUALITY_MIN

    return {
        "quality": quality,
        "primary": primary,
        "indicator": indicator,
        "fractions": {k: round(v, 3) for k, v in fracs.items()},
        "contributions": contributions,
        "primary_floor_ok": primary_floor_ok,
        "passes": passes,
    }
