"""Crypto Signal Bot — entry point + APScheduler timer.

Every 5 minutes between 18:00 and 23:00 IST (60 scans/session):

    STEP 1 full exchange scan (all USDT-M futures perps, $50M volume filter)
    STEP 2 1H context (graded confluence + liquidation sweep) -> fail: skip
    STEP 3 funding rate check (reject overleveraged market)
    STEP 4 duplicate guard (15 min per coin) — checked BEFORE 15M/5M fetches
    STEP 5 15M confirmation + 5M entry; deterministic decision core over
           1H/15M/5M (structure, S/R, liquidity, price action, MTF, risk)
    STEP 6 deterministic emission (critical path) — the Python core's verdict
           is what ships. Nothing here waits on, or is changed by, the model.
    STEP 7 AI OPINION AUDIT (background, off the critical path) — every
           shortlisted coin goes to the model with its FULL FACTUAL data (one
           batched request: indicators, location, sweep, liquidation, measured
           structure/S/R/MTF/futures facts, calculated SL/TP/RR, warnings) and
           NO Python verdict, NO_TRADE reasons or quality scores, so the answer
           is independent. It is recorded in ai_opinions.csv with an `agreement`
           grade (AGREE / DISAGREE / VETO_PROPOSED / SIGNAL_PROPOSED / NO_ANSWER)
           and can never change an emitted signal. Auditing is what makes the
           stage honest: the owner can read how often the model would have
           disagreed before ever handing it a veto.
    STEP 8-9 rank by setup_quality, PERSIST to signals_log.csv FIRST, then the
           Telegram alert (BUY/SELL at quality >= ALERT_QUALITY_MIN only,
           everything else log-only; a blocked setup logs a silent HOLD row at
           most once per HOLD_LOG_COOLDOWN_MIN)

    `--force-llm` is the ONE place where an LLM verdict is applied (post-LLM
    hard safety gates re-validate it): a test harness for what a real decision
    stage would look like, never part of the schedule.

Fetching is concurrent and batched per timeframe; a hard per-scan deadline
(config.SCAN_DEADLINE_SECONDS) guarantees a scan can never bleed into the next
5-minute slot: past it the AI stage is skipped and the Python fallback is used.

Outside 18:00-23:00 IST the scheduler runs nothing: zero activity,
zero market API calls, zero AI calls. The Telegram chat assistant,
liquidation websocket and news engine stay live 24/7.

Usage:
    python main.py            # production schedule (APScheduler, IST)
    python main.py --once     # DEMO/TEST: run one full scan cycle now
    python main.py --force-llm [--force-llm-top N]
                              # TEST ONLY: bypass all gates, send the top-N
                              # highest-quality setups straight to the LLM

Signals only — this bot NEVER places trades (no trading endpoints, no keys).
"""
import argparse
import csv
import logging
import time
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import alerts
import config
import decision as decision_core
import duplicate_guard
import filter_15m
import filter_1h
import filter_5m
import logger
import liquidation
import scan_coordinator
import scanner
import scoring
import telegram_bot
import ondemand
import ai_decision
from ai_decision import (AIDecisionError, nemotron_decision,
                         nemotron_decisions)
from fallback import explanation_fallback, fallback_decision

log = logging.getLogger("main")

# THE single-scan gate + shared guard + background AI worker. Every path
# (scheduled job, /scan_on session, --once) enters run_scan, which claims the
# coordinator before touching the network.
_coordinator = scan_coordinator.ScanCoordinator(duplicate_guard.DuplicateGuard)
# notify + budget_notice_fn wire the one-time "AI budget spent" Telegram line:
# the audit stage is cosmetic, but going silent on it should never be a mystery.
_ai_worker = scan_coordinator.AIOpinionWorker(
    ai_decision.llm_verdicts,
    notify=alerts.send_telegram_text,
    budget_notice_fn=ai_decision.budget_exhausted_notice,
)


def get_coordinator() -> scan_coordinator.ScanCoordinator:
    """Shared coordinator (telegram_bot /status, ondemand wiring, tests)."""
    return _coordinator


def _new_scan_id(now_ist: datetime) -> str:
    return now_ist.strftime("%Y%m%d-%H%M%S")


