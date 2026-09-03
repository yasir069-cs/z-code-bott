"""Decision core — the single source of truth for LONG / SHORT / NO_TRADE.

This is the brain Yasir specified: it orchestrates the priority hierarchy in
order and lets the PRIMARY layers decide, with indicators only confirming.

    1. market structure   (per timeframe)      -> mtf bias & direction
    2. support/resistance  (setup timeframe)
    3. liquidity & sweeps  (setup timeframe)
    4. price action        (entry timeframe)
    5. trendlines          (setup timeframe)
    6. multi-timeframe      -> direction + HTF alignment (mandatory)
    7. futures context      (OI / funding, safe-degrading)
    8. setup quality        -> 0-100, indicators bounded & secondary
    9. risk/reward gate      -> structural SL, opposing-zone target (mandatory)

`decide()` is pure over the frames handed to it (a dict keyed by timeframe
label; roles resolved from config.TF_HTF / TF_SETUP / TF_ENTRY). In live use the
frames already contain only closed candles; in backtest they are sliced to the
decision bar — so there is no look-ahead either way. The same function runs in
both, so a backtest replays exactly what live would have decided.

**NO_TRADE is a first-class result.** When multi-factor confluence is
insufficient, decide() returns NO_TRADE with the explicit reasons, never a
forced trade. Missing OI/funding degrade to data warnings, not failures.
"""
import logging
from typing import Optional

import config
import direction_gate
import futures_context
import liquidity as liquidity_mod
import market_structure
import mtf as mtf_mod
import price_action as price_action_mod
import risk_gate
import scoring
import setup_quality
import support_resistance
import trendlines as trendlines_mod
from indicators import compute_indicators

log = logging.getLogger("decision")

_MIN_FRAME = config.STRUCT_PIVOT_LEFT + config.STRUCT_PIVOT_RIGHT + 2


def _blank(decision: str, reasons: list, warnings: list, **extra) -> dict:
    """Assemble a structured result, filling absent evidence with None/defaults."""
    out = {
        "decision": decision,
        "direction": None,
        "setup_quality": 0.0,
        "confidence": 0.0,
        "primary": 0.0,
        "htf_bias": "neutral",
        "mtf": None,
        "structure": None,
        "sr": None,
        "liquidity": None,
        "price_action": None,
        "trendline": None,
        "futures": None,
        "indicators": None,
        "quality": None,
        "risk": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "rr": None,
        "no_trade_reasons": reasons,
        "data_warnings": warnings,
    }
    out.update(extra)
    return out


