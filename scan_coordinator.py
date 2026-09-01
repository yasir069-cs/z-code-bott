"""ScanCoordinator — one scan at a time, bounded services, observable state.

Every scan entry point (scheduled APScheduler job, Telegram /scan_on
on-demand session, --once demo) passes through the same coordinator:

    SCAN A running -> SCAN B requested -> B does not start ("scan already
    active"). A single non-blocking lock enforces this at process level —
    the bot is one systemd service, so threading is the right scope (no
    Redis/Kafka/distributed locking).

The coordinator also owns:
  * ONE DuplicateGuard for all paths (scheduled and on-demand scans used to
    carry separate guards, so a coin alerted by one path was immediately
    re-alerted by the other).
  * stage timings for the running/last scan (universe, funding, OHLCV...).
  * the last-scan health snapshot /status reads.
  * the background AI opinion worker: the deterministic pipeline persists and
    emits its signals during the scan; AI verdicts are computed afterwards in
    a single-worker executor and recorded to ai_opinions.csv. A slow, failing
    or malformed AI response can therefore never delay or break a scan, and
    the next scheduled scan always starts on time.

AI states: PENDING -> SUCCESS | FAILED | TIMEOUT | UNAVAILABLE.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional

import config

log = logging.getLogger("coordinator")


class _StageTimer:
    """Context manager recording per-stage wall time into a dict."""

    def __init__(self, stages: dict, name: str):
        self._stages = stages
        self._name = name
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stages[self._name] = round((time.monotonic() - self._t0) * 1000.0, 1)
        return False


class AIOpinionWorker:
    """Single-worker background executor for AI opinions.

    The scan hands over the (already persisted) deterministic decisions plus
    the LLM bundles; the worker asks the model for verdicts whenever it gets
    around to it and writes an audit row per candidate. Nothing about this is
    allowed to touch the scan: exceptions are contained, the queue depth is
    bounded, and the worker never emits alerts.
    """

    def __init__(self, verdicts_fn: Callable):
        self._verdicts_fn = verdicts_fn
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-worker")
        self._pending = 0
        self._lock = threading.Lock()
        self.last_status: str = "IDLE"          # IDLE/PENDING/SUCCESS/FAILED/TIMEOUT/UNAVAILABLE
        self.last_finished_at: Optional[datetime] = None
        self.last_duration_s: float = 0.0
        self.last_queue_delay_s: float = 0.0

    def submit(self, scan_id: str, records: list, bundles: list) -> bool:
        """Queue one AI opinion batch. Returns False when the queue is full
        (the batch is dropped with UNAVAILABLE — never blocks the scan)."""
        if not records or not bundles:
            return False
        with self._lock:
            if self._pending >= config.AI_WORKER_MAX_PENDING:
                log.warning("AI worker queue full (%d pending) — opinion batch for "
                            "scan %s dropped (UNAVAILABLE)", self._pending, scan_id)
                self.last_status = "UNAVAILABLE"
                return False
            self._pending += 1
            self.last_status = "PENDING"
            queued_at = time.monotonic()

        def _task():
            with self._lock:
                self._pending -= 1
            self.last_queue_delay_s = round(time.monotonic() - queued_at, 2)
            started = time.monotonic()
            try:
                self._run(scan_id, records, bundles)
                self.last_status = "SUCCESS"
            except Exception as exc:
                name = type(exc).__name__
                self.last_status = "TIMEOUT" if "timeout" in str(exc).lower() else "FAILED"
                log.warning("AI opinion batch for scan %s failed (%s: %s) — scan "
                            "unaffected, deterministic signals stand", scan_id, name, exc)
            finally:
                self.last_duration_s = round(time.monotonic() - started, 2)
                self.last_finished_at = datetime.now(config.TZ)

        self._pool.submit(_task)
        return True

    def _run(self, scan_id: str, records: list, bundles: list) -> None:
        """Fetch verdicts and write the audit rows. AI failure states map to
        distinct statuses; malformed responses surface as FAILED, not crash."""
        by_symbol = {b["symbol"]: b for b in bundles}
        verdicts: dict = {}
        try:
            verdicts = self._verdicts_fn(bundles)
        except Exception as exc:
            text = str(exc).lower()
            status = "TIMEOUT" if "timeout" in text else "FAILED"
            self._write_rows(scan_id, records, {}, status, str(exc)[:300])
            raise

        rows = []
        for rec in records:
            verdict = verdicts.get(rec["symbol"])
            if verdict is None:
                rows.append((rec, None, "UNAVAILABLE", None, ""))
            else:
                rows.append((rec, verdict, "SUCCESS", verdict.get("confidence"),
                             verdict.get("reason") or ""))
        self._write_rows(scan_id, records, verdicts, "SUCCESS", "")
        for rec, verdict, status, conf, reason in rows:
            log.info("AI opinion %s: deterministic=%s ai=%s status=%s",
                     rec["symbol"], rec["deterministic_decision"],
                     (verdict or {}).get("signal"), status)

    def _write_rows(self, scan_id: str, records: list, verdicts: dict,
                    status: str, error: str) -> None:
        try:
            new_file = not config.AI_OPINIONS_LOG_FILE.exists()
            with open(config.AI_OPINIONS_LOG_FILE, "a", newline="",
                      encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=config.AI_OPINION_COLUMNS)
                if new_file:
                    writer.writeheader()
                for rec in records:
                    verdict = verdicts.get(rec["symbol"]) or {}
                    writer.writerow({
                        "timestamp": datetime.now(config.TZ).isoformat(timespec="seconds"),
                        "scan_id": scan_id,
                        "signal_id": rec.get("signal_id", ""),
                        "symbol": rec["symbol"],
                        "deterministic_decision": rec["deterministic_decision"],
                        "ai_opinion": verdict.get("signal", ""),
                        "ai_status": status if verdict else status,
                        "ai_confidence": verdict.get("confidence", ""),
                        "ai_reason": (verdict.get("reason") or error or "")[:300],
                        "final_decision": rec["deterministic_decision"],
                    })
        except OSError as exc:
            log.warning("ai_opinions.csv write failed: %s", exc)

    def status(self) -> dict:
        return {"last_status": self.last_status,
                "pending": self._pending,
                "last_duration_s": self.last_duration_s,
                "last_queue_delay_s": self.last_queue_delay_s}


class ScanCoordinator:
    """Process-level single-scan gate + scan observability."""

    def __init__(self, guard_factory: Callable):
        self._lock = threading.Lock()          # the single-scan gate
        self._guard_factory = guard_factory
        self._guard = None
        self._guard_lock = threading.Lock()

        self.scan_id: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.stages: dict[str, float] = {}
        self.last_scan_id: Optional[str] = None
        self.last_scan_at: Optional[datetime] = None
        self.last_duration_s: float = 0.0
        self.last_summary: dict = {}
        self.error_streak: int = 0

    # -- the gate ------------------------------------------------------------
    def try_begin(self, scan_id: str) -> bool:
        """Atomically claim the scan slot. False => a scan is already active."""
        got = self._lock.acquire(blocking=False)
        if got:
            self.scan_id = scan_id
            self.started_at = datetime.now(config.TZ)
            self.stages = {}
        return got

    def end(self, summary: dict) -> None:
        """Release the scan slot and record the outcome snapshot."""
        if self.started_at is not None:
            self.last_duration_s = round(
                (datetime.now(config.TZ) - self.started_at).total_seconds(), 1)
        self.last_scan_id = self.scan_id
        self.last_scan_at = self.started_at
        self.last_summary = dict(summary)
        if summary.get("error"):
            self.error_streak += 1
        elif summary.get("scanned", 0) > 0:
            self.error_streak = 0
        self.scan_id = None
        self.started_at = None
        try:
            self._lock.release()
        except RuntimeError:
            pass

    # -- timings ---------------------------------------------------------------
    def stage(self, name: str) -> _StageTimer:
        return _StageTimer(self.stages, name)

    # -- shared duplicate guard --------------------------------------------------
    def get_guard(self):
        """The one DuplicateGuard shared by every scan path."""
        with self._guard_lock:
            if self._guard is None:
                self._guard = self._guard_factory()
            return self._guard

    # -- health ---------------------------------------------------------------
    def health(self) -> dict:
        active = self._lock.locked()
        return {
            "active": active,
            "active_scan_id": self.scan_id,
            "last_scan_id": self.last_scan_id,
            "last_scan_at": self.last_scan_at.strftime("%H:%M:%S")
            if self.last_scan_at else None,
            "last_duration_s": self.last_duration_s,
            "last_signals": self.last_summary.get("signals", 0),
            "error_streak": self.error_streak,
            "stages": dict(self.stages),
        }
