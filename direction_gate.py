"""Directional-confirmation gate — a structural bias is a hint, not a trade.

`mtf.combine()` proposes a direction from swing-structure bias alone; a
single fresh CHoCH or a stale swing read can propose SHORT while price sits
above EMA21/VWAP on both tradeable timeframes with rising RSI (the SPCX
shape). Before the direction is scored — let alone ranked or alerted — it
must survive this gate, which cross-checks it against:

  * EMA21+VWAP positioning on the setup (15M) and entry (5M) timeframes,
  * entry-timeframe RSI momentum (level and slope together),
  * genuine structural confirmation — BOS, displacement, a CONFIRMED
    liquidation sweep, or CHoCH + retest. A bias alone is never a
    confirmation, and neither is any single indicator.

Statuses:
  confirmed — nothing contradicts the direction            factor 1.0
  weak      — material contradiction / unconfirmed CHoCH    factor < 1.0
  conflict  — positioning AND momentum contradict with no confirming event;
              the classification itself is wrong -> NO_TRADE + heavy score
              penalty so the setup cannot rank.

Pure: reads already-computed indicator snapshots and detector dicts, no
market access, no look-ahead.
"""
from typing import Optional

import config


def _positioning(snap: Optional[dict]) -> Optional[str]:
    """'bull' / 'bear' when price is beyond BOTH EMA21 and VWAP, else 'mixed'.

    Requiring both keeps positioning honest: price above EMA but below VWAP
    is a mixed read, not bullish positioning."""
    if not snap:
        return None
    if snap["close"] > snap["ema21"] and snap["close"] > snap["vwap"]:
        return "bull"
    if snap["close"] < snap["ema21"] and snap["close"] < snap["vwap"]:
        return "bear"
    return "mixed"


def _entry_momentum(snap: Optional[dict]) -> Optional[str]:
    """Entry-TF RSI momentum: level and slope must agree.

    Rising AND >= 50 is bullish momentum (RSI 63.9 rising); falling AND <= 50
    is bearish; everything else (falling from 60, rising from 40) is neutral
    and makes no statement."""
    if not snap:
        return None
    if snap["rsi"] > snap["rsi_prev"] and snap["rsi"] >= 50:
        return "bull"
    if snap["rsi"] < snap["rsi_prev"] and snap["rsi"] <= 50:
        return "bear"
    return "neutral"


def confirm(direction: str, htf_snap: Optional[dict], setup_snap: Optional[dict],
            entry_snap: Optional[dict], structure: dict, liquidity: dict,
            mtf: dict, cfg=config) -> dict:
    """Cross-check a structure-proposed direction against tradeable evidence.

    Returns status ('confirmed' | 'weak' | 'conflict'), a multiplicative
    factor for setup_quality, and human-readable reasons for logging.
    """
    bull = direction in ("LONG", "BUY")
    opp = "bear" if bull else "bull"
    want_pos = "bull" if bull else "bear"
    want_dir = "bullish" if bull else "bearish"

    setup_pos = _positioning(setup_snap)
    entry_pos = _positioning(entry_snap)
    htf_pos = _positioning(htf_snap)
    momentum = _entry_momentum(entry_snap)

    # genuine confirmation events for the proposed direction
    bos = (structure.get("bos") or {}).get("dir") == want_dir
    disp = (structure.get("displacement") or {}).get("dir") == want_dir
    choch = (structure.get("choch") or {}).get("dir") == want_dir
    retest = (structure.get("retest") or {}).get("dir") == want_dir
    sweep_confirmed = bool(liquidity.get("long_ready") if bull
                           else liquidity.get("short_ready"))

    full_confirmation = bos and disp
    any_confirmation = bos or disp or sweep_confirmed or (choch and retest)

    positions = [p for p in (setup_pos, entry_pos) if p is not None]
    contra = sum(1 for p in positions if p == opp)
    contra_both = len(positions) == 2 and contra == 2
    mom_contra = momentum == opp

    status = "confirmed"
    factor = 1.0
    reasons: list[str] = []

    if contra_both and mom_contra and not (full_confirmation or sweep_confirmed):
        status = "conflict"
        factor = cfg.DIR_CONFLICT_FACTOR
        reasons.append(f"price on the {opp} side of EMA21/VWAP on BOTH tradeable "
                       f"TFs + {opp} entry momentum + no confirming event")
    elif contra_both and mom_contra:
        status = "weak"
        factor = cfg.DIR_CONFLICT_LATE_FACTOR
        reasons.append(f"{opp} positioning on both tradeable TFs with {opp} momentum — "
                       "structure is confirmed but the entry is being fought")
    elif contra_both:
        status = "weak"
        factor = cfg.DIR_POSITIONING_CONFLICT_FACTOR
        reasons.append(f"price on the {opp} side of EMA21/VWAP on BOTH tradeable TFs")
    elif contra == 1 and not any_confirmation:
        status = "weak"
        factor = cfg.DIR_POSITIONING_CONFLICT_FACTOR
        reasons.append(f"{opp} EMA21/VWAP positioning on one tradeable TF "
                       "with no confirming event")
    elif mom_contra and not any_confirmation:
        status = "weak"
        factor = cfg.DIR_MOMENTUM_CONFLICT_FACTOR
        reasons.append(f"{opp} entry momentum (RSI {entry_snap['rsi']:.0f} "
                       f"{'rising' if entry_snap['rsi'] > entry_snap['rsi_prev'] else 'falling'}) "
                       "without any structural confirmation")
    elif mom_contra:
        factor = cfg.DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR
        reasons.append(f"{opp} entry momentum — structure confirmed, reduced conviction")

    # A CHoCH-driven direction (bias against the standing trend) requires
    # post-CHoCH confirmation: displacement in the CHoCH's direction, a
    # retest of the broken level, a confirmed sweep, or positioning actually
    # flipping to the CHoCH's side. Without any of those, the CHoCH is just
    # a break — not a trade.
    if mtf.get("choch_reversal"):
        confirmed_after = (disp or retest or sweep_confirmed
                           or (setup_pos == want_pos and entry_pos == want_pos))
        if not confirmed_after:
            if status == "confirmed":
                status = "weak"
            factor = min(factor, cfg.DIR_UNCONFIRMED_CHOCH_FACTOR)
            reasons.append("direction rides an unconfirmed CHoCH — no displacement, "
                           "retest or positioning flip after the break yet")

    # HTF tape: the tradeable TFs can look clean while the 1H tape still sits
    # on the other side of EMA21/VWAP — an early counter-HTF bounce (or
    # breakdown). With confirming events it keeps reduced conviction; without
    # any it is penalized like a positioning conflict.
    if htf_pos == opp:
        htf_factor = (cfg.DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR if any_confirmation
                      else cfg.DIR_POSITIONING_CONFLICT_FACTOR)
        if htf_factor < factor:
            factor = htf_factor
            reasons.append(f"1H tape on the {opp} side of EMA21/VWAP"
                           + ("" if any_confirmation else " without confirming events"))

    # normalize: anything carrying a penalty is at most 'weak'
    if factor < 1.0 and status == "confirmed":
        status = "weak"

    return {
        "status": status,
        "factor": round(factor, 3),
        "reasons": reasons,
        "setup_positioning": setup_pos or "n/a",
        "entry_positioning": entry_pos or "n/a",
        "htf_positioning": htf_pos or "n/a",
        "entry_momentum": momentum or "n/a",
        "structure_confirmed": any_confirmation,
        "full_confirmation": full_confirmation,
    }
