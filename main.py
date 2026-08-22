"""Crypto Signal Bot — entry point + APScheduler timer (Phase 12).

Every 5 minutes between 18:00 and 23:00 IST (60 scans/session):

    STEP 1 full exchange scan (all USDT-M futures perps, $50M volume filter)
    STEP 2 1H context + liquidation sweep + sideways/overbought guard -> fail: skip
    STEP 3 funding rate check (reject overleveraged market)
    STEP 4 15M confirmation (core mandatory)        -> fail: reject
    STEP 5 5M entry + RSI trend (core mandatory)    -> fail: reject
    STEP 6 AI decision (BUY/SELL/HOLD + SL/TP/RR) or Python fallback
    STEP 7 duplicate guard (15 min per coin)
    STEP 8 leverage suggestion + position size calc
    STEP 9 Telegram alert (BUY/SELL only, HOLD silent)
    STEP 10 append every signal to signals_log.csv
    STEP 11 daily performance summary at session end

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
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import alerts
import config
import duplicate_guard
import filter_15m
import filter_1h
import filter_5m
import logger
import scanner
import telegram_bot
import ondemand
from ai_decision import AIDecisionError, nemotron_decision
from fallback import fallback_decision

log = logging.getLogger("main")


def _in_session(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return config.SESSION_START <= hhmm < config.SESSION_END


def _decide(bundle: dict) -> dict:
    """OpenRouter/Nemotron decision with Python fallback (STEP 6 of the flow)."""
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


def run_scan(exchange, guard: duplicate_guard.DuplicateGuard,
             tickers: dict | None = None,
             funding_rates: dict | None = None,
             force: bool = False) -> dict:
    """One full scan cycle. `force=True` runs it regardless of the session
    window (DEMO/TEST mode) — production scans are only ever scheduled
    inside 18:00-23:00 IST.

    *tickers* is the raw dict from exchange.fetch_tickers(); when provided
    it is forwarded to the scanner so fetch_tickers() is called only once
    per scan cycle.

    *funding_rates* is the {symbol: rate} dict for funding rate filtering."""
    now_ist = datetime.now(config.TZ)
    if not force and not _in_session(now_ist):
        log.info("Outside active session (now %s IST) - zero activity", now_ist.strftime("%H:%M"))
        return {"scanned": 0}

    symbols = scanner.get_active_usdt_symbols(exchange, tickers)
    # Derive {symbol: last_price} for current_price fallback
    tickers_last = {}
    if tickers:
        tickers_last = {s: t.get("last") for s, t in tickers.items() if isinstance(t, dict) and t.get("last")}
    if funding_rates is None:
        funding_rates = {}
    summary = {"scanned": len(symbols), "pass_1h": 0, "pass_15m": 0, "pass_5m": 0,
               "funding_rejected": 0, "duplicates": 0, "signals": 0, "holds": 0}

    for symbol in symbols:
        df_1h = scanner.fetch_ohlcv(exchange, symbol, "1h")
        if df_1h is None:
            continue
        ctx = filter_1h.analyze_1h(df_1h)
        if ctx is None:
            continue
        summary["pass_1h"] += 1
        direction = ctx["direction"]

        # STEP 3: Funding rate check — reject when market is overleveraged
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

        df_15m = scanner.fetch_ohlcv(exchange, symbol, "15m")
        confirm = None if df_15m is None else filter_15m.confirm_15m(df_15m, direction)
        if confirm is None:
            continue
        summary["pass_15m"] += 1

        df_5m = scanner.fetch_ohlcv(exchange, symbol, "5m")
        entry = None if df_5m is None else filter_5m.entry_5m(df_5m, direction)
        if entry is None:
            continue
        summary["pass_5m"] += 1

        last_price = (tickers_last or {}).get(symbol) or entry["indicators"]["close"]
        bundle = {
            "symbol": symbol,
            "direction": direction,
            "current_price": float(last_price),
            "entry_price": entry["indicators"]["close"],
            "ind_1h": ctx["indicators"],
            "sweep": ctx["sweep"],
            "ind_15m": confirm["indicators"],
            "confirm_score": confirm["score"],
            "ind_5m": entry["indicators"],
        }

        # STEP 7: duplicate guard — same coin signaled within cooldown: skip silently
        if guard.is_duplicate(symbol, now_ist):
            summary["duplicates"] += 1
            log.debug("%s: duplicate within cooldown - skipped silently", symbol)
            continue

        # STEP 6: OpenRouter/Nemotron (or fallback) -> BUY / SELL / HOLD
        decision = _decide(bundle)

        # STEP 8: Leverage suggestion + position size
        leverage = _calc_leverage(ctx["indicators"]["atr"], float(last_price))
        pos = _calc_position_size(decision["entry"], decision.get("sl"))

        sig = {
            "coin": symbol,
            "signal": decision["signal"],
            "entry": decision["entry"],
            "SL": decision["sl"],
            "TP": decision["tp"],
            "RR": decision["rr"],
            "leverage": leverage,
            "position_size": pos["value"],
            "funding_rate": fr,
            "reason": decision["reason"],
            "ai_used": decision["ai_used"],
        }

        if decision["signal"] == "HOLD":
            # HOLD: no Telegram alert, still logged, NO cooldown recorded
            logger.log_signal(sig)
            summary["holds"] += 1
        else:
            sent = alerts.send_alert(sig)  # STEP 9 (failure logged, bot continues)
            logger.log_signal(sig)          # STEP 10
            summary["signals"] += 1
            log.info("%s %s alert_sent=%s entry=%.6g sl=%.6g tp=%.6g rr=%.2f lev=%s pos=$%.2f",
                     symbol, sig["signal"], sent, sig["entry"], sig["SL"], sig["TP"],
                     sig["RR"], leverage, pos["value"])
            guard.record(symbol, now_ist)  # cooldown only for BUY/SELL, not HOLD

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
    """Production schedule: 60 scans between 18:00 and 22:55 IST, duplicate
    tracker reset at 23:00, session markers. Complete silence outside."""
    exchange = scanner.make_exchange()
    guard = duplicate_guard.DuplicateGuard()
    scheduler = BlockingScheduler(timezone=config.SCHEDULER_TZ)

    def scan_job() -> None:
        try:
            tickers = exchange.fetch_tickers()
            funding_rates = scanner.fetch_funding_rates(exchange)
            run_scan(exchange, guard, tickers, funding_rates)
        except Exception as exc:  # scheduler jobs must never kill the loop
            log.error("Scan job failed: %s", exc)

    # 18:00, 18:05 ... 18:55  (12 scans)
    scheduler.add_job(scan_job, CronTrigger(hour=18, minute="*/5", timezone=config.SCHEDULER_TZ),
                      id="scan_18", name="scan 18:00-18:55")
    # 19:00 ... 21:55         (36 scans)
    scheduler.add_job(scan_job, CronTrigger(hour="19-21", minute="*/5", timezone=config.SCHEDULER_TZ),
                      id="scan_19_21", name="scan 19:00-21:55")
    # 22:00 ... 22:55         (12 scans) -> 60 scans total, last one 5 min before close
    scheduler.add_job(scan_job, CronTrigger(hour=22, minute="*/5", timezone=config.SCHEDULER_TZ),
                      id="scan_22", name="scan 22:00-22:55")

    def session_start() -> None:
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
        alerts.send_telegram_text(
            "🌙 <b>Trading Session Ended (23:00 IST)</b>\n\n"
            f"{summary}\n\n"
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
