"""Phase 11 — Signal logger.

Appends every BUY / SELL / HOLD signal to signals_log.csv:
    timestamp, coin, signal, entry, SL, TP, RR, reason, ai_used
Append-only: the file is never truncated or deleted.
"""
import csv
import logging
from datetime import datetime

import config

log = logging.getLogger("logger")


def log_signal(signal: dict) -> None:
    """Append one signal row. `signal` keys mirror config.CSV_COLUMNS
    (timestamp/SL/TP/ai_used are filled here when missing)."""
    row = {
        "timestamp": signal.get("timestamp") or datetime.now(config.TZ).isoformat(timespec="seconds"),
        "coin": signal["coin"],
        "signal": signal["signal"],
        "entry": signal.get("entry", ""),
        "SL": signal.get("SL") if signal.get("SL") is not None else "",
        "TP": signal.get("TP") if signal.get("TP") is not None else "",
        "RR": signal.get("RR") if signal.get("RR") is not None else "",
        "reason": signal.get("reason", ""),
        "ai_used": signal.get("ai_used", ""),
    }
    new_file = not config.SIGNALS_LOG_FILE.exists()
    try:
        with open(config.SIGNALS_LOG_FILE, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=config.CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        log.error("failed writing signal row for %s: %s", row["coin"], exc)
        raise
    log.info("Signal logged: %s %s %s (ai_used=%s)",
             row["coin"], row["signal"], row["entry"], row["ai_used"])
