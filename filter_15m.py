"""15M setup-timeframe **feature extractor** (structure-first rework).

`confirm_15m` used to hard-gate the 1H direction on the 15M indicator checklist
(EMA21 / VWAP as vetoes, RSI / volume / Bollinger graded), dropping the coin on
any failure. Under the structure-first design it is now a thin feature extractor:
it reads the 15M market structure, reports whether that structure *aligns* with
the direction the funnel is considering, and attaches the indicator snapshot for
the bounded secondary layer. It never hard-rejects — the authoritative verdict
is made by ``decision.decide()`` across all three timeframes.

Kept name/signature so `main.py` and the scripts stay recognizable.
"""
import logging
from typing import Optional

import config
import market_structure
from indicators import compute_indicators

log = logging.getLogger("filter_15m")

_MIN_FRAME = config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT + 2

# Which structural bias "agrees" with a trade direction.
_WANT_BIAS = {"BUY": "bullish", "SELL": "bearish"}


def confirm_15m(df, direction: str) -> Optional[dict]:
    """Read the 15M setup-timeframe structure for `direction` (BUY|SELL).

    Returns a feature dict (never a pass/fail gate):

        direction  — the direction being evaluated
        bias       — 15M structural bias (bullish/bearish/ranging)
        aligned    — True when the 15M structure supports `direction`
        structure  — full market_structure.analyze output
        indicators — secondary-layer snapshot (may be None)

    Returns None only when the frame is too short to analyze.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")
    if df is None or len(df) < _MIN_FRAME:
        return None

    struct = market_structure.analyze(df)
    aligned = struct["bias"] == _WANT_BIAS[direction]
    log.debug("15M %s bias=%s aligned=%s", direction, struct["bias"], aligned)

    return {
        "direction": direction,
        "bias": struct["bias"],
        "aligned": aligned,
        "structure": struct,
        "indicators": compute_indicators(df),
    }
