"""Phase 1 — Data Foundation.

Full Binance USDT-M Futures exchange scan via CCXT public mode (no API key):
  * fetch_tickers() -> ALL USDT perpetual pairs, fetched dynamically (never hardcoded)
  * volume filter: skip coins with 24h quote volume < VOLUME_MIN_USDT
  * stablecoin/leveraged-token blacklist: those can never be valid futures setups
  * fetch 1H / 15M / 5M OHLCV (last CANDLE_LIMIT closed candles + warm-up)

Retry policy (rules.md): exchange fetch fails -> retry 3x with exponential
backoff 1s -> 2s -> 4s, then skip the coin.

Timeliness (the owner's "alerts must never be late" requirement) is handled
by three mechanisms in this module:

  1. A candle-boundary OHLCV cache. A cached frame stays valid until the
     moment the NEXT candle closes, so 1H data is fetched once an hour
     instead of once per 5-minute scan. This is the single biggest saving:
     ~152 fetches per scan drops to ~11-50 in steady state.
  2. Concurrent fetching over a thread pool, throttled by a shared token
     bucket so the workers cannot stampede Binance's weight limit. Each
     thread gets its own ccxt instance because they are not thread-safe.
  3. A cached market map. load_markets(reload=True) ran every scan and cost
     3-18s before any real work started; it now reloads hourly.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import ccxt
import pandas as pd

import config

log = logging.getLogger("scanner")

_TIMEFRAME_DELTA = {"1h": pd.Timedelta(hours=1), "15m": pd.Timedelta(minutes=15), "5m": pd.Timedelta(minutes=5)}

# Leveraged tokens are not valid futures targets for this strategy.
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def make_exchange() -> ccxt.Exchange:
    """Binance USDT-M Futures public market data. No keys, no trading endpoints."""
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})


# --------------------------------------------------------------- rate limiting

class TokenBucket:
    """Thread-safe token bucket.

    Binance's USDT-M weight budget is 2400/min and an OHLCV call costs 5, so
    ~6 requests/second is the sustainable ceiling. `enableRateLimit` on each
    ccxt instance only paces that instance; with a pool of workers we need one
    shared limiter or 8 threads each pace themselves and collectively burst.
    """

    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity if capacity is not None else rate_per_sec)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until `tokens` are available, then consume them."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            time.sleep(min(wait, 1.0))


_bucket = TokenBucket(config.FETCH_RATE_LIMIT_PER_SEC)

# ccxt exchange objects carry mutable per-request state, so each worker thread
# gets its own rather than sharing one across the pool.
_thread_local = threading.local()


def thread_exchange() -> ccxt.Exchange:
    """The calling thread's own exchange instance, created on first use."""
    ex = getattr(_thread_local, "exchange", None)
    if ex is None:
        ex = make_exchange()
        # Share the already-loaded market map so workers don't each re-fetch it.
        if _markets_cache.markets is not None:
            ex.markets = _markets_cache.markets
            ex.markets_by_id = _markets_cache.markets_by_id
            ex.symbols = list(_markets_cache.markets.keys())
        _thread_local.exchange = ex
    return ex


# ------------------------------------------------------------- markets caching

class _MarketsCache:
    """load_markets() result, reloaded at most every MARKETS_RELOAD_MIN minutes.

    Previously reload=True ran on every scan, adding a variable 3-18s before
    the first candle was even fetched. The instrument list does not change
    minute to minute.
    """

    def __init__(self):
        self.markets: Optional[dict] = None
        self.markets_by_id: Optional[dict] = None
        self._loaded_at: float = 0.0
        self._lock = threading.Lock()

    def get(self, exchange: ccxt.Exchange, force: bool = False) -> dict:
        with self._lock:
            age_min = (time.monotonic() - self._loaded_at) / 60.0
            if self.markets is None or force or age_min >= config.MARKETS_RELOAD_MIN:
                reason = "first load" if self.markets is None else f"age {age_min:.0f}min"
                exchange.load_markets(reload=True)
                self.markets = exchange.markets
                self.markets_by_id = getattr(exchange, "markets_by_id", None)
                self._loaded_at = time.monotonic()
                log.info("Markets loaded (%s): %d instruments", reason, len(self.markets))
            else:
                # Point this instance at the cached map without an API call.
                exchange.markets = self.markets
                if self.markets_by_id is not None:
                    exchange.markets_by_id = self.markets_by_id
                exchange.symbols = list(self.markets.keys())
            return self.markets

    def reset(self) -> None:
        with self._lock:
            self.markets = None
            self.markets_by_id = None
            self._loaded_at = 0.0


