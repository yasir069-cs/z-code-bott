"""Phase 9 — Duplicate guard.

Two independent cooldowns, both in-memory and thread-safe for the scheduler:

  * **Alert cooldown** (`DUPLICATE_COOLDOWN_MIN`, 15 min): a coin that got a
    BUY/SELL alert is not alerted again until it expires, and it is skipped
    *before* any 15M/5M fetch is paid for. Checked for every candidate,
    recorded only for alerted signals — a silent HOLD must not be able to mute
    a real setup that appears 5 minutes later.
  * **HOLD log cooldown** (`HOLD_LOG_COOLDOWN_MIN`, 30 min): the decision core
    rejects most coins on most scans, and every rejection used to append a
    fresh CSV row every 5 minutes (~2.2k HOLD rows per session, 24/7 in an
    on-demand session). An *identical* rejection — same verdict, same blocking
    reason — is written once per window; a new reason or a changed verdict is
    always written, because that is the information the owner reads.

The whole tracker resets at session end (23:00 IST, scheduled in main.py).
"""
import logging
import threading
from datetime import datetime, timedelta

import config

log = logging.getLogger("duplicate_guard")


class DuplicateGuard:
    def __init__(self, cooldown_minutes: int = config.DUPLICATE_COOLDOWN_MIN,
                 hold_log_minutes: int = config.HOLD_LOG_COOLDOWN_MIN):
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._hold_cooldown = timedelta(minutes=hold_log_minutes)
        self._last_signal: dict[str, datetime] = {}
        self._last_hold: dict[str, tuple[datetime, str]] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, symbol: str, now: datetime) -> bool:
        """True when this coin alerted within the alert cooldown window."""
        with self._lock:
            last = self._last_signal.get(symbol)
            return last is not None and (now - last) < self._cooldown

    def record(self, symbol: str, now: datetime) -> None:
        with self._lock:
            self._last_signal[symbol] = now

    def hold_logged_recently(self, symbol: str, now: datetime,
                             reason: str = "") -> bool:
        """True when this exact rejection was already written inside the window.

        `reason` is the verdict + blocking-reason signature; a different reason
        is never suppressed, so the log still shows a setup changing shape.
        """
        with self._lock:
            entry = self._last_hold.get(symbol)
        if entry is None:
            return False
        when, stored = entry
        return (now - when) < self._hold_cooldown and (not reason or stored == reason)

    def record_hold(self, symbol: str, now: datetime, reason: str = "") -> None:
        """Stamp a written HOLD row so the same blocked setup is not re-logged."""
        with self._lock:
            self._last_hold[symbol] = (now, reason)

    def reset(self) -> None:
        """Session-end reset — forget every coin."""
        with self._lock:
            alerts_n = len(self._last_signal)
            holds_n = len(self._last_hold)
            self._last_signal.clear()
            self._last_hold.clear()
        log.info("Duplicate tracker reset (%d alert / %d hold entries cleared)",
                 alerts_n, holds_n)

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._last_signal)

    def hold_tracked_count(self) -> int:
        with self._lock:
            return len(self._last_hold)
