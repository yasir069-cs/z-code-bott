"""Phase 1 — Data Foundation.

Full Binance exchange scan via CCXT public mode (no API key):
  * fetch_tickers() -> ALL USDT pairs, fetched dynamically (never hardcoded)
  * volume filter: skip coins with 24h quote volume < $5M
  * fetch 1H / 15M / 5M OHLCV (last 50 closed candles each)

Retry policy (rules.md): exchange fetch fails -> retry 3x with exponential
backoff 1s -> 2s -> 4s, then skip the coin.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import ccxt
import pandas as pd

import config

log = logging.getLogger("scanner")

_TIMEFRAME_DELTA = {"1h": pd.Timedelta(hours=1), "15m": pd.Timedelta(minutes=15), "5m": pd.Timedelta(minutes=5)}

# Leveraged tokens are not spot trading targets for this strategy.
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def make_exchange() -> ccxt.Exchange:
    """Binance public market data. No keys, no trading endpoints."""
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def get_active_usdt_symbols(exchange: ccxt.Exchange) -> dict[str, float]:
    """Return {symbol: 24h quote volume} for every active, spot USDT pair
    whose 24h volume >= VOLUME_MIN_USDT, ordered by volume (desc)."""
    exchange.load_markets(reload=True)
    markets = exchange.markets

    tickers = exchange.fetch_tickers()
    result: dict[str, float] = {}
    for symbol, ticker in tickers.items():
        market = markets.get(symbol)
        if market is None or not market.get("spot", False) or not market.get("active", False):
            continue
        if market.get("quote") != "USDT":
            continue
        base = market.get("base", "")
        if any(base.endswith(suffix) and len(base) > len(suffix) for suffix in _LEVERAGED_SUFFIXES):
            continue
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            continue
        quote_volume = float(quote_volume)
        if quote_volume < config.VOLUME_MIN_USDT:
            continue
        result[symbol] = quote_volume

    ordered = dict(sorted(result.items(), key=lambda kv: kv[1], reverse=True))
    log.info("Exchange scan: %d total tickers, %d USDT pairs with 24h volume >= $%s",
             len(tickers), len(ordered), f"{config.VOLUME_MIN_USDT:,}")
    return ordered


def _backoff_sleep(attempt: int) -> None:
    """Exponential backoff: 1s -> 2s -> 4s."""
    time.sleep(2 ** (attempt - 1))


def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str,
                limit: int = config.CANDLE_LIMIT,
                warmup: int = config.INDICATOR_WARMUP) -> Optional[pd.DataFrame]:
    """Fetch CLOSED candles for symbol/timeframe.

    Returns the last `limit + warmup` closed candles: the final `limit`
    rows are the strategy window (zone/swing/volume logic), the earlier
    `warmup` rows let the pandas-ta recursions converge to TradingView
    values. The still-forming candle is dropped. Returns None after 3
    failed attempts (caller skips the coin).
    """
    if timeframe not in _TIMEFRAME_DELTA:
        raise ValueError(f"unsupported timeframe: {timeframe}")

    want = limit + warmup
    for attempt in range(1, config.FETCH_RETRY_MAX + 1):
        try:
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
            return closed.tail(want)
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("%s %s: fetch attempt %d/%d failed: %s",
                        symbol, timeframe, attempt, config.FETCH_RETRY_MAX, exc)
            if attempt < config.FETCH_RETRY_MAX:
                _backoff_sleep(attempt)
    log.error("%s %s: fetch failed after %d attempts, skipping coin", symbol, timeframe, config.FETCH_RETRY_MAX)
    return None


def fetch_all_timeframes(exchange: ccxt.Exchange, symbol: str) -> Optional[dict[str, pd.DataFrame]]:
    """Fetch 1H + 15M + 5M closed candles for one symbol, or None if any fails."""
    frames: dict[str, pd.DataFrame] = {}
    for timeframe in config.TIMEFRAMES:
        df = fetch_ohlcv(exchange, symbol, timeframe)
        if df is None:
            return None
        frames[timeframe] = df
    return frames