_markets_cache = _MarketsCache()


# ---------------------------------------------------------------- OHLCV caching

class _OHLCVCache:
    """OHLCV frames keyed by (symbol, timeframe, want), expiring on the candle
    boundary rather than on a fixed TTL.

    A frame whose last closed candle opened at T is valid until T + 2*period —
    the instant the next candle closes and new data actually exists. So a 1H
    frame survives every 5-minute scan within the hour, while a 5M frame
    expires each scan. Nothing stale is ever served.
    """

    def __init__(self):
        self._entries: dict[tuple, tuple[pd.DataFrame, pd.Timestamp]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _expiry(df: pd.DataFrame, timeframe: str) -> pd.Timestamp:
        return df.index[-1] + 2 * _TIMEFRAME_DELTA[timeframe]

    def get(self, symbol: str, timeframe: str, want: int) -> Optional[pd.DataFrame]:
        if not config.OHLCV_CACHE_ENABLED:
            return None
        key = (symbol, timeframe, want)
        now = pd.Timestamp(datetime.now(timezone.utc))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            df, expires_at = entry
            if now >= expires_at:
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return df

    def put(self, symbol: str, timeframe: str, want: int, df: pd.DataFrame) -> None:
        if not config.OHLCV_CACHE_ENABLED or df is None or df.empty:
            return
        with self._lock:
            self._entries[(symbol, timeframe, want)] = (df, self._expiry(df, timeframe))

    def prune(self) -> int:
        """Drop expired entries so a long session cannot grow the cache without
        bound as coins enter and leave the volume filter."""
        now = pd.Timestamp(datetime.now(timezone.utc))
        with self._lock:
            stale = [k for k, (_, exp) in self._entries.items() if now >= exp]
            for k in stale:
                del self._entries[k]
            return len(stale)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }

    def reset_counters(self) -> None:
        with self._lock:
            self.hits = 0
            self.misses = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0


_ohlcv_cache = _OHLCVCache()


def cache_stats() -> dict:
    """Cache counters, for the scan summary and /status."""
    return _ohlcv_cache.stats()


def prune_cache() -> int:
    return _ohlcv_cache.prune()


def reset_cache_counters() -> None:
    _ohlcv_cache.reset_counters()


def clear_cache() -> None:
    """Drop every cached frame (used by tests and by --once runs)."""
    _ohlcv_cache.clear()


# ------------------------------------------------------------------- discovery

def _is_excluded_base(base: str) -> bool:
    """Stablecoins, fiat and leveraged tokens can never be a valid setup for
    this strategy — a USDC or EUR perp has no trend to sweep. They were
    reaching the AI stage and burning the scarce free-tier budget.
    """
    if base in config.EXCLUDED_BASES:
        return True
    return any(base.endswith(sfx) and len(base) > len(sfx) for sfx in _LEVERAGED_SUFFIXES)


def get_active_usdt_symbols(exchange: ccxt.Exchange,
                             prefetched_tickers: dict | None = None) -> dict[str, float]:
    """Return {symbol: 24h quote volume} for every active, USDT-M futures
    perpetual pair whose 24h volume >= VOLUME_MIN_USDT, ordered by volume (desc).

    When *prefetched_tickers* is provided (the raw dict from
    exchange.fetch_tickers()), we skip the redundant API call.
    """
    markets = _markets_cache.get(exchange)

    # Reuse pre-fetched tickers when available to avoid a second API call
    tickers = prefetched_tickers if prefetched_tickers else exchange.fetch_tickers()

    result: dict[str, float] = {}
    excluded = 0
    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if market is None or not market.get("swap", False) or not market.get("active", False):
            continue
        # Only USDT-margined (linear) perpetuals, skip coin-margined (inverse)
        if not market.get("linear", False):
            continue
        if market.get("quote") != "USDT":
            continue
        if _is_excluded_base(market.get("base", "")):
            excluded += 1
            continue
        quote_volume = ticker.get("quoteVolume") if isinstance(ticker, dict) else None
        if quote_volume is None:
            continue
        quote_volume = float(quote_volume)
        if quote_volume < config.VOLUME_MIN_USDT:
            continue
        result[symbol] = quote_volume

    ordered = dict(sorted(result.items(), key=lambda kv: kv[1], reverse=True))
    log.info("Futures scan: %d total tickers, %d USDT-M perps with 24h volume >= $%s "
             "(%d stablecoin/leveraged excluded)",
             len(tickers), len(ordered), f"{config.VOLUME_MIN_USDT:,}", excluded)
    return ordered


