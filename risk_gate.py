"""Risk/reward & trade-quality gate — priority-8 layer (MANDATORY).

Builds a structure-based stop (below the last swing low for a long, above the
last swing high for a short, buffered by ATR) and a realistic target at the
nearest *reachable* OPPOSING S/R zone. It then returns NO_TRADE reasons — poor
R/R, a stop too wide, a target too close, price already pressing into the
opposing zone, excessive spread, or no clear target (uncertainty). An empty
reason list means the trade clears the gate.

Target selection reads the full zone list, not just `nearest_support` /
`nearest_resistance`: the nearest opposing zone is very often a WALL rather
than a target — it can straddle the entry (a zone is a *range*, typically
~1 ATR wide, so `mid` below price yet `hi` above it for a short) or sit a
fraction of an ATR away. Either used to mean "no trade" while a second zone
several ATR deeper sat unused in `sr["zones"]`. The gate now walks the
opposing-side zones nearest-first and takes the first one that is actually
beyond the entry and at least `RISK_MIN_TARGET_ATR * ATR` of reward away. When
no zone qualifies it still returns the closest valid level so `target_too_close`
fires as before, and it never returns a level on the wrong side of the entry
(an upside "target" for a short was possible before, and only `poor_rr`
happened to catch it). `RISK_TARGET_SCAN_ZONES = False` restores the
nearest-only behaviour.

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


def _opposing_zones(direction: str, sr: dict, cfg) -> tuple[list[dict], bool]:
    """Opposing-side zones the target may be taken from, nearest the entry first.

    Returns `(zones, scanned)`. With `RISK_TARGET_SCAN_ZONES` on (default) every
    zone the S/R layer detected on the opposing side is a candidate; with it off
    — or when the caller only supplied the `nearest_*` convenience pair — the
    legacy nearest-only behaviour is preserved exactly.
    """
    side = "resistance" if direction == "LONG" else "support"
    zones = sr.get("zones") or []
    if getattr(cfg, "RISK_TARGET_SCAN_ZONES", True) and zones:
        same = [z for z in zones if z.get("side") == side]
        if direction == "LONG":
            same.sort(key=lambda z: z.get("lo", float("inf")))
        else:
            same.sort(key=lambda z: -z.get("hi", float("-inf")))
        return same, True
    zone = sr.get("nearest_resistance") if direction == "LONG" else sr.get("nearest_support")
    return ([zone] if zone else []), False


def _target_level(direction: str, entry: float, zone: dict, pad: float) -> Optional[float]:
    """The TP one opposing zone offers, or None when that zone cannot be a target.

    A LONG aims just short of the zone's lower edge, a SHORT just above its upper
    edge; a zone whose far edge is not beyond the entry (it straddles price) is
    a wall, not a target — and taking it anyway used to place the TP on the wrong
    side of the entry, which only `poor_rr` happened to catch.
    """
    if direction == "LONG":
        lo = zone.get("lo")
        if lo is None or lo <= entry:
            return None
        level = lo - pad
        return level if level > entry else None
    hi = zone.get("hi")
    if hi is None or hi >= entry:
        return None
    level = hi + pad
    return level if level < entry else None


def _opposing_target(direction: str, entry: float, sr: dict, atr: float,
                     cfg) -> tuple[Optional[float], dict]:
    """Return `(target_level, info)` for the chosen opposing zone.

    `info` carries `zone` (compact "support@94.6" label for the alert), `skipped`
    (nearer opposing zones rejected as unusable) and `note` (the full sentence the
    LLM facts block reads). The nearest opposing zone wins when it offers at least
    `RISK_MIN_TARGET_ATR * ATR` of reward beyond the entry; otherwise the search
    continues one zone deeper. When nothing qualifies, the closest valid level is
    returned anyway so `target_too_close` still fires — the diagnostic the owner
    set, never a silently-accepted stretch target.
    """
    pad = cfg.RISK_TARGET_ZONE_PAD_ATR * atr
    need = cfg.RISK_MIN_TARGET_ATR * atr
    zones, scanned = _opposing_zones(direction, sr, cfg)
    if not zones:
        return None, {}

    fallback: Optional[float] = None
    fallback_info: dict = {}
    for skipped, zone in enumerate(zones):
        level = _target_level(direction, entry, zone, pad)
        if level is None:                     # straddles the entry / pad crosses it
            continue
        reward = abs(level - entry)
        side = zone.get("side", "?")
        mid = zone.get("mid")
        mid_s = f"{mid:.6g}" if isinstance(mid, (int, float)) else str(mid)
        label = f"{side}@{mid_s}"
        if reward >= need:
            info = {"zone": label, "skipped": skipped}
            if skipped:
                info["note"] = (f"{side} zone@{mid_s} "
                                f"({skipped} nearer opposing zone(s) unusable or too close)")
            else:
                info["note"] = f"{side} zone@{mid_s}"
            return level, info
        if fallback is None:
            far = f"{reward / atr:.1f} ATR" if atr > 0 else f"{reward:.6g}"
            note = f"nearest {side} zone@{mid_s} only {far} away"
            if scanned and len(zones) - skipped <= 1:
                note += " — no deeper opposing zone reachable"
            fallback = level
            fallback_info = {"zone": label, "skipped": skipped, "note": note}
    if fallback is not None:
        return fallback, fallback_info
    return None, ({"note": f"{len(zones)} opposing zone(s) sit inside/at the entry"}
                  if scanned else {})


def evaluate(direction: str, entry: float, structure: dict, sr: dict, atr: float,
             cfg=config, spread_pct: Optional[float] = None) -> dict:
    """Return the trade's SL/TP/RR and any NO_TRADE reasons."""
    reasons: list[str] = []
    if atr <= 0:
        return {"ok": False, "entry": entry, "sl": None, "tp": None, "rr": None,
                "risk": None, "reward": None, "target_zone": "", "target_skipped": 0,
                "target_note": "",
                "reasons": ["no_atr"]}

    sl = _structure_stop(direction, entry, structure, atr, cfg)
    tp, target_info = _opposing_target(direction, entry, sr, atr, cfg)

    if sl is None:
        reasons.append("no_structure_stop")
    if tp is None:
        reasons.append("no_clear_target")

    risk = abs(entry - sl) if sl is not None else None
    reward = abs(tp - entry) if tp is not None else None
    rr = (reward / risk) if (risk and risk > 0 and reward is not None) else None

    if risk is not None and risk > cfg.RISK_MAX_STOP_ATR * atr:
        reasons.append("stop_too_wide")
    if reward is not None and reward < cfg.RISK_MIN_TARGET_ATR * atr:
        reasons.append("target_too_close")
    if rr is not None and rr < cfg.MIN_RR:
        reasons.append("poor_rr")

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
        # which zone the target came from (and why a nearer one was skipped) —
        # surfaced in the alert and the LLM facts block, never a decision input
        "target_zone": target_info.get("zone", ""),
        "target_skipped": int(target_info.get("skipped") or 0),
        "target_note": target_info.get("note", ""),
        "reasons": reasons,
    }
