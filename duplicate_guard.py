"""Phase 9 — Duplicate guard.

Tracks the last signal timestamp per coin in an in-memory dict (thread-safe
for the scheduler). Same coin within 20 minutes -> skip silently. The whole
tracker resets daily at 9:30 PM IST (scheduled in main.py).
"""
import logging
import threading
from datetime import datetime, timedelta

import config

log = logging.getLogger("duplicate_guard")


class DuplicateGuard:
    def __init__(self, cooldown_minutes: int = config.DUPLICATE_COOLDOWN_MIN):
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._last_signal: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, symbol: str, now: datetime) -> bool:
        """True when this coin already signaled within the cooldown window."""
        with self._lock:
            last = self._last_signal.get(symbol)
            return last is not None and (now - last) < self._cooldown

    def record(self, symbol: str, now: datetime) -> None:
        with self._lock:
            self._last_signal[symbol] = now

    def reset(self) -> None:
        """Daily 9:30 PM IST reset — forget every coin."""
        with self._lock:
            count = len(self._last_signal)
            self._last_signal.clear()
        log.info("Duplicate tracker reset (%d coins cleared)", count)

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._last_signal)
