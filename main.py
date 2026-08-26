"""Crypto Signal Bot — entry point + APScheduler timer.

Every 5 minutes between 18:00 and 23:00 IST (60 scans/session):

    STEP 1 full exchange scan (all USDT-M futures perps, $50M volume filter)
    STEP 2 1H context (graded confluence + liquidation sweep) -> fail: skip
    STEP 3 funding rate check (reject overleveraged market)
    STEP 4 duplicate guard (15 min per coin) — checked BEFORE 15M/5M fetches
    STEP 5 15M confirmation + 5M entry (graded, gated on confluence)
    STEP 6 AI decision — ONE batched request for the whole scan (BUY/SELL/HOLD
           + SL/TP/RR + confidence), Python fallback per undecided coin
    STEP 7 leverage suggestion + position size
    STEP 8 Telegram alert (BUY/SELL only, HOLD silent) with the full setup
    STEP 9 append every signal to signals_log.csv
    STEP 10 daily performance summary at session end

Fetching is concurrent and batched per timeframe; a hard per-scan deadline
(config.SCAN_DEADLINE_SECONDS) guarantees a scan can never bleed into the next
5-minute slot: past it the AI stage is skipped and the Python fallback is used.

Outside 18:00-23:00 IST the scheduler runs nothing: zero activity,
zero market API calls, zero AI calls.

Usage:
    python main.py            # production schedule (APScheduler, IST)
    python main.py --once     # DEMO/TEST: run one full scan cycle now

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
import scanner
import scoring
import telegram_bot
import ondemand
from ai_decision import (AIDecisionError, budget_exhausted_notice,
                         nemotron_decision, nemotron_decisions)
from fallback import explanation_fallback, fallback_decision

log = logging.getLogger("main")


def _in_session(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return config.SESSION_START <= hhmm < config.SESSION_END


# decision.decide() speaks LONG/SHORT/NO_TRADE; the alert/log/guard layer speaks
# BUY/SELL/HOLD. Map at this single boundary so everything downstream is unchanged.
_SIGNAL_MAP = {"LONG": "BUY", "SHORT": "SELL", "NO_TRADE": "HOLD"}


def _decide(bundle: dict) -> dict:
    """OpenRouter/Nemotron decision with Python fallback (single-candidate).

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
               rsi_bounce: bool, funding_rate, last_price: float) -> dict:
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
        "reason": explanation_fallback(d),
        "ai_used": False,
        # --- structure-first decision evidence (new CSV columns) ---
        "decision": d.get("decision"),
        "setup_quality": round(quality, 1),
        "htf_bias": d.get("htf_bias"),
        "structure": _summarize_structure(struct),
        "sr_zone": _fmt_zone(opp_zone),
        "liquidity": _summarize_liquidity(liq, signal),
        "no_trade_reason": ", ".join(d.get("no_trade_reasons") or []),
        "data_warnings": ", ".join(d.get("data_warnings") or []),
    }


