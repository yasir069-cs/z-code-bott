"""Signal logger.

Appends every BUY / SELL / HOLD signal to signals_log.csv. The row is built
generically from config.CSV_COLUMNS, so adding a column to that list is enough
to start recording it — a field can never again be silently dropped because
this module hand-listed a shorter set of keys than the header declares.

Append-only: the file is never truncated or deleted. If the on-disk header no
longer matches CSV_COLUMNS (an older run wrote fewer columns), the old file is
archived to signals_log.csv.v1.bak and a fresh file with the correct header is
started — the history is preserved in the archive, never overwritten in place.
"""
import csv
import json
import logging
from datetime import datetime

import config

log = logging.getLogger("logger")


def migrate_csv_header() -> None:
    """Reconcile the on-disk header with config.CSV_COLUMNS.

    Called once at startup. If the file exists with a different header (e.g. a
    9-column file written before extra columns were added), it is renamed to a
    `.vN.bak` sibling so a DictWriter never writes 20 fields under a 9-field
    header again (which shifted every value into the wrong column). No data is
    lost — the old rows live on in the archive.
    """
    path = config.SIGNALS_LOG_FILE
    if not path.exists():
        return
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), None)
    except OSError as exc:
        log.error("could not read %s to check its header: %s", path.name, exc)
        return

    if header == config.CSV_COLUMNS:
        return

    bak = path.parent / (path.name + ".v1.bak")
    n = 2
    while bak.exists():
        bak = path.parent / (path.name + f".v{n}.bak")
        n += 1
    try:
        path.rename(bak)
    except OSError as exc:
        log.error("could not archive stale %s: %s", path.name, exc)
        return
    log.warning("signals_log header changed (%d -> %d columns); archived old file to %s",
                len(header or []), len(config.CSV_COLUMNS), bak.name)


def _cell(value):
    """Coerce a sig value into a single CSV cell.

    Structured values (the sweep dict the Telegram alert consumes) are reduced
    to a scalar label; None becomes an empty cell.
    """
    if isinstance(value, dict):
        return value.get("type") or ("detected" if value.get("detected") else "none")
    if value is None:
        return ""
    return value


def log_signal(signal: dict) -> None:
    """Append one signal row, keyed exactly by config.CSV_COLUMNS."""
    row = {}
    for col in config.CSV_COLUMNS:
        if col == "timestamp":
            row[col] = (signal.get("timestamp")
                        or datetime.now(config.TZ).isoformat(timespec="seconds"))
        else:
            value = signal.get(col)
            # Keep the structured websocket summary inspectable in the CSV;
            # legacy fields such as sweep retain their compact scalar format.
            row[col] = json.dumps(value, sort_keys=True, separators=(",", ":")) \
                if col == "liquidation" and isinstance(value, dict) else _cell(value)

    new_file = not config.SIGNALS_LOG_FILE.exists()
    try:
        with open(config.SIGNALS_LOG_FILE, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=config.CSV_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except OSError as exc:
        log.error("failed writing signal row for %s: %s", row.get("coin"), exc)
        raise
    log.info("Signal logged: %s %s %s (ai_used=%s)",
             row.get("coin"), row.get("signal"), row.get("entry"), row.get("ai_used"))
