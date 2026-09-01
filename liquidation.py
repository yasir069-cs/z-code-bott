"""Binance Futures force-order stream and an in-memory liquidation cache.

The listener is deliberately independent from the scan loop.  Scans only read
the cache and therefore never wait for a websocket event or reconnect.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque

import config

log = logging.getLogger("liquidation")

_STREAM_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
_DEFAULT_WINDOWS = {"5m": 300, "15m": 900, "1h": 3600}


def _configured_windows() -> dict:
    """Lookback windows (name -> seconds) from config, validated.

    Invalid entries are dropped; a wholly invalid/empty mapping falls back to
    the defaults so a bad override can never produce empty summaries."""
    raw = getattr(config, "LIQUIDATION_WINDOWS", None)
    if isinstance(raw, dict) and raw:
        windows = {}
        for name, seconds in raw.items():
            try:
                seconds = int(seconds)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                windows[str(name)] = seconds
        if windows:
            return windows
    return dict(_DEFAULT_WINDOWS)


def _norm(symbol: str) -> str:
    """Normalize to Binance's raw stream form: 'BTC/USDT:USDT' -> 'BTCUSDT'.

    The websocket caches events under the exchange's raw symbol (BTCUSDT),
    while the scan pipeline speaks ccxt symbols — without this the lookup
    never matches and every summary would read "no events"."""
    return str(symbol or "").upper().split(":")[0].replace("/", "")


class LiquidationCache:
    """Thread-safe rolling cache of normalized force-order events."""

    def __init__(self, max_age_seconds: int | None = None):
        self._events = defaultdict(deque)
        # Retention defaults to the largest configured window: every event a
        # summary might still count must stay in the rolling cache.
        self._max_age = max_age_seconds or max(_configured_windows().values())
        self._lock = threading.RLock()
        self._connected = False
        self._last_message_at = None
        self._warning = "liquidation stream has not connected"

    def add_event(self, event: dict) -> None:
        symbol = _norm(event.get("symbol", ""))
        if not symbol:
            return
        normalized = {
            "symbol": symbol,
            "side": str(event.get("side", "")).upper(),
            "price": float(event.get("price", 0) or 0),
            "quantity": float(event.get("quantity", 0) or 0),
            "notional": float(event.get("notional", 0) or 0),
            "timestamp": float(event.get("timestamp", time.time())),
        }
        with self._lock:
            self._events[symbol].append(normalized)
            self._last_message_at = time.time()
            self._warning = ""
            self._prune_locked(now=normalized["timestamp"])

    def set_connection(self, connected: bool, warning: str = "") -> None:
        with self._lock:
            self._connected = connected
            self._warning = warning

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def stream_status(self) -> dict:
        """Application-level stream health: an open socket is not health.

        FRESH      — connected and messages arriving within the threshold
        STALE      — connected but no message for > LIQ_STALE_SECONDS (the
                     ping/pong keepalive proves the TRANSPORT is alive while
                     the feed itself has gone quiet — old events must not be
                     presented as current information)
        DISCONNECTED — no socket at all
        """
        with self._lock:
            connected = self._connected
            last = self._last_message_at
        if not connected:
            return {"status": "DISCONNECTED", "age_s": None}
        if last is None:
            return {"status": "STALE", "age_s": None}
        age = max(0.0, time.time() - last)
        status = "FRESH" if age <= config.LIQ_STALE_SECONDS else "STALE"
        return {"status": status, "age_s": round(age, 1)}

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._max_age
        for symbol, events in list(self._events.items()):
            while events and events[0]["timestamp"] < cutoff:
                events.popleft()
            if not events:
                del self._events[symbol]

    def summary(self, symbol: str, current_price=None, sr=None) -> dict:
        """Return honest per-window aggregates; never estimate missing data.

        A STALE stream downgrades availability: cached events keep their
        per-window numbers (they are historical facts), but the summary is
        marked unavailable-with-warning so downstream logic cannot treat
        'no recent events' as a fresh reading of the market."""
        symbol = _norm(symbol)
        now = time.time()
        stream = self.stream_status()
        with self._lock:
            self._prune_locked(now)
            events = list(self._events.get(symbol, ()))
            stream_warning = self._warning
        windows = {}
        for name, seconds in _configured_windows().items():
            recent = [e for e in events if now - e["timestamp"] <= seconds]
            long_events = [e for e in recent if e["side"] == "SELL"]
            short_events = [e for e in recent if e["side"] == "BUY"]
            windows[name] = {
                "long_notional": round(sum(e["notional"] for e in long_events), 8),
                "long_count": len(long_events),
                "short_notional": round(sum(e["notional"] for e in short_events), 8),
                "short_count": len(short_events),
                "burst": len(recent) >= config.LIQUIDATION_BURST_COUNT,
            }
        latest = max(events, key=lambda e: e["timestamp"], default=None)
        stream_ok = stream["status"] == "FRESH"
        available = bool(events) and stream_ok
        warning = ""
        if not available:
            if stream["status"] == "STALE":
                age = f"({stream['age_s']:.0f}s since last message)" if stream["age_s"] \
                    else "(no message since start)"
                warning = f"liquidation stream stale {age}"
            elif stream["status"] == "DISCONNECTED":
                warning = stream_warning or "liquidation stream disconnected"
            elif not events:
                warning = "no liquidation events in cache"
        price_context = []
        if available and current_price:
            for event in events:
                price_context.append({
                    "event_price": event["price"],
                    "price_delta_pct": round((event["price"] / current_price - 1) * 100, 4)
                    if event["price"] else None,
                })
        if available and sr:
            levels = []
            for key in ("nearest_support", "nearest_resistance"):
                zone = sr.get(key)
                if zone:
                    levels.append({"type": key, "mid": zone.get("mid"),
                                   "distance": zone.get("distance")})
            price_context.append({"nearby_sr": levels})
        freshness = round(max(0.0, now - latest["timestamp"]), 1) if latest else None
        return {"available": available, "warning": warning,
                "connected": stream["status"] != "DISCONNECTED",
                "stream_status": stream["status"],
                "freshness_seconds": freshness,
                "latest_event_timestamp": latest["timestamp"] if latest else None,
                "windows": windows, "event_price_context": price_context}


class LiquidationListener:
    """Persistent reconnecting daemon listener for Binance force orders."""

    def __init__(self, cache: LiquidationCache | None = None):
        self.cache = cache or LiquidationCache()
        self._stop = threading.Event()
        self._thread = None

    def _on_message(self, _ws, raw: str) -> None:
        # Binance's !forceOrder@arr wraps EVERY event in a JSON array:
        # [{"e": "forceOrder", "E": ..., "o": {...}}]. A bare object is
        # accepted too, so both stream shapes feed the cache.
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("Invalid liquidation message: %s", exc)
            return
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            self._ingest_order(item.get("o"))

    def _ingest_order(self, orders) -> None:
        if not isinstance(orders, dict) or not orders:
            return
        try:
            side = str(orders.get("S", "")).upper()
            price = float(orders.get("ap") or orders.get("p") or 0)
            quantity = float(orders.get("z") or orders.get("q") or 0)
            self.cache.add_event({"symbol": orders.get("s"), "side": side,
                                  "price": price, "quantity": quantity,
                                  "notional": price * quantity,
                                  "timestamp": float(orders.get("T", 0)) / 1000 or time.time()})
        except (TypeError, ValueError) as exc:
            log.warning("Invalid liquidation event: %s", exc)

    def _on_open(self, _ws) -> None:
        self.cache.set_connection(True)
        log.info("Binance liquidation websocket connected")

    def _on_close(self, _ws, _code, _message) -> None:
        self.cache.set_connection(False, "liquidation stream disconnected")

    def _run(self) -> None:
        # Import lazily so cache-only consumers and tests do not require the
        # optional websocket client until the persistent listener is started.
        try:
            import websocket
        except ImportError as exc:
            self.cache.set_connection(False, f"websocket client unavailable: {exc}")
            log.error("Liquidation listener disabled: websocket-client is not installed")
            return
        while not self._stop.is_set():
            try:
                self.cache.set_connection(False, "liquidation stream disconnected")
                ws = websocket.WebSocketApp(_STREAM_URL, on_open=self._on_open,
                                            on_close=self._on_close,
                                            on_message=self._on_message)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                log.warning("Liquidation websocket error: %s", exc)
                self.cache.set_connection(False, f"liquidation websocket error: {exc}")
            if not self._stop.is_set():
                self._stop.wait(config.LIQUIDATION_RECONNECT_SECONDS)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="BinanceLiquidations")
        self._thread.start()
        self._start_watchdog()
        log.info("Binance liquidation listener started")

    def _start_watchdog(self) -> None:
        """Log (once per transition) when the stream goes stale, so a quiet
        socket cannot silently masquerade as live liquidation data."""
        def _watch():
            warned = False
            while not self._stop.is_set():
                status = self.cache.stream_status()
                if status["status"] == "STALE" and not warned:
                    age = f"{status['age_s']:.0f}s" if status["age_s"] else "since start"
                    log.warning("Liquidation stream STALE — no message for %s; "
                                "summaries degrade to unavailable", age)
                    warned = True
                elif status["status"] == "FRESH":
                    warned = False
                self._stop.wait(60)
        threading.Thread(target=_watch, daemon=True,
                         name="LiquidationWatchdog").start()

    def stop(self) -> None:
        self._stop.set()


_listener = LiquidationListener()


def start_listener() -> LiquidationCache:
    _listener.start()
    return _listener.cache


def stream_status() -> dict:
    """Stream-level health for /status (FRESH/STALE/DISCONNECTED + age)."""
    return _listener.cache.stream_status()


def get_summary(symbol: str, current_price=None, sr=None) -> dict:
    return _listener.cache.summary(symbol, current_price=current_price, sr=sr)