def _in_session(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return config.SESSION_START <= hhmm < config.SESSION_END


# decision.decide() speaks LONG/SHORT/NO_TRADE; the alert/log/guard layer speaks
# BUY/SELL/HOLD. Map at this single boundary so everything downstream is unchanged.
_SIGNAL_MAP = {"LONG": "BUY", "SHORT": "SELL", "NO_TRADE": "HOLD"}


def _decide(bundle: dict) -> dict:
    """AI decision with Python fallback (single-candidate).

    Kept for on-demand single lookups, tests and scripts/e2e_demo.py. The
    scheduled scan uses the batched path (nemotron_decisions) instead.
    """
    try:
        decision = nemotron_decision(bundle)
        log.info("%s: AI -> %s (confidence=%s)", bundle["symbol"], decision["signal"],
                 decision.get("confidence"))
        return decision
    except AIDecisionError as exc:
        log.warning("%s: AI unavailable (%s) -> Python fallback", bundle["symbol"], exc)
        return fallback_decision(bundle)


def _calc_leverage(atr: float, price: float) -> str:
    """Suggest leverage based on ATR as percentage of price."""
    if price <= 0:
        return "3x-5x"
    atr_pct = atr / price
    if atr_pct < config.LEV_ATR_LOW:
        return "10x-15x"
    elif atr_pct < config.LEV_ATR_HIGH:
        return "5x-8x"
    else:
        return "3x-5x"


def _calc_position_size(entry: float, sl: float) -> dict:
    """Calculate position size based on account balance and risk per trade."""
    risk_amount = config.ACCOUNT_BALANCE * (config.RISK_PER_TRADE_PCT / 100)
    risk_per_unit = abs(entry - sl) if sl else 0
    if risk_per_unit <= 0 or entry <= 0:
        return {"qty": 0, "value": 0, "risk_amount": risk_amount}
    qty = risk_amount / risk_per_unit
    value = qty * entry
    return {"qty": round(qty, 6), "value": round(value, 2), "risk_amount": round(risk_amount, 2)}


def _summarize_structure(struct: dict | None) -> str:
    """Compact one-cell structure label for the CSV / log."""
    if not struct:
        return ""
    label = f"{struct.get('trend', '?')}/{struct.get('bias', '?')}"
    if struct.get("choch"):
        label += f" CHoCH-{struct['choch'].get('dir', '')}"
    elif struct.get("bos"):
        label += f" BOS-{struct['bos'].get('dir', '')}"
    return label


def _fmt_zone(zone: dict | None) -> str:
    if not zone:
        return ""
    tag = "major" if zone.get("major") else "minor"
    return f"{tag}@{zone.get('mid')}"


def _target_zone_label(risk: dict | None, opp_zone: dict | None) -> str:
    """Which zone the TP came from, for the alert's "Target zone" line.

    The risk gate's own answer wins, because it may have skipped a nearer zone
    that straddles the entry: crediting `nearest_support`/`nearest_resistance`
    in that case would name a level the TP was never taken from.
    """
    risk = risk or {}
    zone = (risk.get("target_zone") or "").strip()
    skipped = int(risk.get("target_skipped") or 0)
    if not zone:
        return _fmt_zone(opp_zone)
    return f"{zone} (+{skipped} nearer zone(s) skipped)" if skipped else zone


def _aligned_sweep(liq: dict | None, signal: str) -> dict | None:
    """The sweep on the side that matters for `signal` (buy-side for a short,
    sell-side for a long — liquidity.analyze keys them by trade direction)."""
    if not liq:
        return None
    if signal == "BUY":
        return liq.get("buy_sweep")
    if signal == "SELL":
        return liq.get("sell_sweep")
    return None


def _summarize_liquidity(liq: dict | None, signal: str) -> str:
    if not liq:
        return ""
    sweep = _aligned_sweep(liq, signal)
    if sweep:
        return f"{sweep.get('side', '')}:{'confirmed' if sweep.get('confirmed') else 'unconfirmed'}"
    ready = liq.get("long_ready") if signal == "BUY" else liq.get("short_ready")
    return "ready" if ready else "none"


def _build_sig(symbol: str, d: dict, signal: str, snap5: dict | None,
               rsi_bounce: bool, funding_rate, last_price: float,
               ai_used: bool = False, ai_reason: str | None = None) -> dict:
    """Assemble the alert/log record from a finished decision.decide() result.

    The deterministic core owns the verdict and every level (entry/SL/TP/RR/
    setup_quality); this only formats them plus the structured multi-factor
    evidence for the Telegram alert and the CSV. The reason text is the local
    structured explanation — when the LLM explanation layer is wired it will
    replace this string (and set ai_used=True); it can never change the verdict.
    """
    struct = d.get("structure") or {}
    sr = d.get("sr") or {}
    liq = d.get("liquidity") or {}
    liquidation_summary = liquidation.get_summary(symbol, current_price=last_price, sr=sr)

    sweep = _aligned_sweep(liq, signal)
    quality = float(d.get("setup_quality") or 0.0)
    # Sweep still governs FULL confidence (owner's rule): no confirmed sweep ->
    # capped just below the HIGH band, and the alert says so.
    confidence = scoring.apply_sweep_confidence_cap(quality, sweep)

    entry = d.get("entry")
    atr = float(struct.get("atr") or 0.0)
    leverage = _calc_leverage(atr, float(last_price or entry or 0.0))
    pos = _calc_position_size(entry or 0.0, d.get("sl"))

    opp_zone = (sr.get("nearest_resistance") if signal == "BUY"
                else sr.get("nearest_support") if signal == "SELL" else None)

    ind_block = {}
    if snap5:
        vol_avg = snap5.get("volume_avg20") or 0.0
        ind_block = {
            "rsi_now": snap5.get("rsi"),
            "rsi_prev": snap5.get("rsi_prev"),
            "price_above_ema": snap5.get("close", 0) > snap5.get("ema21", 0),
            "price_above_vwap": snap5.get("close", 0) > snap5.get("vwap", 0),
            "volume_ratio": (snap5["volume"] / vol_avg) if vol_avg > 0 else 0.0,
        }

    sweep_type = ("bullish" if signal == "BUY" else "bearish") if sweep else ""

    reason = explanation_fallback(d)
    if ai_reason:
        reason = f"{ai_reason} | {reason}"
    # Liquidation context joins the reason text only when the websocket
    # summary is available AND the window it quotes is actually configured —
    # a missing window must not fabricate a "0 events in 1h" reading.
    one_hour_liq = (liquidation_summary.get("windows") or {}).get("1h")
    if liquidation_summary.get("available") and one_hour_liq:
        reason += (f" Liquidations: {one_hour_liq.get('long_count', 0)} long / "
                   f"{one_hour_liq.get('short_count', 0)} short events in 1h.")
    return {
        "coin": symbol,
        "signal": signal,
        "entry": entry,
        "SL": d.get("sl"),
        "TP": d.get("tp"),
        "RR": d.get("rr"),
        "leverage": leverage,
        "position_size": pos["value"],
        "funding_rate": funding_rate,
        "confidence": round(float(confidence), 1),
        "confluence": round(quality, 1),
        "score_1h": None,          # per-TF indicator scores retired (structure decides)
        "score_15m": None,
        "score_5m": None,
        # dict for the Telegram alert; logger coerces it to a scalar for the CSV
        "sweep": {"detected": sweep is not None, "type": sweep_type,
                  "age": (sweep or {}).get("age_candles")},
        "sweep_age": (sweep or {}).get("age_candles", ""),
        "rsi_bounce": bool(rsi_bounce),
        "rsi_bounce_detected": bool(rsi_bounce),
        "indicators": ind_block,
        "reason": reason,
        "ai_used": bool(ai_used),
        # --- structure-first decision evidence (new CSV columns) ---
        "decision": d.get("decision"),
        "setup_quality": round(quality, 1),
        "htf_bias": d.get("htf_bias"),
        "structure": _summarize_structure(struct),
        "sr_zone": _target_zone_label(d.get("risk"), opp_zone),
        "liquidity": _summarize_liquidity(liq, signal),
        "no_trade_reason": ", ".join(d.get("no_trade_reasons") or []),
        "data_warnings": ", ".join(d.get("data_warnings") or []),
        "liquidation": liquidation_summary,
    }


def _build_llm_bundle(cand: dict, d: dict, last_price) -> dict | None:
    """Assemble the AI-candidate bundle for a finished decision (--force-llm).

    The LLM prompt reads the three indicator snapshots plus the 1H sweep; a
    coin whose indicator warm-up failed returns None and is skipped rather
    than crashing the batched prompt.
    """
    feat_1h = cand.get("feat_1h") or {}
    ind_1h = feat_1h.get("indicators")
    snap5 = (cand.get("feat_5m") or {}).get("indicators")
    if not ind_1h or not snap5:
        return None
    direction = _SIGNAL_MAP.get(d.get("direction")) or cand.get("direction")
    if direction not in ("BUY", "SELL"):
        return None
    current_price = last_price or snap5.get("close") or d.get("entry")
    if not current_price:
        return None
    return {
        "symbol": cand["symbol"],
        "direction": direction,
        "current_price": float(current_price),
        "entry_price": float(d.get("entry") or snap5.get("close")),
        "ind_1h": ind_1h,
        "sweep": feat_1h.get("sweep"),
        "ind_15m": (cand.get("feat_15m") or {}).get("indicators"),
        "ind_5m": snap5,
        "liquidation": liquidation.get_summary(cand["symbol"],
                                               current_price=current_price,
                                               sr=d.get("sr")),
    }


def _build_decision_bundle(cand: dict, d: dict, snap5: dict | None,
                           last_price) -> dict | None:
    """Assemble the FULL structured-data bundle for the LLM decision stage.

    Everything the pipeline collected for this shortlisted coin: the three
    indicator snapshots, the 1H sweep, the websocket liquidation summary, and
    the deterministic core's complete output (direction, quality, penalties,
    risk levels, no-trade reasons). The model gets the whole picture BEFORE
    any final gate rejection is applied to it."""
    snaps = d.get("snaps") or {}
    ind_1h, ind_15m = snaps.get("1h"), snaps.get("15m")
    snap5 = snap5 or snaps.get("5m")
    if not ind_1h or not snap5:
        return None
    current_price = last_price or snap5.get("close") or d.get("entry")
    if not current_price:
        return None
    det = dict(d)
    det["funding_rate"] = cand.get("fr")
    return {
        "symbol": cand["symbol"],
        "funnel_direction": cand["direction"],
        "current_price": float(current_price),
        "entry_price": float(d.get("entry") or snap5.get("close")),
        "ind_1h": ind_1h,
        "ind_15m": ind_15m,
        "ind_5m": snap5,
        "sweep": (cand.get("feat_1h") or {}).get("sweep"),
        "liquidation": liquidation.get_summary(cand["symbol"],
                                               current_price=current_price,
                                               sr=d.get("sr")),
        "deterministic": det,
    }


def _apply_llm_verdict(d: dict, verdict: dict | None,
                       symbol: str = "") -> tuple[dict, str, bool, str | None]:
    """Apply the LLM's independent decision to one candidate's evidence.

    Returns (decision, signal, ai_used, ai_reason). The model chose
    LONG/SHORT/NO_TRADE from factual evidence (it never saw a Python
    verdict). Its choice then faces the hard safety gates via
    decision.post_llm_validate — data validity, invalid levels, stop width,
    minimum R:R, quality threshold. An absent verdict (AI unavailable /
    budget out) leaves the deterministic result untouched."""
    if not verdict:
        return d, _SIGNAL_MAP.get(d.get("decision"), "HOLD"), False, None

    verdict_signal = verdict.get("signal")
    if verdict_signal == "NO_TRADE":
        out = dict(d)
        out["decision"] = "NO_TRADE"
        reasons = list(out.get("no_trade_reasons") or [])
        if "llm_no_trade" not in reasons:
            reasons.append("llm_no_trade")
        out["no_trade_reasons"] = reasons
        return out, "HOLD", True, verdict.get("reason")

    want = "LONG" if verdict_signal == "LONG" else \
           "SHORT" if verdict_signal == "SHORT" else None
    if want is None:                      # unusable verdict -> deterministic
        return d, _SIGNAL_MAP.get(d.get("decision"), "HOLD"), False, None

    # The LLM confirmed a direction the deterministic core already approved:
    # nothing new to validate, the core's own gates passed for it.
    if want == d.get("direction") and d.get("decision") == want:
        return d, _SIGNAL_MAP.get(d.get("decision"), "HOLD"), True, verdict.get("reason")

    # The LLM chose a direction Python did not approve (a flip, or the same
    # direction the core rejected): the hard safety gates decide, not opinions.
    out = decision_core.post_llm_validate(d, want)
    return out, _SIGNAL_MAP.get(out.get("decision"), "HOLD"), True, verdict.get("reason")


def _hold_reason_key(sig: dict) -> str:
    """Dedupe signature for a silent NO_TRADE row: verdict + blocking reasons.

    Two identical rejections five minutes apart are the same information; a coin
    whose blocking reason changed (`poor_rr` -> `counter_htf`) is a setup moving,
    and that must stay in the log.
    """
    return f"{sig.get('decision')}|{sig.get('no_trade_reason')}"


def _persist(sig: dict, summary: dict) -> bool:
    """Append one row to signals_log.csv, containing any disk failure.

    The write happens BEFORE the Telegram send, so it must never take the rest of
    the scan down with it: one unwritable row used to abort the persist loop and
    cost every remaining candidate its alert. The error is counted and logged.
    """
    try:
        logger.log_signal(sig)
        return True
    except OSError as exc:
        summary["log_failed"] = summary.get("log_failed", 0) + 1
        log.error("signal log write failed for %s (%s) — continuing",
                  sig.get("coin"), exc)
        return False


def _emission_kind(signal: str, quality: float) -> str:
    """Post-LLM emission rule (owner's tier system): HOLD log-only; BUY/SELL
    below ALERT_QUALITY_MIN (50) log-only; at/above it alert + log + cooldown.
    The alert's tier (NORMAL 50-60 / HIGH 60-70 / STRONG 70+) is labelled by
    alerts._conf_label from the computed confidence."""
    if signal == "HOLD":
        return "hold"
    return "alert" if quality >= config.ALERT_QUALITY_MIN else "log_only"


def run_force_llm(exchange, top_n: int = 3) -> dict:
    """TESTING ONLY: bypass every gate and send the top-N setups to the LLM.

    run_scan() turns any gate failure into NO_TRADE and the LLM is never
    consulted. This path deliberately skips that: every coin that reaches the
    decision core is ranked by setup_quality and the best `top_n` are handed
    to the batched LLM call regardless of their gate reasons. Results are
    LOGGED ONLY — no Telegram alerts, no signals_log.csv rows, no duplicate
    cooldown — so production output stays clean while the LLM integration is
    being evaluated.
    """
    deadline = time.monotonic() + config.SCAN_DEADLINE_SECONDS
    tickers = exchange.fetch_tickers()
    tickers_last = {s: t.get("last") for s, t in tickers.items()
                    if isinstance(t, dict) and t.get("last")}
    funding_rates = scanner.fetch_funding_rates(exchange)

    symbols = scanner.get_active_usdt_symbols(exchange, tickers)
    frames_1h = scanner.fetch_timeframe_batch(symbols, "1h", deadline=deadline)
    candidates = []
    for symbol, df_1h in frames_1h.items():
        feat = filter_1h.analyze_1h(df_1h)
        if feat is None or feat["direction"] is None:
            continue
        candidates.append({"symbol": symbol, "direction": feat["direction"],
                           "df_1h": df_1h, "feat_1h": feat,
                           "fr": funding_rates.get(symbol)})

    frames_15m = scanner.fetch_timeframe_batch([c["symbol"] for c in candidates], "15m",
                                               deadline=deadline)
    with_15m = []
    for c in candidates:
        df_15m = frames_15m.get(c["symbol"])
        if df_15m is None:
            continue
        c["df_15m"] = df_15m
        c["feat_15m"] = filter_15m.confirm_15m(df_15m, c["direction"])
        with_15m.append(c)

    frames_5m = scanner.fetch_timeframe_batch([c["symbol"] for c in with_15m], "5m",
                                              deadline=deadline)
    decided = []
    for c in with_15m:
        df_5m = frames_5m.get(c["symbol"])
        if df_5m is None:
            continue
        c["feat_5m"] = filter_5m.entry_5m(df_5m, c["direction"])
        frames = {config.TF_HTF: c["df_1h"], config.TF_SETUP: c["df_15m"],
                  config.TF_ENTRY: df_5m}
        d = decision_core.decide(frames, funding_rate=c["fr"], symbol=c["symbol"])
        decided.append((float(d.get("setup_quality") or 0.0), c, d))

    if not decided:
        log.info("FORCE-LLM: no coin reached the decision core")
        return {"decided": 0, "sent_to_llm": 0, "answered": 0}

    decided.sort(key=lambda t: t[0], reverse=True)
    ranked = []
    for quality, c, d in decided:
        if len(ranked) >= top_n:
            break
        bundle = _build_llm_bundle(c, d, tickers_last.get(c["symbol"]))
        if bundle is None:
            log.debug("FORCE-LLM: %s skipped — indicator snapshot incomplete", c["symbol"])
            continue
        ranked.append((quality, bundle))

    log.info("FORCE-LLM: %d coins decided; sending top %d to the LLM: %s",
             len(decided), len(ranked),
             ", ".join(f"{b['symbol']}(q={q:.0f})" for q, b in ranked) or "none")

    results: dict = {}
    if ranked:
        try:
            results = nemotron_decisions([b for _, b in ranked], deadline=deadline)
        except AIDecisionError as exc:
            log.warning("FORCE-LLM: AI unavailable (%s) — no answers this run", exc)

    for quality, bundle in ranked:
        r = results.get(bundle["symbol"])
        if r is None:
            log.info("FORCE-LLM %s (quality=%.0f): NO AI ANSWER", bundle["symbol"], quality)
            continue
        log.info("FORCE-LLM %s (quality=%.0f): AI -> %s entry=%s sl=%s tp=%s rr=%s "
                 "confidence=%s | %s",
                 bundle["symbol"], quality, r.get("signal"), r.get("entry"),
                 r.get("sl"), r.get("tp"), r.get("rr"), r.get("confidence"),
                 r.get("reason"))
    return {"decided": len(decided), "sent_to_llm": len(ranked),
            "answered": len(results)}


def run_scan(exchange, guard: duplicate_guard.DuplicateGuard | None = None,
             tickers: dict | None = None,
             funding_rates: dict | None = None,
             force: bool = False) -> dict:
    """One full scan cycle, coordinated: only ONE scan may run at a time.

    `force=True` runs it regardless of the session window (DEMO/TEST mode) —
    production scans are only ever scheduled inside 18:00-23:00 IST. If
    another scan is active (scheduled + on-demand overlap), this call skips
    immediately and says so.

    Critical path = universe -> funding -> OHLCV -> deterministic decision ->
    risk gate -> persist -> Telegram, all stage-timed and deadline-bounded.
    AI runs in the BACKGROUND afterwards (audit-only, never blocks the scan
    and never changes an already-persisted signal).

    *tickers* is the raw dict from exchange.fetch_tickers(); when provided it
    is forwarded to the scanner so fetch_tickers() is called once per cycle.
    *funding_rates* is the {symbol: rate} dict for funding-rate filtering.
    """
    now_ist = datetime.now(config.TZ)
    scan_id = _new_scan_id(now_ist)

    if not force and not _in_session(now_ist):
        log.info("Outside active session (now %s IST) - zero activity",
                 now_ist.strftime("%H:%M"))
        return {"scanned": 0}

    # THE single-scan gate: scheduled, /scan_on and --once all pass here.
    if not _coordinator.try_begin(scan_id):
        log.warning("Scan %s skipped — scan %s already active", scan_id,
                    _coordinator.scan_id)
        return {"scanned": 0, "skipped": "scan already active"}
    try:
        return _run_scan_locked(exchange, guard, tickers, funding_rates,
                                now_ist, scan_id)
    except Exception as exc:
        # the scan slot must never stay claimed because one scan blew up
        log.error("Scan %s failed: %s: %s", scan_id, type(exc).__name__, exc)
        _coordinator.end({"error": str(exc)[:200], "scanned": 0})
        return {"scanned": 0, "error": str(exc)[:200]}


def _run_scan_locked(exchange, guard, tickers, funding_rates,
                     now_ist, scan_id) -> dict:
    """The scan body — only reached with the coordinator slot claimed."""
    deadline = time.monotonic() + config.SCAN_DEADLINE_SECONDS
    # one shared guard across every scan path (was: per-path instances)
    guard = guard or _coordinator.get_guard()

    with _coordinator.stage("universe"):
        symbols = scanner.get_active_usdt_symbols(exchange, tickers)
    tickers_last = {}
    if tickers:
        tickers_last = {s: t.get("last") for s, t in tickers.items()
                        if isinstance(t, dict) and t.get("last")}
    if funding_rates is None:
        funding_rates = {}
    summary = {"scanned": len(symbols), "pass_1h": 0, "pass_15m": 0, "pass_5m": 0,
               "funding_rejected": 0, "duplicates": 0, "gate_rejected": 0,
               "signals": 0, "log_only": 0, "holds": 0, "hold_deduped": 0,
               "telegram_failed": 0, "log_failed": 0, "ai_status": "QUEUED"}

    # --- STEP 1+2: batch-fetch 1H for every symbol; structure funnel ---
    with _coordinator.stage("ohlcv_1h"):
        frames_1h = scanner.fetch_timeframe_batch(symbols, "1h", deadline=deadline)
    candidates = []  # survivors of the 1H structure funnel + funding + duplicate guard
    with _coordinator.stage("filter_1h"):
        for symbol, df_1h in frames_1h.items():
            feat = filter_1h.analyze_1h(df_1h)
            if feat is None or feat["direction"] is None:
                continue                   # ranging / undecided HTF -> not worth LTF fetch
            summary["pass_1h"] += 1
            direction = feat["direction"]

            # STEP 3: funding rate — reject when the market is overleveraged
            fr = funding_rates.get(symbol)
            if fr is not None:
                if direction == "BUY" and fr > config.FUNDING_RATE_MAX_LONG:
                    log.info("%s: BUY rejected — funding %.4f%% > %.4f%% (longs overleveraged)",
                             symbol, fr * 100, config.FUNDING_RATE_MAX_LONG * 100)
                    summary["funding_rejected"] += 1
                    continue
                if direction == "SELL" and fr < config.FUNDING_RATE_MIN_SHORT:
                    log.info("%s: SELL rejected — funding %.4f%% < %.4f%% (shorts overleveraged)",
                             symbol, fr * 100, config.FUNDING_RATE_MIN_SHORT * 100)
                    summary["funding_rejected"] += 1
                    continue

            # STEP 4: duplicate guard FIRST — a cooling-down coin costs zero LTF fetches
            if guard.is_duplicate(symbol, now_ist):
                summary["duplicates"] += 1
                log.debug("%s: duplicate within cooldown - skipped before LTF fetch", symbol)
                continue

            # feat_1h travels with the candidate: it carries the 1H sweep and the
            # indicator snapshot the LLM bundle and the alert both read. Dropping
            # it here made every scheduled scan tell the model "sweep: none
            # detected" even when filter_1h had found one.
            candidates.append({"symbol": symbol, "direction": direction,
                               "df_1h": df_1h, "feat_1h": feat, "fr": fr})

    # --- STEP 5a: batch-fetch 15M (setup TF) for the survivors ---
    with _coordinator.stage("ohlcv_15m"):
        frames_15m = scanner.fetch_timeframe_batch(
            [c["symbol"] for c in candidates], "15m", deadline=deadline)
    with_15m = []
    for c in candidates:
        df_15m = frames_15m.get(c["symbol"])
        if df_15m is None:
            continue
        summary["pass_15m"] += 1
        c["df_15m"] = df_15m
        with_15m.append(c)

    # --- STEP 5b: batch-fetch 5M (entry TF); run the decision core over all 3 TFs ---
    with _coordinator.stage("ohlcv_5m"):
        frames_5m = scanner.fetch_timeframe_batch(
            [c["symbol"] for c in with_15m], "5m", deadline=deadline)
    analysed = []   # (d, cand, snap5, rsi_bounce, last_price) pre-LLM
    with _coordinator.stage("oi"), _coordinator.stage("decision"):
        for c in with_15m:
            df_5m = frames_5m.get(c["symbol"])
            if df_5m is None:
                continue
            summary["pass_5m"] += 1
            symbol, direction, fr = c["symbol"], c["direction"], c["fr"]

            # entry-TF features: the 5M snapshot + RSI-bounce badge (never gates)
            entry_feat = filter_5m.entry_5m(df_5m, direction)
            snap5 = (entry_feat or {}).get("indicators")
            rsi_bounce = bool((entry_feat or {}).get("rsi_bounce_detected"))

            # crypto-futures context: OI history (safe-degrade to None past the
            # deadline; bounded per-request by the ccxt timeout + retries)
            oi_df = None
            if config.OI_FETCH_ENABLED and time.monotonic() < deadline:
                oi_df = scanner.fetch_open_interest_history(
                    exchange, symbol, config.OI_HISTORY_TIMEFRAME,
                    config.OI_HISTORY_LIMIT)

            # THE decision evidence: deterministic core over HTF/setup/entry frames.
            frames = {config.TF_HTF: c["df_1h"], config.TF_SETUP: c["df_15m"],
                      config.TF_ENTRY: df_5m}
            d = decision_core.decide(frames, funding_rate=fr, oi_df=oi_df,
                                     symbol=symbol)
            last_price = ((tickers_last or {}).get(symbol) or d.get("entry")
                          or (snap5 or {}).get("close"))
            analysed.append((d, c, snap5, rsi_bounce, last_price))

            # per-symbol diagnostics live at DEBUG — the INFO funnel stays
            # one concise summary per scan
            _struct = d.get("structure") or {}
            _rr = d.get("rr")
            log.debug("PRE-LLM %s: funnel=%s structure=%s/%s htf=%s quality=%.0f rr=%s",
                      symbol, direction, _struct.get("trend", "n/a"),
                      _struct.get("bias", "n/a"), d.get("htf_bias", "n/a"),
                      d.get("setup_quality") or 0.0,
                      f"{_rr:.2f}" if _rr is not None else "n/a")

    if not analysed:
        log.info("Scan %s complete: %s", scan_id, summary)
        _coordinator.end(summary)
        return summary

        # --- STEP 6: AI primary decision (critical path) ---
    # AI decides LONG/SHORT/NO_TRADE from collected data.
    # Python deterministic decision is fallback only (AI unavailable/timeout/budget).
    decided = []
    ai_records = []

    # Build bundles for all analysed candidates
    bundles_for_ai = []
    analysed_map = {}
    for d, c, snap5, rsi_bounce, last_price in analysed:
        symbol = c["symbol"]
        bundle = _build_decision_bundle(c, d, snap5, last_price)
        if bundle:
            bundles_for_ai.append(bundle)
            analysed_map[symbol] = (d, c, snap5, rsi_bounce, last_price)

    # Call AI for all candidates in one batch
    ai_verdicts = {}
    if config.LLM_DECISION_ENABLED and bundles_for_ai:
        try:
            raw_verdicts = ai_decision.llm_verdicts(bundles_for_ai, deadline=deadline)
            for symbol, verdict in (raw_verdicts or {}).items():
                ai_verdicts[symbol] = verdict
        except Exception as exc:
            log.warning("AI batch call failed — falling back to Python for all: %s", exc)

    for d, c, snap5, rsi_bounce, last_price in analysed:
        symbol, fr = c["symbol"], c["fr"]

        verdict = ai_verdicts.get(symbol)
        d_out, signal, ai_used, ai_reason = _apply_llm_verdict(d, verdict, symbol)

        if not ai_used:
            # Fallback: Python deterministic decision
            signal = _SIGNAL_MAP.get(d.get("decision"), "HOLD")
            d_out = d
            log.info("[FALLBACK] %s — AI unavailable, Python decision used: %s", symbol, signal)

        quality = float(d_out.get("setup_quality") or 0.0)
        if signal == "HOLD":
            summary["gate_rejected"] += 1
            gates = ", ".join(d_out.get("no_trade_reasons") or []) or "insufficient confluence"
            log.debug("GATE %s: %s blocked — %s (quality=%.0f)", symbol,
                      d_out.get("direction") or "n/a", gates, quality)

        sig = _build_sig(symbol, d_out, signal, snap5, rsi_bounce, fr, last_price)
        sig["ai_used"] = ai_used
        sig["ai_reason"] = ai_reason or ""
        base = symbol.split("/")[0].split(":")[0]
        sig["signal_id"] = f"{scan_id}-{base}-{signal}"
        decided.append((quality, signal, sig, symbol))
        ai_records.append({
            "symbol": symbol,
            "signal_id": sig["signal_id"],
            "deterministic_decision": d.get("decision") or "NO_TRADE",
        })
    # --- STEP 8-9: rank by setup-quality; PERSIST FIRST, then alert ---
    # Strongest setups first. Emission rule (owner's tier system): HOLD
    # log-only; BUY/SELL below ALERT_QUALITY_MIN (50) log-only; at/above it
    # alert + cooldown with the tier label (NORMAL 50-60 / HIGH 60-70 /
    # STRONG 70+). The CSV row is written BEFORE the Telegram send: delivery
    # failure can never cost the signal its persistence.
    decided.sort(key=lambda t: t[0], reverse=True)
    with _coordinator.stage("persist"):
        for quality, signal, sig, symbol in decided:
            kind = _emission_kind(signal, quality)
            if kind == "hold":
                # NO_TRADE: logged, never alerted, never cooled down. The same
                # blocked setup reappears on every scan, so an identical verdict
                # + reason is written once per HOLD_LOG_COOLDOWN_MIN; a change in
                # either is new information and always lands. The stamp goes on
                # only after a successful write, so a disk error retries next scan.
                hold_key = _hold_reason_key(sig)
                if guard.hold_logged_recently(symbol, now_ist, hold_key):
                    summary["hold_deduped"] += 1
                    log.debug("%s HOLD already logged (reason unchanged)", symbol)
                    continue
                if _persist(sig, summary):
                    guard.record_hold(symbol, now_ist, hold_key)
                    summary["holds"] += 1
            elif kind == "log_only":
                _persist(sig, summary)      # below the Telegram quality threshold
                summary["log_only"] += 1
                log.debug("%s %s log-only (quality %.0f < %d)", symbol, signal,
                          quality, config.ALERT_QUALITY_MIN)
            else:
                _persist(sig, summary)      # PERSISTED before delivery
                with _coordinator.stage("telegram"):
                    sent = alerts.send_alert(sig)   # bounded (20s), failure logged
                if not sent:
                    summary["telegram_failed"] += 1
                summary["signals"] += 1
                log.info("%s %s alert_sent=%s entry=%.6g sl=%.6g tp=%.6g rr=%s conf=%.0f "
                         "lev=%s pos=$%.2f", symbol, signal, sent, sig["entry"],
                         sig["SL"], sig["TP"], sig["RR"], sig["confidence"],
                         sig["leverage"], sig["position_size"])
                guard.record(symbol, now_ist)   # cooldown only for alerted BUY/SELL

      # --- STEP 7: AI already ran on critical path (see STEP 6) ---
    summary["ai_status"] = "PRIMARY" if config.LLM_DECISION_ENABLED else "OFF"
    _log_funnel_summary(scan_id, summary)
    _coordinator.end(summary)
    return summary
def _log_funnel_summary(scan_id: str, summary: dict) -> None:
    """One concise INFO block per scan (per-symbol detail stays at DEBUG)."""
    stages = _coordinator.stages
    critical_ms = sum(v for k, v in stages.items() if k != "telegram")
    log.info(
        "SCAN #%s COMPLETE | universe %d | 1H directional %d | decided %d | "
        "funding %d | duplicates %d | gate_rejected %d | "
        "alerts %d (log_only %d, holds %d, hold_deduped %d) | "
        "log_failed %d | telegram_failed %d | "
        "critical %.1fs | AI %s",
        scan_id, summary.get("scanned", 0), summary.get("pass_1h", 0),
        summary.get("pass_5m", 0), summary.get("funding_rejected", 0),
        summary.get("duplicates", 0), summary.get("gate_rejected", 0),
        summary.get("signals", 0), summary.get("log_only", 0),
        summary.get("holds", 0), summary.get("hold_deduped", 0),
        summary.get("log_failed", 0), summary.get("telegram_failed", 0),
        critical_ms / 1000.0, summary.get("ai_status", "OFF"))


def start_ondemand_scan() -> dict:
    """Telegram /scan_on: delegate to ondemand module."""
    return ondemand.start_ondemand_scan(
        run_scan_fn=run_scan,
        make_exchange_fn=scanner.make_exchange,
        fetch_funding_fn=scanner.fetch_funding_rates,
        guard_fn=get_coordinator().get_guard,   # SAME guard as scheduled scans
    )


def stop_ondemand_scan() -> dict:
    """Telegram /scan_off: delegate to ondemand module."""
    return ondemand.stop_ondemand_scan()


def _daily_summary() -> str:
    """Generate daily performance summary from signals_log.csv."""
    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    buys, sells, holds = 0, 0, 0
    try:
        if not config.SIGNALS_LOG_FILE.exists():
            return "No signals logged yet."
        with open(config.SIGNALS_LOG_FILE, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = row.get("timestamp", "")
                if not ts.startswith(today):
                    continue
                sig = row.get("signal", "")
                if sig == "BUY":
                    buys += 1
                elif sig == "SELL":
                    sells += 1
                elif sig == "HOLD":
                    holds += 1
    except Exception as exc:
        log.error("Daily summary read error: %s", exc)
        return f"Error reading signals: {exc}"

    total = buys + sells
    return (
        f"📊 <b>Daily Report ({today})</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟢 BUY signals: <b>{buys}</b>\n"
        f"🔴 SELL signals: <b>{sells}</b>\n"
        f"⏸ HOLD (silent): <b>{holds}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Total alerts sent: <b>{total}</b>\n"
        f"💰 Account: <b>${config.ACCOUNT_BALANCE:,.0f}</b> | "
        f"Risk: <b>{config.RISK_PER_TRADE_PCT}%</b>/trade"
    )


def build_scheduler() -> BlockingScheduler:
    """Production schedule: one scan every five minutes, 24/7.

    One job (not three overlapping ones) with an explicit overrun policy is
    what keeps alerts on time: `max_instances=1` + `coalesce=True` +
    `misfire_grace_time` mean a slow scan can never run concurrently with the
    next one nor stack up a backlog, and the `second` offset fires each scan a
    few seconds AFTER the candle close so the just-closed 5M candle exists.
    """
    exchange = scanner.make_exchange()
    # the ONE guard: shared with /scan_on sessions and --once via the
    # coordinator, so cooldowns are respected across every scan path
    guard = _coordinator.get_guard()
    scheduler = BlockingScheduler(timezone=config.SCHEDULER_TZ)
    # Per-session health tracking: consecutive-failure streak (health warning)
    # and scan/signal counts so an empty session reads as confirmed-healthy
    # silence, not a dead bot.
    session = {"streak": 0, "scans": 0, "signals": 0}

    def scan_job() -> None:
        t0 = time.monotonic()
        try:
            tickers = exchange.fetch_tickers()
            funding_rates = scanner.fetch_funding_rates(exchange)
            summary = run_scan(exchange, guard, tickers, funding_rates)
            session["scans"] += 1
            session["signals"] += summary.get("signals", 0)
            session["streak"] = 0
        except Exception as exc:  # scheduler jobs must never kill the loop
            session["streak"] += 1
            log.error("Scan job failed (%d in a row): %s", session["streak"], exc)
            if session["streak"] == 3:
                alerts.send_telegram_text(
                    f"⚠️ <b>Bot health warning</b>\n"
                    f"3 scans in a row failed (last: {type(exc).__name__}). "
                    f"Signals may be paused — check the server.")
        finally:
            elapsed = time.monotonic() - t0
            if elapsed > config.SCAN_INTERVAL_MIN * 60 * 0.8:  # >80% of 5min slot
                log.warning("⚠️ Scan took %.1fs (slot is %ds) — next scan may be skipped!",
                            elapsed, config.SCAN_INTERVAL_MIN * 60)

    # 18:00:15, 18:05:15 ... 22:55:15 -> 60 scans, no hour-boundary overlap
    scheduler.add_job(
        scan_job,
        CronTrigger(hour="18-22", minute="*/5", second=config.SCAN_SECOND_OFFSET,
                    timezone=config.SCHEDULER_TZ),
        id="scan", name="5-min scan 18:00-22:55 IST",
        max_instances=1, misfire_grace_time=config.SCAN_MISFIRE_GRACE_SEC, coalesce=True,
    )

    def session_start() -> None:
        session.update(streak=0, scans=0, signals=0)  # fresh counters for the night
        log.info("6:00 PM IST - Trading session started, beginning 5-minute scans")
        alerts.send_telegram_text(
            "🟢 <b>Trading Session Started (18:00 IST)</b>\n"
            "⚡ Scanning all USDT-M Futures pairs every 5 minutes..."
        )

    scheduler.add_job(session_start, CronTrigger(hour=18, minute=0, timezone=config.SCHEDULER_TZ),
                      id="session_start", name="session start marker")

    scheduler.add_job(guard.reset, CronTrigger(hour=23, minute=0, timezone=config.SCHEDULER_TZ),
                      id="guard_reset", name="reset duplicate tracker 23:00")

    def session_end() -> None:
        log.info("11:00 PM IST - session over, bot sleeps until 6:00 PM tomorrow")
        summary = _daily_summary()
        # Confirm the bot was alive even on a silent night, so "no alerts" is
        # never mistaken for a crashed bot (the owner's "koi issue nahi" ask).
        if session["scans"] == 0:
            health = ("⚠️ <b>No scans ran this session</b> — please check the server "
                      "(the schedule may not have fired).")
        elif session["signals"] == 0:
            health = (f"✅ <b>Bot healthy</b> — ran {session['scans']} scans; no setup "
                      f"met the confluence bar today (normal on quiet days).")
        else:
            health = f"✅ <b>Bot healthy</b> — ran {session['scans']} scans this session."
        alerts.send_telegram_text(
            "🌙 <b>Trading Session Ended (23:00 IST)</b>\n\n"
            f"{summary}\n\n"
            f"{health}\n\n"
            "<i>24/7 AI Chat Assistant remains active!</i>"
        )

    scheduler.add_job(session_end, CronTrigger(hour=23, minute=1, timezone=config.SCHEDULER_TZ),
                      id="session_end", name="session end marker")
    return scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Signal Bot (signals only, never trades)")
    parser.add_argument("--once", action="store_true",
                        help="DEMO/TEST: run one full scan cycle immediately")
    parser.add_argument("--force-llm", action="store_true",
                        help="TEST ONLY: bypass all gates; top setups go straight to the LLM")
    parser.add_argument("--force-llm-top", type=int, default=3, metavar="N",
                        help="TEST ONLY: how many top-quality coins --force-llm sends (default 3)")
    args = parser.parse_args()

    config.setup_logging()
    liquidation.start_listener()  # persistent; scans only read its current cache
    logger.migrate_csv_header()  # reconcile signals_log.csv to the current column set
    for warning in config.check_exposed_credentials():
        log.warning("SECURITY: %s", warning)
    # .env mistakes that would otherwise surface as "the AI went quiet"
    for warning in config.check_config_warnings():
        log.warning("CONFIG: %s", warning)
    log.info("=== Crypto Signal Bot starting (model=%s @ %s, telegram=%s, ai_key=%s) ===",
             config.AI_MODEL, config.AI_BASE_URL,
             "configured" if config.TELEGRAM_TOKEN else "NOT configured",
             "configured" if config.OPENROUTER_API_KEY else "NOT configured")

    # One line that states what the AI stage actually is, because "AI enabled" was
    # readable four different ways: which model, whether the provider is being
    # asked for JSON it must obey, how the token budget is sized, and — the one
    # people keep getting wrong — that the answer is AUDITED, never applied. When
    # `ai_used=False` shows up on every row, this line is the first thing to read:
    # it is the expected value for a scheduled scan, not a failure.
    log.info("AI CONTRACT: primary decision-maker (Python is fallback only) | "
             "queue=every decided setup incl. HOLDs | model=%s fallback=%s | "
             "json_mode=%s reasoning=%s | max_tokens=%d retry_cap=%d timeout=%.0fs | "
             "batch=%d retries=%d budget=%d/day | LLM_DECISION_ENABLED=%s",
             config.AI_MODEL, config.AI_MODEL_FALLBACK or "none",
             "on" if config.AI_JSON_MODE else "off",
             "on" if config.AI_REASONING_ENABLED else "off",
             config.AI_MAX_TOKENS, config.AI_MAX_TOKENS_RETRY_CAP,
             config.AI_TIMEOUT_SECONDS, config.AI_BATCH_MAX, config.AI_RETRY_MAX,
             config.AI_DAILY_BUDGET, config.LLM_DECISION_ENABLED)

    # News verification engine: discovery -> deterministic verification ->
    # (VERIFIED only) AI summary + impact analysis -> Telegram. Verification
    # and interpretation stay separate; news never feeds trading signals.
    if config.NEWS_ENABLED:
        import news
        news.start_engine()

    if args.once:
        log.info("DEMO/TEST mode: single scan cycle (outside scheduled session)")
        exchange = scanner.make_exchange()
        try:
            tickers = exchange.fetch_tickers()
            funding_rates = scanner.fetch_funding_rates(exchange)
        except Exception as exc:
            log.error("Ticker/funding fetch failed in demo mode: %s", exc)
            tickers = {}
            funding_rates = {}
        run_scan(exchange, duplicate_guard.DuplicateGuard(), tickers, funding_rates, force=True)
        return

    if args.force_llm:
        top_n = max(1, args.force_llm_top)
        log.info("FORCE-LLM TEST MODE: all gates bypassed — top %d setups go straight "
                 "to the LLM (logged only, no alerts/CSV/cooldown)", top_n)
        exchange = scanner.make_exchange()
        run_force_llm(exchange, top_n=top_n)
        return

    if config.TELEGRAM_TOKEN:
        started = telegram_bot.start_bot_listener()
        if started:
            log.info("Telegram Chat Assistant live: users can chat and query the bot on Telegram")

    # Send startup notification to Telegram
    alerts.send_telegram_text(
        f"🚀 <b>Crypto Signal Bot Started</b>\n\n"
         f"⏰ <b>Session:</b> {config.SESSION_START} to {config.SESSION_END} IST (every 5 min)\n"
        f"🤖 <b>AI Model:</b> <code>{config.AI_MODEL}</code>\n"
        f"⚡ <b>Market:</b> Futures (USDT-M Perpetual)\n"
        f"📊 <b>Volume Filter:</b> &gt;= ${config.VOLUME_MIN_USDT:,} USDT\n"
        f"💰 <b>Risk:</b> {config.RISK_PER_TRADE_PCT}% per trade on ${config.ACCOUNT_BALANCE:,.0f}\n"
        f"💬 <b>24/7 AI Chat:</b> Send /start or any question anytime!"
    )

    scheduler = build_scheduler()
    log.info("Scheduler live: scans every 5 min from %s to %s IST (60 scans), "
             "duplicate reset at %s. Ctrl+C to stop.",
             config.SESSION_START, config.SESSION_END, config.GUARD_RESET_TIME)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        alerts.send_telegram_text("🛑 <b>Crypto Signal Bot Stopped</b>")
        log.info("Bot stopped by user")


if __name__ == "__main__":
    main()
