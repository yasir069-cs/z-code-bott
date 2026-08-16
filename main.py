"""Crypto Signal Bot — entry point + APScheduler timer (Phase 12).

Every 5 minutes between 6:30 PM and 9:30 PM IST (36 scans/session):

    STEP 1 full exchange scan (all USDT pairs, $5M volume filter)
    STEP 2 1H context + liquidation sweep        -> fail: skip coin
    STEP 3 15M confirmation (need 4/5)           -> fail: reject
    STEP 4 5M entry + RSI trend                  -> fail: reject
    STEP 5 Claude AI decision (BUY/SELL/HOLD + SL/TP/RR) or Python fallback
    STEP 6 duplicate guard (20 min per coin)
    STEP 7 Telegram alert (BUY/SELL only, HOLD silent)
    STEP 8 append every signal to signals_log.csv

Outside 18:30-21:30 IST the scheduler runs nothing: zero activity,
zero market API calls, zero Claude calls.

Usage:
    python main.py            # production schedule (APScheduler, IST)
    python main.py --once     # DEMO/TEST: run one full scan cycle now

Signals only — this bot NEVER places trades (no trading endpoints, no keys).
"""
import argparse
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
from ai_decision import AIDecisionError, nemotron_decision
from fallback import fallback_decision

log = logging.getLogger("main")


def _in_session(now_ist: datetime) -> bool:
    hhmm = now_ist.strftime("%H:%M")
    return config.SESSION_START <= hhmm < config.SESSION_END


def _decide(bundle: dict) -> dict:
    """OpenRouter/Nemotron decision with Python fallback (STEP 5 of the flow)."""
    try:
        decision = nemotron_decision(bundle)
        log.info("%s: AI -> %s (confidence=%s)", bundle["symbol"], decision["signal"],
                 decision.get("confidence"))
        return decision
    except AIDecisionError as exc:
        log.warning("%s: AI unavailable (%s) -> Python fallback", bundle["symbol"], exc)
        return fallback_decision(bundle)


def run_scan(exchange, guard: duplicate_guard.DuplicateGuard,
             tickers_last: dict[str, float] | None = None,
             force: bool = False) -> dict:
    """One full scan cycle. `force=True` runs it regardless of the session
    window (DEMO/TEST mode) — production scans are only ever scheduled
    inside 18:30-21:30 IST."""
    now_ist = datetime.now(config.TZ)
    if not force and not _in_session(now_ist):
        log.info("Outside active session (now %s IST) - zero activity", now_ist.strftime("%H:%M"))
        return {"scanned": 0}

    symbols = scanner.get_active_usdt_symbols(exchange)
    summary = {"scanned": len(symbols), "pass_1h": 0, "pass_15m": 0, "pass_5m": 0,
               "duplicates": 0, "signals": 0, "holds": 0}

    for symbol in symbols:
        df_1h = scanner.fetch_ohlcv(exchange, symbol, "1h")
        if df_1h is None:
            continue
        ctx = filter_1h.analyze_1h(df_1h)
        if ctx is None:
            continue
        summary["pass_1h"] += 1
        direction = ctx["direction"]

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

        # STEP 6: duplicate guard — same coin signaled within 20 min: skip silently
        if guard.is_duplicate(symbol, now_ist):
            summary["duplicates"] += 1
            log.debug("%s: duplicate within cooldown - skipped silently", symbol)
            continue

        # STEP 5: OpenRouter/Nemotron (or fallback) -> BUY / SELL / HOLD
        decision = _decide(bundle)
        sig = {
            "coin": symbol,
            "signal": decision["signal"],
            "entry": decision["entry"],
            "SL": decision["sl"],
            "TP": decision["tp"],
            "RR": decision["rr"],
            "reason": decision["reason"],
            "ai_used": decision["ai_used"],
        }

        if decision["signal"] == "HOLD":
            # HOLD: no Telegram alert, still logged
            logger.log_signal(sig)
            summary["holds"] += 1
        else:
            sent = alerts.send_alert(sig)  # STEP 7 (failure logged, bot continues)
            logger.log_signal(sig)          # STEP 8
            summary["signals"] += 1
            log.info("%s %s alert_sent=%s entry=%.6g sl=%.6g tp=%.6g rr=%.2f",
                     symbol, sig["signal"], sent, sig["entry"], sig["SL"], sig["TP"], sig["RR"])
        guard.record(symbol, now_ist)

    log.info("Scan complete: %s", summary)
    return summary


def build_scheduler() -> BlockingScheduler:
    """Production schedule: 36 scans between 18:30 and 21:25 IST, duplicate
    tracker reset at 21:30, session markers. Complete silence outside."""
    exchange = scanner.make_exchange()
    guard = duplicate_guard.DuplicateGuard()
    scheduler = BlockingScheduler(timezone=config.SCHEDULER_TZ)

    def scan_job() -> None:
        try:
            tickers = exchange.fetch_tickers()
            tickers_last = {s: t.get("last") for s, t in tickers.items() if t.get("last")}
            run_scan(exchange, guard, tickers_last)
        except Exception as exc:  # scheduler jobs must never kill the loop
            log.error("Scan job failed: %s", exc)

    # 18:30, 18:35 ... 18:55  (6)
    scheduler.add_job(scan_job, CronTrigger(hour=18, minute="30-59/5", timezone=config.SCHEDULER_TZ),
                      id="scan_1830", name="scan 18:30-18:55")
    # 19:00 ... 20:55        (24)
    scheduler.add_job(scan_job, CronTrigger(hour="19-20", minute="*/5", timezone=config.SCHEDULER_TZ),
                      id="scan_19_20", name="scan 19:00-20:55")
    # 21:00 ... 21:25        (6)  -> 36 scans total, last one 5 min before close
    scheduler.add_job(scan_job, CronTrigger(hour=21, minute="0-25/5", timezone=config.SCHEDULER_TZ),
                      id="scan_21", name="scan 21:00-21:25")

    scheduler.add_job(guard.reset, CronTrigger(hour=21, minute=30, timezone=config.SCHEDULER_TZ),
                      id="guard_reset", name="reset duplicate tracker 21:30")

    def session_end() -> None:
        log.info("9:30 PM IST - session over, bot sleeps until 6:30 PM tomorrow")

    scheduler.add_job(session_end, CronTrigger(hour=21, minute=31, timezone=config.SCHEDULER_TZ),
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
            tickers_last = {s: t.get("last") for s, t in tickers.items() if t.get("last")}
        except Exception as exc:
            log.error("Ticker fetch failed in demo mode: %s", exc)
            tickers_last = {}
        run_scan(exchange, duplicate_guard.DuplicateGuard(), tickers_last, force=True)
        return

    scheduler = build_scheduler()
    log.info("Scheduler live: scans every 5 min from %s to %s IST (36 scans), "
             "duplicate reset at %s. Ctrl+C to stop.",
             config.SESSION_START, config.SESSION_END, config.GUARD_RESET_TIME)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by user")


if __name__ == "__main__":
    main()
