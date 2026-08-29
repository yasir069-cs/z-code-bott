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
_WINDOWS = {"5m": 300, "15m": 900, "1h": 3600}


def _norm(symbol: str) -> str:
    """Normalize to Binance's raw stream form: 'BTC/USDT:USDT' -> 'BTCUSDT'.

    The websocket caches events under the exchange's raw symbol (BTCUSDT),
    while the scan pipeline speaks ccxt symbols — without this the lookup
    never matches and every summary would read "no events"."""
    return str(symbol or "").upper().split(":")[0].replace("/", "")


class LiquidationCache:
    """Thread-safe rolling cache of normalized force-order events."""

    def __init__(self, max_age_seconds: int = 3600):
        self._events = defaultdict(deque)
        self._max_age = max_age_seconds
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

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._max_age
        for symbol, events in list(self._events.items()):
            while events and events[0]["timestamp"] < cutoff:
                events.popleft()
            if not events:
                del self._events[symbol]

    def summary(self, symbol: str, current_price=None, sr=None) -> dict:
        """Return honest per-window aggregates; never estimate missing data."""
        symbol = _norm(symbol)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            events = list(self._events.get(symbol, ()))
            connected = self._connected
            stream_warning = self._warning
        windows = {}
        for name, seconds in _WINDOWS.items():
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
        available = bool(events) and connected
        warning = "" if available else (stream_warning or "no liquidation events in cache")
        if events and not connected:
            warning = stream_warning or "liquidation stream disconnected"
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
        return {"available": available, "warning": warning, "connected": connected,
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
        try:
            payload = json.loads(raw)
            orders = payload.get("o") if isinstance(payload, dict) else None
            if not orders:
                return
            side = orders.get("S", "")
            price = float(orders.get("ap") or orders.get("p") or 0)
            quantity = float(orders.get("z") or orders.get("q") or 0)
            self.cache.add_event({"symbol": orders.get("s"), "side": side,
                                  "price": price, "quantity": quantity,
                                  "notional": price * quantity,
                                  "timestamp": float(orders.get("T", 0)) / 1000 or time.time()})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
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
        log.info("Binance liquidation listener started")

    def stop(self) -> None:
        self._stop.set()


_listener = LiquidationListener()


def start_listener() -> LiquidationCache:
    _listener.start()
    return _listener.cache


def get_summary(symbol: str, current_price=None, sr=None) -> dict:
    return _listener.cache.summary(symbol, current_price=current_price, sr=sr)
