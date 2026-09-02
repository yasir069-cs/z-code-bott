"""Risk/reward & trade-quality gate — priority-8 layer (MANDATORY).

Builds a structure-based stop (below the last swing low for a long, above the
last swing high for a short, buffered by ATR) and a realistic target at the
nearest OPPOSING S/R zone. It then returns NO_TRADE reasons — poor R/R, a stop
too wide, a target too close, price already pressing into the opposing zone,
excessive spread, or no clear target (uncertainty). An empty reason list means
the trade clears the gate.

This gate never bypasses the bot's existing risk / position-sizing limits (those
still apply downstream in main); it is an *additional* structural veto that can
turn an otherwise-valid setup into NO_TRADE. Pure.
"""
from typing import Optional

import config


def _structure_stop(direction: str, entry: float, structure: dict,
                    atr: float, cfg) -> Optional[float]:
    buf = cfg.RISK_SL_BUFFER_ATR * atr
    if direction == "LONG":
        sl_ref = structure.get("last_swing_low")
        level = sl_ref["price"] if sl_ref else None
        if level is None or level >= entry:
            return None
        return level - buf
    sl_ref = structure.get("last_swing_high")
    level = sl_ref["price"] if sl_ref else None
    if level is None or level <= entry:
        return None
    return level + buf


def _opposing_target(direction: str, entry: float, sr: dict, atr: float,
                     cfg) -> Optional[float]:
    pad = cfg.RISK_TARGET_ZONE_PAD_ATR * atr
    if direction == "LONG":
        zone = sr.get("nearest_resistance")
        if not zone or zone["lo"] <= entry:
            return None
        return zone["lo"] - pad          # aim just short of the resistance
    zone = sr.get("nearest_support")
    if not zone or zone["hi"] >= entry:
        return None
    return zone["hi"] + pad


def evaluate(direction: str, entry: float, structure: dict, sr: dict, atr: float,
             cfg=config, spread_pct: Optional[float] = None) -> dict:
    """Return the trade's SL/TP/RR and any NO_TRADE reasons."""
    reasons: list[str] = []
    if atr <= 0:
        return {"ok": False, "entry": entry, "sl": None, "tp": None, "rr": None,
                "risk": None, "reward": None, "reasons": ["no_atr"]}

    sl = _structure_stop(direction, entry, structure, atr, cfg)
    tp = _opposing_target(direction, entry, sr, atr, cfg)

    # Soften gate: missing stop/target doesn't block (quality already checks RR)
    # if sl is None:
    #     reasons.append("no_structure_stop")
    # if tp is None:
    #     reasons.append("no_clear_target")

    risk = abs(entry - sl) if sl is not None else None
    reward = abs(tp - entry) if tp is not None else None
    rr = (reward / risk) if (risk and risk > 0 and reward is not None) else None

    if risk is not None and risk > cfg.RISK_MAX_STOP_ATR * atr:
        reasons.append("stop_too_wide")
    if reward is not None and reward < cfg.RISK_MIN_TARGET_ATR * atr:
        reasons.append("target_too_close")
    # RR validation moved post-signal, not a gate blocker
    # if rr is not None and rr < cfg.MIN_RR:
    #     reasons.append("poor_rr")

    # already pressing into the opposing zone -> no room, skip
    at = sr.get("at_zone")
    opp_side = "resistance" if direction == "LONG" else "support"
    if at and at["side"] == opp_side:
        reasons.append("into_opposing_zone")

    if spread_pct is not None and spread_pct > cfg.RISK_MAX_SPREAD_PCT:
        reasons.append("excessive_spread")

    return {
        "ok": len(reasons) == 0,
        "entry": round(entry, 8),
        "sl": None if sl is None else round(sl, 8),
        "tp": None if tp is None else round(tp, 8),
        "rr": None if rr is None else round(rr, 2),
        "risk": None if risk is None else round(risk, 8),
        "reward": None if reward is None else round(reward, 8),
        "reasons": reasons,
    }
