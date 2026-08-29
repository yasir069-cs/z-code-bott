"""Crypto-futures context — priority-7 layer (contextual, safe-degrading).

Interprets Open Interest change vs price, plus funding, the way Yasir described:
with context, not fixed rules. The four OI/price quadrants:

    price up   + OI up   -> new longs opening        (bullish, high conviction)
    price up   + OI down -> shorts covering          (bullish, low conviction)
    price down + OI up   -> new shorts opening        (bearish, high conviction)
    price down + OI down -> longs unwinding           (bearish, low conviction)

Funding at an extreme flags a crowded side (squeeze risk one way, squeeze fuel
the other). Basis and long/short ratio are left as config-flagged slots for a
later phase.

CRITICAL: this layer degrades safely. Missing OI or funding is recorded as a
warning and simply drops out of the read — no value is ever fabricated, and the
decision core still returns a decision with the data-quality warning attached.
"""
import config
import ta_utils


def interpret(oi_df, funding_rate, df, cfg=config) -> dict:
    """Interpret OI/funding against recent price action. All inputs optional."""
    notes: list[str] = []
    warnings: list[str] = []

    oi_change = None
    if oi_df is not None and len(oi_df) >= 2 and "oi" in oi_df:
        first = float(oi_df["oi"].iloc[0])
        last = float(oi_df["oi"].iloc[-1])
        oi_change = ta_utils.safe_pct(last - first, first)
    else:
        warnings.append("open_interest_unavailable")

    price_change = None
    if df is not None and len(df) >= 2:
        n = min(len(df), cfg.OI_HISTORY_LIMIT)
        p0 = float(df["close"].iloc[-n])
        p1 = float(df["close"].iloc[-1])
        price_change = ta_utils.safe_pct(p1 - p0, p0)

    bias = "neutral"
    conviction = None
    if (oi_change is not None and price_change is not None
            and abs(oi_change) >= cfg.OI_CHANGE_MIN_PCT):
        if price_change > 0 and oi_change > 0:
            bias, conviction = "bullish", "high"
            notes.append("price up + OI up: new longs opening")
        elif price_change > 0 and oi_change < 0:
            bias, conviction = "bullish", "low"
            notes.append("price up + OI down: short covering (weak)")
        elif price_change < 0 and oi_change > 0:
            bias, conviction = "bearish", "high"
            notes.append("price down + OI up: new shorts opening")
        elif price_change < 0 and oi_change < 0:
            bias, conviction = "bearish", "low"
            notes.append("price down + OI down: long unwind (weak)")

    if funding_rate is None:
        warnings.append("funding_unavailable")
    elif funding_rate > cfg.FUNDING_EXTREME_LONG:
        notes.append(f"funding {funding_rate:+.4f} extreme positive: crowded longs")
    elif funding_rate < cfg.FUNDING_EXTREME_SHORT:
        notes.append(f"funding {funding_rate:+.4f} extreme negative: crowded shorts")

    available = oi_change is not None or funding_rate is not None

    return {
        "available": available,
        "oi_change_pct": None if oi_change is None else round(oi_change, 5),
        "price_change_pct": None if price_change is None else round(price_change, 5),
        "funding_rate": funding_rate,
        "bias": bias,
        "conviction": conviction,
        "notes": notes,
        "warnings": warnings,
    }