def run_scan(exchange, guard: duplicate_guard.DuplicateGuard,
             tickers: dict | None = None,
             funding_rates: dict | None = None,
             force: bool = False) -> dict:
    """One full scan cycle. `force=True` runs it regardless of the session
    window (DEMO/TEST mode) — production scans are only ever scheduled
    inside 18:00-23:00 IST.

    *tickers* is the raw dict from exchange.fetch_tickers(); when provided it
    is forwarded to the scanner so fetch_tickers() is called once per cycle.
    *funding_rates* is the {symbol: rate} dict for funding-rate filtering.

    Fetching is batched per timeframe and bounded by a hard deadline so the
    scan can never overrun its 5-minute slot.
    """
    now_ist = datetime.now(config.TZ)
    if not force and not _in_session(now_ist):
        log.info("Outside active session (now %s IST) - zero activity", now_ist.strftime("%H:%M"))
        return {"scanned": 0}

    deadline = time.monotonic() + config.SCAN_DEADLINE_SECONDS

    symbols = scanner.get_active_usdt_symbols(exchange, tickers)
    tickers_last = {}
    if tickers:
        tickers_last = {s: t.get("last") for s, t in tickers.items()
                        if isinstance(t, dict) and t.get("last")}
    if funding_rates is None:
        funding_rates = {}
    summary = {"scanned": len(symbols), "pass_1h": 0, "pass_15m": 0, "pass_5m": 0,
               "funding_rejected": 0, "duplicates": 0, "gate_rejected": 0,
               "ai_used": 0, "signals": 0, "holds": 0}

    # --- STEP 1+2: batch-fetch 1H for every symbol; structure funnel ---
    frames_1h = scanner.fetch_timeframe_batch(symbols, "1h", deadline=deadline)
    candidates = []  # survivors of the 1H structure funnel + funding + duplicate guard
    for symbol, df_1h in frames_1h.items():
        feat = filter_1h.analyze_1h(df_1h)
        if feat is None or feat["direction"] is None:
            continue                       # ranging / undecided HTF -> not worth LTF fetch
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

        candidates.append({"symbol": symbol, "direction": direction,
                           "df_1h": df_1h, "fr": fr})

    # --- STEP 5a: batch-fetch 15M (setup TF) for the survivors ---
    frames_15m = scanner.fetch_timeframe_batch([c["symbol"] for c in candidates], "15m",
                                               deadline=deadline)
    with_15m = []
    for c in candidates:
        df_15m = frames_15m.get(c["symbol"])
        if df_15m is None:
            continue
        summary["pass_15m"] += 1
        c["df_15m"] = df_15m
        with_15m.append(c)

    # --- STEP 5b: batch-fetch 5M (entry TF); run the decision core over all 3 TFs ---
    frames_5m = scanner.fetch_timeframe_batch([c["symbol"] for c in with_15m], "5m",
                                              deadline=deadline)
    decided = []   # (setup_quality, signal, sig, symbol) for ranked emission
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

        # crypto-futures context: OI history (safe-degrade to None past the deadline)
        oi_df = None
        if config.OI_FETCH_ENABLED and time.monotonic() < deadline:
            oi_df = scanner.fetch_open_interest_history(
                exchange, symbol, config.OI_HISTORY_TIMEFRAME, config.OI_HISTORY_LIMIT)

        # THE decision: deterministic core over HTF/setup/entry frames.
        frames = {config.TF_HTF: c["df_1h"], config.TF_SETUP: c["df_15m"],
                  config.TF_ENTRY: df_5m}
        d = decision_core.decide(frames, funding_rate=fr, oi_df=oi_df)

        signal = _SIGNAL_MAP.get(d["decision"], "HOLD")
        last_price = ((tickers_last or {}).get(symbol) or d.get("entry")
                      or (snap5 or {}).get("close"))
        sig = _build_sig(symbol, d, signal, snap5, rsi_bounce, fr, last_price)

        if signal == "HOLD":
            summary["gate_rejected"] += 1
            log.info("%s: NO_TRADE — %s (quality=%.0f)", symbol,
                     ", ".join(d["no_trade_reasons"]) or "insufficient confluence",
                     d.get("setup_quality") or 0.0)
        decided.append((float(d.get("setup_quality") or 0.0), signal, sig, symbol))

    if not decided:
        log.info("Scan complete: %s", summary)
        return summary

    # --- STEP 6-9: rank by setup-quality; alert (BUY/SELL) + log every signal ---
    # Strongest setups first so the best alerts lead the session; NO_TRADE (HOLD)
    # rows are logged silently. The LLM explanation layer (optional, cosmetic) is
    # not called here — the reason text is the local structured explanation.
    decided.sort(key=lambda t: t[0], reverse=True)
    for _quality, signal, sig, symbol in decided:
        if signal == "HOLD":
            logger.log_signal(sig)          # NO_TRADE: logged, no alert, no cooldown
            summary["holds"] += 1
        else:
            sent = alerts.send_alert(sig)   # failure logged, bot continues
            logger.log_signal(sig)
            summary["signals"] += 1
            log.info("%s %s alert_sent=%s entry=%.6g sl=%.6g tp=%.6g rr=%s conf=%.0f "
                     "lev=%s pos=$%.2f", symbol, signal, sent, sig["entry"],
                     sig["SL"], sig["TP"], sig["RR"], sig["confidence"], sig["leverage"],
                     sig["position_size"])
            guard.record(symbol, now_ist)   # cooldown only for BUY/SELL, not HOLD

    log.info("Scan complete: %s", summary)
    return summary


def start_ondemand_scan() -> dict:
    """Telegram /scan_on: delegate to ondemand module."""
    return ondemand.start_ondemand_scan(
        run_scan_fn=run_scan,
        make_exchange_fn=scanner.make_exchange,
        fetch_funding_fn=scanner.fetch_funding_rates,
        DuplicateGuardClass=duplicate_guard.DuplicateGuard,
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
    """Production schedule: 60 scans between 18:00 and 22:55 IST from a SINGLE
    job, duplicate tracker reset at 23:00, session markers. Silence outside.

    One job (not three overlapping ones) with an explicit overrun policy is
    what keeps alerts on time: `max_instances=1` + `coalesce=True` +
    `misfire_grace_time` mean a slow scan can never run concurrently with the
    next one nor stack up a backlog, and the `second` offset fires each scan a
    few seconds AFTER the candle close so the just-closed 5M candle exists.
    """
    exchange = scanner.make_exchange()
    guard = duplicate_guard.DuplicateGuard()
    scheduler = BlockingScheduler(timezone=config.SCHEDULER_TZ)
    # Per-session health tracking: consecutive-failure streak (health warning)
    # and scan/signal counts so an empty session reads as confirmed-healthy
    # silence, not a dead bot.
    session = {"streak": 0, "scans": 0, "signals": 0}

    def scan_job() -> None:
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
    args = parser.parse_args()

    config.setup_logging()
    logger.migrate_csv_header()  # reconcile signals_log.csv to the current column set
    log.info("=== Crypto Signal Bot starting (model=%s, telegram=%s, openrouter=%s) ===",
             config.AI_MODEL,
             "configured" if config.TELEGRAM_TOKEN else "NOT configured",
             "configured" if config.OPENROUTER_API_KEY else "NOT configured")

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