def decide(frames: dict, funding_rate: Optional[float] = None,
           oi_df=None, cfg=config, symbol: Optional[str] = None) -> dict:
    """Return the full structured decision for one symbol.

    `frames` maps timeframe labels (e.g. "1h", "15m", "5m") to OHLCV frames.
    Returns a dict whose "decision" is LONG, SHORT or NO_TRADE plus all the
    evidence and (for NO_TRADE) the exact reasons. `symbol` is optional and
    only used for penalty logging.
    """
    warnings: list[str] = []
    htf_df = frames.get(cfg.TF_HTF)
    setup_df = frames.get(cfg.TF_SETUP)
    entry_df = frames.get(cfg.TF_ENTRY)

    for label, df in ((cfg.TF_HTF, htf_df), (cfg.TF_SETUP, setup_df), (cfg.TF_ENTRY, entry_df)):
        if df is None or len(df) < _MIN_FRAME:
            warnings.append(f"insufficient_frame:{label}")
    if warnings:
        return _blank("NO_TRADE", ["insufficient_data"], warnings)

    # 1 + 6 — structure per timeframe, combined by role
    htf_struct = market_structure.analyze(htf_df, cfg)
    setup_struct = market_structure.analyze(setup_df, cfg)
    entry_struct = market_structure.analyze(entry_df, cfg)
    mtf = mtf_mod.combine(htf_struct, setup_struct, entry_struct, cfg)

    # 2-5, 7 — the remaining detectors (on their role frames)
    sr = support_resistance.analyze(setup_df, cfg)
    liq = liquidity_mod.analyze(setup_df, cfg)
    pa = price_action_mod.analyze(entry_df, cfg)
    tl = trendlines_mod.analyze(setup_df, cfg)
    futures = futures_context.interpret(oi_df, funding_rate, entry_df, cfg)
    warnings.extend(futures.get("warnings", []))

    entry = float(entry_df["close"].iloc[-1])
    atr = setup_struct["atr"] or sr["atr"]

    common = {"mtf": mtf, "structure": setup_struct, "sr": sr, "liquidity": liq,
              "price_action": pa, "trendline": tl, "futures": futures,
              "htf_bias": mtf["htf_bias"], "entry": round(entry, 8)}

    direction = mtf["direction"]
    if direction is None:
        return _blank("NO_TRADE", ["no_directional_bias"], warnings, **common)

    reasons: list[str] = []
    if cfg.MTF_REQUIRE_HTF_ALIGN and mtf["counter_htf"]:
        reasons.append("counter_htf")

    # 9 (indicators) fold into setup_quality as a bounded, secondary term
    snap = compute_indicators(setup_df)
    if snap is None:
        warnings.append("no_indicator_snapshot")
    indicator_conf = scoring.indicator_confirmation(snap, direction, cfg)

    # HTF exhaustion context: where price sits in the 1H range, the RSI LEVEL
    # (not slope), the Bollinger extreme and the last candle's volume. The
    # location/exhaustion penalties in setup_quality read this snapshot.
    htf_snap = compute_indicators(htf_df)
    if htf_snap is None:
        warnings.append("no_htf_indicator_snapshot")
    entry_snap = compute_indicators(entry_df)
    if entry_snap is None:
        warnings.append("no_entry_indicator_snapshot")

    # 6b — directional-confirmation gate: the structure-proposed direction
    # must survive the tradeable evidence (EMA21/VWAP positioning on 15M+5M,
    # entry momentum, genuine confirming events) BEFORE it is scored or
    # ranked. A bias alone — even a fresh CHoCH — is not a trade direction.
    dir_check = direction_gate.confirm(direction, htf_snap, snap, entry_snap,
                                       setup_struct, liq, mtf, cfg)
    if dir_check["status"] == "conflict":
        reasons.append("directional_conflict")
        log.info("%s: %s direction CONFLICT — %s", symbol or "setup", direction,
                 "; ".join(dir_check["reasons"]))

    # 9 — mandatory risk/reward gate, evaluated BEFORE quality so an
    # unreachable target drags the score (and therefore the ranking) down
    # instead of being a post-hoc veto after a high score was assigned.
    risk = risk_gate.evaluate(direction, entry, setup_struct, sr, atr, cfg)

    # 8 — setup quality (primary-weighted, indicators bounded, discounted by
    # direction contradiction + location / RSI / band / volume / RR penalties)
    quality = setup_quality.score(direction, setup_struct, sr, liq, pa, mtf, tl,
                                  futures, indicator_conf, cfg,
                                  htf_snap=htf_snap, risk=risk,
                                  direction_factor=dir_check["factor"],
                                  direction_reasons=dir_check["reasons"])
    if quality.get("penalties"):
        log.info("%s: %s penalized — %s (quality=%.1f, primary %.1f -> %.1f)",
                 symbol or "setup", direction, "; ".join(quality["penalties"]),
                 quality["quality"], quality.get("raw_primary", quality["primary"]),
                 quality["primary"])

    if not quality["primary_floor_ok"]:
        reasons.append("insufficient_primary_evidence")
    elif not quality["passes"]:
        reasons.append("low_setup_quality")
    if not risk["ok"]:
        reasons.extend(risk["reasons"])

    decision = direction if not reasons else "NO_TRADE"
    if decision != "NO_TRADE":
        log.info("%s decided quality=%.1f rr=%s", decision, quality["quality"], risk["rr"])

    return {
        "decision": decision,
        "direction": direction,
        "setup_quality": quality["quality"],
        "confidence": quality["quality"],
        "primary": quality["primary"],
        "htf_bias": mtf["htf_bias"],
        "mtf": mtf,
        "structure": setup_struct,
        "sr": sr,
        "liquidity": liq,
        "price_action": pa,
        "trendline": tl,
        "futures": futures,
        "indicators": indicator_conf,
        "quality": quality,
        "direction_check": dir_check,
        "risk": risk,
        "entry": round(entry, 8),
        "sl": risk["sl"],
        "tp": risk["tp"],
        "rr": risk["rr"],
        "no_trade_reasons": reasons,
        "data_warnings": warnings,
        # indicator snapshots by role, exposed so the LLM decision stage gets
        # the full structured data and a flipped direction can be re-scored
        # without re-fetching anything
        "snaps": {"1h": htf_snap, "15m": snap, "5m": entry_snap},
    }