# --------------------------------------------------------------------- fetching

def _backoff_sleep(attempt: int) -> None:
    """Exponential backoff: 1s -> 2s -> 4s."""
    time.sleep(2 ** (attempt - 1))


def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str,
                limit: int = config.CANDLE_LIMIT,
                warmup: int = config.INDICATOR_WARMUP,
                use_cache: bool = True) -> Optional[pd.DataFrame]:
    """Fetch CLOSED candles for symbol/timeframe.

    Returns the last `limit + warmup` closed candles: the final `limit`
    rows are the strategy window (zone/swing/volume logic), the earlier
    `warmup` rows let the pandas-ta recursions converge to TradingView
    values. The still-forming candle is dropped. Returns None after 3
    failed attempts (caller skips the coin).

    Served from the candle-boundary cache when a valid frame is held.
    """
    if timeframe not in _TIMEFRAME_DELTA:
        raise ValueError(f"unsupported timeframe: {timeframe}")

    want = limit + warmup

    if use_cache:
        cached = _ohlcv_cache.get(symbol, timeframe, want)
        if cached is not None:
            return cached

    for attempt in range(1, config.FETCH_RETRY_MAX + 1):
        try:
            _bucket.acquire()
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=want + 1)
            if not rows or len(rows) < want:
                log.warning("%s %s: exchange returned %d rows (< %d), skipping",
                            symbol, timeframe, len(rows or []), want)
                return None
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp").astype("float64")

            now = datetime.now(timezone.utc)
            tf_delta = _TIMEFRAME_DELTA[timeframe]
            closed = df[df.index + tf_delta <= pd.Timestamp(now)]
            if len(closed) < want:  # fresh listing; not enough closed history
                log.warning("%s %s: only %d closed candles available, skipping",
                            symbol, timeframe, len(closed))
                return None
            result = closed.tail(want)
            if use_cache:
                _ohlcv_cache.put(symbol, timeframe, want, result)
            return result
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("%s %s: fetch attempt %d/%d failed: %s",
                        symbol, timeframe, attempt, config.FETCH_RETRY_MAX, exc)
            if attempt < config.FETCH_RETRY_MAX:
                _backoff_sleep(attempt)
    log.error("%s %s: fetch failed after %d attempts, skipping coin", symbol, timeframe, config.FETCH_RETRY_MAX)
    return None


def fetch_timeframe_batch(symbols: list[str], timeframe: str,
                          max_workers: int = config.FETCH_MAX_WORKERS,
                          deadline: Optional[float] = None) -> dict[str, pd.DataFrame]:
    """Fetch one timeframe for many symbols concurrently.

    Returns {symbol: frame} containing only the symbols that succeeded — a
    failed or skipped coin is simply absent, matching fetch_ohlcv's contract.

    `deadline` is an optional time.monotonic() value; symbols not yet started
    when it passes are abandoned so a slow exchange can never push the scan
    into the next 5-minute slot. Anything dropped is logged, never silent.
    """
    if not symbols:
        return {}

    frames: dict[str, pd.DataFrame] = {}
    abandoned = 0

    def _work(sym: str):
        if deadline is not None and time.monotonic() > deadline:
            return sym, None, True
        return sym, fetch_ohlcv(thread_exchange(), sym, timeframe), False

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix=f"fetch-{timeframe}") as pool:
        futures = [pool.submit(_work, s) for s in symbols]
        for fut in as_completed(futures):
            try:
                sym, df, skipped = fut.result()
            except Exception as exc:  # a worker must never kill the scan
                log.warning("%s batch worker raised: %s", timeframe, exc)
                continue
            if skipped:
                abandoned += 1
            elif df is not None:
                frames[sym] = df

    elapsed = time.monotonic() - started
    if abandoned:
        log.warning("%s batch: %d/%d symbols abandoned at the scan deadline",
                    timeframe, abandoned, len(symbols))
    log.info("%s batch: %d/%d frames in %.1fs (%d workers, cache %s)",
             timeframe, len(frames), len(symbols), elapsed, max_workers,
             cache_stats()["hit_rate"])
    return frames


