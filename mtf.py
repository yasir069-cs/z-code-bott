"""Multi-timeframe combination — priority-6 layer (MANDATORY).

Roles are configurable (config.TF_HTF / TF_SETUP / TF_ENTRY); by default
1H drives the higher-timeframe bias, 15M validates the setup, and 5M provides
the entry trigger. Each input is a `market_structure.analyze()` result for that
timeframe.

The rule Yasir set: the HTF bias leads. A setup that the entry timeframe drives
straight against the HTF bias is rejected or reduced (MTF_REQUIRE_HTF_ALIGN /
the counter- and neutral-HTF penalties). When the HTF is itself neutral, the
setup timeframe stands in, but the setup is treated as lower-conviction.

Pure: only combines already-computed structure reads, no market access.
"""
import config

_BIAS_TO_DIR = {"bullish": "LONG", "bearish": "SHORT"}


def combine(htf: dict, setup: dict, entry: dict, cfg=config) -> dict:
    """Resolve a multi-timeframe direction and HTF alignment.

    Returns the per-TF biases, a proposed direction, and alignment flags the
    decision core uses to enforce MTF_REQUIRE_HTF_ALIGN and setup_quality uses
    to weight the MTF layer.
    """
    htf_bias = htf.get("bias", "neutral")
    setup_bias = setup.get("bias", "neutral")
    entry_bias = entry.get("bias", "neutral")

    # HTF leads; if it is neutral the setup timeframe stands in (lower conviction).
    primary = htf_bias if htf_bias != "neutral" else setup_bias
    direction = _BIAS_TO_DIR.get(primary)

    neutral_htf = htf_bias == "neutral"
    counter_htf = False
    choch_reversal = False
    penalty = 0
    notes: list[str] = []

    if direction is not None:
        want = "bullish" if direction == "LONG" else "bearish"
        opp_trend = "downtrend" if direction == "LONG" else "uptrend"
        # The bias agrees with the direction but the standing HTF trend does
        # not: the bias comes from a fresh CHoCH — an early, unconfirmed
        # reversal. Tradable information, but materially reduced conviction
        # until the trend itself flips or the reversal is confirmed.
        if htf_bias == want and htf.get("trend", "range") == opp_trend:
            choch_reversal = True
            penalty += cfg.MTF_TREND_CONFLICT_PENALTY
            notes.append(f"HTF bias ({htf_bias}) is a fresh CHoCH against the "
                         f"{htf.get('trend')} — reversal not yet trend-confirmed")
        # entry timeframe actively fighting a non-neutral HTF bias
        if not neutral_htf and entry_bias not in (want, "neutral"):
            counter_htf = True
            penalty += cfg.MTF_COUNTER_SETUP_PENALTY
            notes.append(f"entry TF ({entry_bias}) opposes HTF bias ({htf_bias})")
        if neutral_htf:
            penalty += cfg.MTF_NEUTRAL_HTF_PENALTY
            notes.append("HTF bias neutral — setup timeframe leads, reduced conviction")
    else:
        notes.append("no directional HTF/setup bias")

    aligned = direction is not None and not counter_htf

    return {
        "htf_bias": htf_bias,
        "setup_bias": setup_bias,
        "entry_bias": entry_bias,
        "direction": direction,
        "aligned": aligned,
        "counter_htf": counter_htf,
        "choch_reversal": choch_reversal,
        "neutral_htf": neutral_htf,
        "penalty": penalty,
        "notes": notes,
    }