def post_llm_validate(d: dict, direction: str, cfg=config) -> dict:
    """Hard safety validation of an LLM-chosen direction (post-decision).

    The LLM decides independently from factual evidence (it never saw a Python
    verdict); this function then applies ONLY the gates that can never be
    bypassed:

      * data validity (indicator snapshots present, entry computable)
      * mathematically invalid levels / stop width / minimum R:R
        (risk_gate, run for the CHOSEN direction)
      * the setup-quality threshold (the existing computation, run for the
        chosen direction — disagreement with the measured evidence shows up
        as a low score, never as an opinionated veto)

    Duplicate protection and the Telegram alert floor live in the scan
    pipeline (funnel + emission rule), unchanged.

    Returns a decision dict shaped like decide()'s: the chosen direction when
    every gate passes, else NO_TRADE with the exact gate reasons. Pure:
    reuses the evidence and snapshots stored in `d`.
    """
    if direction not in ("LONG", "SHORT"):
        out = dict(d)
        out.update({"decision": "NO_TRADE",
                    "no_trade_reasons": ["invalid_direction"]})
        return out

    snaps = d.get("snaps") or {}
    mtf = d.get("mtf") or {}
    structure = d.get("structure") or {}
    sr = d.get("sr") or {}
    liq = d.get("liquidity") or {}
    pa = d.get("price_action") or {}
    tl = d.get("trendline") or {}
    futures = d.get("futures") or {}
    entry = d.get("entry")
    atr = structure.get("atr") or (sr or {}).get("atr")
    htf_snap, setup_snap, entry_snap = snaps.get("1h"), snaps.get("15m"), snaps.get("5m")

    reasons: list[str] = []
    if entry is None or not htf_snap or not setup_snap:
        reasons.append("invalid_data")

    # indicator confirmation is direction-specific: recompute for the CHOSEN
    # direction instead of reusing the one computed for the core's direction
    indicator_conf = scoring.indicator_confirmation(setup_snap, direction, cfg)
    dir_check = direction_gate.confirm(direction, htf_snap, setup_snap,
                                       entry_snap, structure, liq, mtf, cfg)
    risk = risk_gate.evaluate(direction, entry, structure, sr, atr, cfg)
    quality = setup_quality.score(direction, structure, sr, liq, pa, mtf, tl,
                                  futures, indicator_conf, cfg,
                                  htf_snap=htf_snap, risk=risk,
                                  direction_factor=dir_check["factor"],
                                  direction_reasons=dir_check["reasons"])

    if not risk["ok"]:
        reasons.extend(risk["reasons"])
    if not quality["primary_floor_ok"]:
        reasons.append("insufficient_primary_evidence")
    elif not quality["passes"]:
        reasons.append("low_setup_quality")

    out = dict(d)
    out.update({
        "direction": direction,
        "decision": direction if not reasons else "NO_TRADE",
        "setup_quality": quality["quality"],
        "confidence": quality["quality"],
        "primary": quality["primary"],
        "quality": quality,
        "direction_check": dir_check,
        "risk": risk,
        "sl": risk["sl"],
        "tp": risk["tp"],
        "rr": risk["rr"],
        "no_trade_reasons": reasons,
    })
    return out