def fetch_all_timeframes(exchange: ccxt.Exchange, symbol: str) -> Optional[dict[str, pd.DataFrame]]:
    """Fetch 1H + 15M + 5M closed candles for one symbol, or None if any fails."""
    frames: dict[str, pd.DataFrame] = {}
    for timeframe in config.TIMEFRAMES:
        df = fetch_ohlcv(exchange, symbol, timeframe)
        if df is None:
            return None
        frames[timeframe] = df
    return frames


def fetch_funding_rates(exchange: ccxt.Exchange) -> dict[str, float]:
    """Fetch current funding rates for all USDT-M futures pairs.

    Returns {symbol: funding_rate} dict. Funding rate is a decimal
    (e.g. 0.0001 = 0.01%). Returns empty dict on failure (non-fatal).
    """
    for attempt in range(1, config.FETCH_RETRY_MAX + 1):
        try:
            _bucket.acquire()
            rates = exchange.fetch_funding_rates()
            result = {}
            for symbol, data in rates.items():
                if isinstance(data, dict) and data.get("fundingRate") is not None:
                    result[symbol] = float(data["fundingRate"])
            log.info("Funding rates fetched: %d symbols", len(result))
            return result
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("Funding rate fetch attempt %d/%d failed: %s",
                        attempt, config.FETCH_RETRY_MAX, exc)
            if attempt < config.FETCH_RETRY_MAX:
                _backoff_sleep(attempt)
    log.error("Funding rate fetch failed after %d attempts, continuing without", config.FETCH_RETRY_MAX)
    return {}


def fetch_open_interest_history(exchange: ccxt.Exchange, symbol: str,
                                timeframe: str = config.OI_HISTORY_TIMEFRAME,
                                limit: int = config.OI_HISTORY_LIMIT) -> Optional[pd.DataFrame]:
    """Fetch recent open-interest history for one symbol (futures context).

    Returns a DataFrame indexed by UTC timestamp with a single ``oi`` column
    (open-interest amount, base units), oldest→newest, or ``None`` when OI is
    disabled, unsupported, or every retry fails. Open interest is *contextual*:
    a missing value must degrade the decision safely (no fabricated data), so
    callers treat ``None`` as "OI unavailable", never as zero.

    Only called for the handful of symbols that reach the decision stage, so a
    per-symbol call here is cheap — no market-wide OI sweep.
    """
    if not config.OI_FETCH_ENABLED:
        return None
    if not exchange.has.get("fetchOpenInterestHistory"):
        return None

    for attempt in range(1, config.FETCH_RETRY_MAX + 1):
        try:
            _bucket.acquire()
            rows = exchange.fetch_open_interest_history(symbol, timeframe=timeframe, limit=limit)
            if not rows:
                return None
            recs = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                ts = r.get("timestamp")
                # ccxt normalises to openInterestAmount (base) / openInterestValue (quote);
                # fall back to the raw info payload when a key is absent.
                oi = r.get("openInterestAmount")
                if oi is None:
                    info = r.get("info") or {}
                    oi = info.get("sumOpenInterest") or info.get("openInterest")
                if ts is None or oi is None:
                    continue
                recs.append((ts, float(oi)))
            if not recs:
                return None
            df = pd.DataFrame(recs, columns=["timestamp", "oi"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df.set_index("timestamp").sort_index()
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("%s: OI-history fetch attempt %d/%d failed: %s",
                        symbol, attempt, config.FETCH_RETRY_MAX, exc)
            if attempt < config.FETCH_RETRY_MAX:
                _backoff_sleep(attempt)
    log.warning("%s: OI-history fetch failed after %d attempts, continuing without",
                symbol, config.FETCH_RETRY_MAX)
    return None
