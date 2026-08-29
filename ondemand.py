"""On-demand scan controller — Telegram /scan_on & /scan_off support.

Separated from main.py to avoid circular imports (main -> telegram_bot -> main).
main.py imports this module to wire up the scheduler.
telegram_bot.py imports this module for /scan_on and /scan_off commands.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config

log = logging.getLogger("ondemand")

# ------------------------------------------------------------------ state
_ondemand_scheduler: BackgroundScheduler | None = None
_ondemand_exchange = None
_ondemand_guard = None


def start_ondemand_scan(run_scan_fn, make_exchange_fn, fetch_funding_fn,
                        DuplicateGuardClass) -> dict:
    """Start on-demand 5-minute scans anytime outside 18:00-23:00 IST.

    Parameters are injected from main.py to avoid circular imports.
    Returns a status dict: {'status': 'started' | 'already_running'}.
    """
    global _ondemand_scheduler, _ondemand_exchange, _ondemand_guard

    if _ondemand_scheduler and _ondemand_scheduler.running:
        log.info("On-demand scan already running — ignoring start request")
        return {"status": "already_running"}

    _ondemand_exchange = make_exchange_fn()
    _ondemand_guard = DuplicateGuardClass()
    _ondemand_scheduler = BackgroundScheduler(timezone=config.SCHEDULER_TZ)

    def ondemand_job() -> None:
        try:
            tickers = _ondemand_exchange.fetch_tickers()
            funding_rates = fetch_funding_fn(_ondemand_exchange)
            # force=True — bypasses 18:00-23:00 session window check
            run_scan_fn(_ondemand_exchange, _ondemand_guard, tickers, funding_rates, force=True)
        except Exception as exc:
            log.error("On-demand scan job failed: %s", exc)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    _ondemand_scheduler.add_job(
        ondemand_job,
        IntervalTrigger(minutes=5, timezone=config.SCHEDULER_TZ),
        id="ondemand_scan",
        name="On-demand Telegram scan",
        next_run_time=datetime.now(ZoneInfo(config.SCHEDULER_TZ)),  # run immediately
    )
    _ondemand_scheduler.start()
    log.info("On-demand scan STARTED via Telegram (every 5 min, force mode)")
    return {"status": "started"}


def stop_ondemand_scan() -> dict:
    """Stop the on-demand scan session.

    Returns a status dict: {'status': 'stopped' | 'not_running'}.
    """
    global _ondemand_scheduler

    if not _ondemand_scheduler or not _ondemand_scheduler.running:
        log.info("On-demand scan stop requested but nothing is running")
        return {"status": "not_running"}

    _ondemand_scheduler.shutdown(wait=False)
    _ondemand_scheduler = None
    log.info("On-demand scan STOPPED via Telegram")
    return {"status": "stopped"}
