"""Phase 2 tests — fetch caching, rate limiting and symbol exclusion.

These cover the timeliness work: the owner's requirement is that alerts are
never late, and the dominant cost in a scan was refetching 1H candles for
~100 coins every 5 minutes when 1H data changes once an hour.

No network access — the exchange is stubbed.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import config
import scanner


@pytest.fixture(autouse=True)
def _clean_cache():
    scanner.clear_cache()
    yield
    scanner.clear_cache()


def _ohlcv_rows(tf: str, n: int = 320, last_close_ago_min: float = 1.0):
    """Raw ccxt-style rows whose most recent CLOSED candle closed
    `last_close_ago_min` minutes ago, plus one still-forming candle."""
    delta = scanner._TIMEFRAME_DELTA[tf]
    now = pd.Timestamp(datetime.now(timezone.utc))
    last_open = now - pd.Timedelta(minutes=last_close_ago_min) - delta
    opens = pd.date_range(end=last_open, periods=n, freq=delta)
    rows = [[int(ts.timestamp() * 1000), 100.0, 101.0, 99.0, 100.5, 1000.0] for ts in opens]
    # the still-forming candle the fetcher must drop
    rows.append([int((last_open + delta).timestamp() * 1000), 100.5, 101.0, 100.0, 100.7, 500.0])
    return rows


class _StubExchange:
    """Counts fetch_ohlcv calls so cache behaviour is observable."""

    def __init__(self, tf="1h", last_close_ago_min=1.0):
        self.calls = 0
        self.tf = tf
        self.last_close_ago_min = last_close_ago_min
        self.markets = {}

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
        self.calls += 1
        return _ohlcv_rows(timeframe, n=(limit or 320), last_close_ago_min=self.last_close_ago_min)


# ----------------------------------------------------------------- OHLCV cache

def test_second_fetch_within_the_candle_is_served_from_cache():
    ex = _StubExchange()
    a = scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")
    b = scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")
    assert a is not None and b is not None
    assert ex.calls == 1, "the second call must not hit the exchange"
    pd.testing.assert_frame_equal(a, b)


def _cached_frame(tf: str, closed_ago_min: float):
    """A frame whose last CLOSED candle closed `closed_ago_min` minutes ago.
    The index holds candle OPEN times, so the last open is one period earlier."""
    delta = scanner._TIMEFRAME_DELTA[tf]
    now = pd.Timestamp(datetime.now(timezone.utc))
    last_open = now - pd.Timedelta(minutes=closed_ago_min) - delta
    idx = pd.date_range(end=last_open, periods=5, freq=delta)
    return pd.DataFrame({c: np.arange(5, dtype=float)
                         for c in ["open", "high", "low", "close", "volume"]}, index=idx)


def test_cache_expires_once_a_new_candle_has_closed():
    """A 1H frame whose next candle has already closed must not be served —
    reusing it would mean alerting on a stale entry price."""
    scanner._ohlcv_cache.put("BTC/USDT:USDT", "1h", 300, _cached_frame("1h", 61))
    assert scanner._ohlcv_cache.get("BTC/USDT:USDT", "1h", 300) is None


def test_cache_serves_a_frame_for_the_rest_of_its_candle():
    """55 minutes into the hour there is still no newer 1H candle, so the
    cached frame is the freshest data that exists."""
    scanner._ohlcv_cache.put("BTC/USDT:USDT", "1h", 300, _cached_frame("1h", 55))
    assert scanner._ohlcv_cache.get("BTC/USDT:USDT", "1h", 300) is not None


def test_cache_survives_the_whole_hour_for_1h_data():
    ex = _StubExchange(last_close_ago_min=55.0)  # still inside the same hour
    for _ in range(12):  # twelve 5-minute scans
        scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")
    assert ex.calls == 1, "1H data should cost one fetch per hour, not per scan"


@pytest.mark.parametrize("tf,closed_ago,expect_hit", [
    ("5m", 1, True),    # inside the current 5M candle
    ("5m", 6, False),   # a new 5M candle has closed -> entry data must be fresh
    ("15m", 5, True),
    ("15m", 16, False),
    ("1h", 30, True),
    ("1h", 61, False),
])
def test_expiry_lands_exactly_on_the_candle_boundary(tf, closed_ago, expect_hit):
    scanner._ohlcv_cache.put("X/USDT:USDT", tf, 300, _cached_frame(tf, closed_ago))
    got = scanner._ohlcv_cache.get("X/USDT:USDT", tf, 300)
    assert (got is not None) is expect_hit


def test_cache_is_keyed_per_symbol_and_timeframe():
    ex = _StubExchange()
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")
    scanner.fetch_ohlcv(ex, "ETH/USDT:USDT", "1h")
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "15m")
    assert ex.calls == 3
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")  # all three now cached
    scanner.fetch_ohlcv(ex, "ETH/USDT:USDT", "1h")
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "15m")
    assert ex.calls == 3


def test_use_cache_false_always_refetches():
    ex = _StubExchange()
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h", use_cache=False)
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h", use_cache=False)
    assert ex.calls == 2


def test_cached_frame_still_excludes_the_forming_candle():
    """The cache must not reintroduce the bug the fetcher exists to avoid."""
    ex = _StubExchange()
    df = scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")
    now = pd.Timestamp(datetime.now(timezone.utc))
    assert (df.index + scanner._TIMEFRAME_DELTA["1h"] <= now).all()


def test_prune_drops_only_expired_entries():
    scanner._ohlcv_cache.put("FRESH/USDT:USDT", "1h", 300, _cached_frame("1h", 5))
    scanner._ohlcv_cache.put("STALE/USDT:USDT", "1h", 300, _cached_frame("1h", 61))
    assert scanner.cache_stats()["entries"] == 2
    removed = scanner.prune_cache()
    assert removed == 1
    assert scanner.cache_stats()["entries"] == 1


def test_cache_stats_track_hits_and_misses():
    ex = _StubExchange()
    scanner.reset_cache_counters()
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")   # miss
    scanner.fetch_ohlcv(ex, "BTC/USDT:USDT", "1h")   # hit
    st = scanner.cache_stats()
    assert st["hits"] == 1 and st["misses"] == 1
    assert st["hit_rate"] == 0.5


# ------------------------------------------------------------------ rate limit

def test_token_bucket_enforces_the_rate_across_threads():
    """8 workers must not collectively exceed the shared ceiling — that is the
    whole point of hoisting the limiter out of the per-instance ccxt one."""
    bucket = scanner.TokenBucket(20.0)
    start = time.monotonic()

    def burn():
        for _ in range(10):
            bucket.acquire()

    threads = [threading.Thread(target=burn) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.monotonic() - start
    # 40 tokens at 20/s, starting with a 20-token burst -> at least ~1s
    assert elapsed >= 0.8, f"bucket leaked: 40 tokens in {elapsed:.2f}s"


def test_token_bucket_rejects_a_nonsense_rate():
    with pytest.raises(ValueError):
        scanner.TokenBucket(0)


# -------------------------------------------------------------- symbol filter

@pytest.mark.parametrize("base", ["USDC", "FDUSD", "TUSD", "DAI", "EUR", "EURI",
                                  "BFUSD", "RLUSD", "USDE", "USD1"])
def test_stablecoins_are_excluded(base):
    """These reached the AI stage and burned the scarce free-tier budget."""
    assert scanner._is_excluded_base(base) is True


@pytest.mark.parametrize("base", ["ETHUP", "BTCDOWN", "ADABULL", "XRPBEAR"])
def test_leveraged_tokens_are_excluded(base):
    assert scanner._is_excluded_base(base) is True


@pytest.mark.parametrize("base", ["BTC", "ETH", "SOL", "SUSHI", "DOGE", "UP"])
def test_real_coins_are_kept(base):
    assert scanner._is_excluded_base(base) is False


def test_get_active_usdt_symbols_applies_every_filter(monkeypatch):
    markets = {
        "BTC/USDT:USDT":  dict(swap=True, active=True, linear=True, quote="USDT", base="BTC"),
        "ETH/USDT:USDT":  dict(swap=True, active=True, linear=True, quote="USDT", base="ETH"),
        "USDC/USDT:USDT": dict(swap=True, active=True, linear=True, quote="USDT", base="USDC"),
        "TINY/USDT:USDT": dict(swap=True, active=True, linear=True, quote="USDT", base="TINY"),
        "BTC/USD:BTC":    dict(swap=True, active=True, linear=False, quote="USD", base="BTC"),
        "OLD/USDT:USDT":  dict(swap=True, active=False, linear=True, quote="USDT", base="OLD"),
    }
    tickers = {
        "BTC/USDT:USDT":  {"quoteVolume": 900_000_000},
        "ETH/USDT:USDT":  {"quoteVolume": 400_000_000},
        "USDC/USDT:USDT": {"quoteVolume": 800_000_000},   # huge, but a stablecoin
        "TINY/USDT:USDT": {"quoteVolume": 1_000},         # below the floor
        "BTC/USD:BTC":    {"quoteVolume": 500_000_000},   # inverse
        "OLD/USDT:USDT":  {"quoteVolume": 500_000_000},   # delisted
    }

    class _Ex:
        def __init__(self):
            self.markets = markets
            self.symbols = list(markets)
        def load_markets(self, reload=False):
            return markets

    scanner._markets_cache.reset()
    out = scanner.get_active_usdt_symbols(_Ex(), tickers)
    assert list(out) == ["BTC/USDT:USDT", "ETH/USDT:USDT"]  # volume-ordered
    scanner._markets_cache.reset()


# ------------------------------------------------------------- markets caching

def test_markets_are_not_reloaded_on_every_scan():
    """load_markets(reload=True) per scan cost 3-18s before any real work."""
    class _Ex:
        def __init__(self):
            self.loads = 0
            self.markets = {"BTC/USDT:USDT": {}}
            self.symbols = ["BTC/USDT:USDT"]
        def load_markets(self, reload=False):
            self.loads += 1
            return self.markets

    scanner._markets_cache.reset()
    ex = _Ex()
    for _ in range(12):
        scanner._markets_cache.get(ex)
    assert ex.loads == 1, "markets should load once, not once per scan"
    scanner._markets_cache.reset()


# ---------------------------------------------------------------- batch fetch

def test_batch_returns_only_successful_symbols(monkeypatch):
    def fake(exchange, symbol, timeframe, **kw):
        if symbol == "BAD/USDT:USDT":
            return None
        return pd.DataFrame({"close": [1.0]},
                            index=pd.date_range("2026-01-01", periods=1, tz="UTC"))

    monkeypatch.setattr(scanner, "fetch_ohlcv", fake)
    monkeypatch.setattr(scanner, "thread_exchange", lambda: None)
    out = scanner.fetch_timeframe_batch(["A/USDT:USDT", "BAD/USDT:USDT", "C/USDT:USDT"], "1h")
    assert set(out) == {"A/USDT:USDT", "C/USDT:USDT"}


def test_batch_honours_the_scan_deadline(monkeypatch):
    """Past the deadline the scan must stop fetching rather than bleed into
    the next 5-minute slot."""
    monkeypatch.setattr(scanner, "thread_exchange", lambda: None)
    monkeypatch.setattr(scanner, "fetch_ohlcv",
                        lambda *a, **k: pd.DataFrame({"close": [1.0]},
                                                     index=pd.date_range("2026-01-01", periods=1, tz="UTC")))
    out = scanner.fetch_timeframe_batch(["A/USDT:USDT"] * 5, "1h",
                                        deadline=time.monotonic() - 1)
    assert out == {}, "nothing should be fetched after the deadline"


def test_batch_of_nothing_is_not_an_error():
    assert scanner.fetch_timeframe_batch([], "1h") == {}


def test_batch_survives_a_worker_exception(monkeypatch):
    """One bad coin must never kill the whole scan."""
    def fake(exchange, symbol, timeframe, **kw):
        if symbol == "BOOM/USDT:USDT":
            raise RuntimeError("exchange blew up")
        return pd.DataFrame({"close": [1.0]},
                            index=pd.date_range("2026-01-01", periods=1, tz="UTC"))

    monkeypatch.setattr(scanner, "fetch_ohlcv", fake)
    monkeypatch.setattr(scanner, "thread_exchange", lambda: None)
    out = scanner.fetch_timeframe_batch(["OK/USDT:USDT", "BOOM/USDT:USDT"], "1h")
    assert set(out) == {"OK/USDT:USDT"}


# ------------------------------------------------- thin history (fresh listings)
#
# Binance returns fewer rows than asked for a symbol listed days ago. The fetcher
# used to throw those coins away before the strategy ever saw them — a 45-bar
# frame still gives the structural core its 50-candle window, just with less
# pandas-ta warm-up, so it is degraded, not dropped.

def test_short_history_is_served_when_it_covers_the_candles():
    """45 closed 15M candles >= FRAME_MIN_CANDLES -> usable frame, one fetch."""
    want = config.CANDLE_LIMIT + config.INDICATOR_WARMUP
    rows = _ohlcv_rows("15m", n=45)
    assert len(rows) == 46                       # 45 closed + the forming candle

    class _Ex:
        def __init__(self):
            self.calls = 0
        def fetch_ohlcv(self, symbol, timeframe="15m", limit=None):
            self.calls += 1
            return rows

    ex = _Ex()
    df = scanner.fetch_ohlcv(ex, "NEWCOIN/USDT:USDT", "15m")
    assert df is not None, "a 45-candle frame must not be thrown away"
    assert len(df) == 45 < want                  # short, but real data
    assert scanner.fetch_ohlcv(ex, "NEWCOIN/USDT:USDT", "15m") is not None
    assert ex.calls == 1, "short frames are cached like any other frame"


def test_short_history_is_logged_as_degraded_not_silent(caplog):
    import logging
    rows = _ohlcv_rows("1h", n=44)

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
            return rows

    with caplog.at_level(logging.INFO, logger="scanner"):
        scanner.fetch_ohlcv(_Ex(), "NEWCOIN/USDT:USDT", "1h")
    msgs = [r.getMessage() for r in caplog.records if "short history" in r.getMessage()]
    assert len(msgs) == 1 and "44/300" in msgs[0]
    assert not [r for r in caplog.records if "skipping" in r.getMessage()]


def test_history_below_the_minimum_is_still_rejected(caplog):
    """Fewer closed candles than FRAME_MIN_CANDLES cannot support the strategy."""
    import logging
    rows = _ohlcv_rows("1h", n=config.FRAME_MIN_CANDLES - 1)

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
            return rows

    with caplog.at_level(logging.WARNING, logger="scanner"):
        assert scanner.fetch_ohlcv(_Ex(), "FRESH/USDT:USDT", "1h") is None
    assert any("skipping" in r.getMessage() for r in caplog.records)


def test_minimum_is_configurable(monkeypatch):
    """FRAME_MIN_CANDLES is the only gate: raising it must reject the same frame."""
    monkeypatch.setattr(config, "FRAME_MIN_CANDLES", 60)
    rows = _ohlcv_rows("1h", n=45)

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
            return rows

    assert scanner.fetch_ohlcv(_Ex(), "NEWCOIN/USDT:USDT", "1h") is None


def test_full_history_is_unaffected():
    rows = _ohlcv_rows("1h", n=config.CANDLE_LIMIT + config.INDICATOR_WARMUP + 1)

    class _Ex:
        def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
            return rows

    df = scanner.fetch_ohlcv(_Ex(), "BTC/USDT:USDT", "1h")
    assert len(df) == config.CANDLE_LIMIT + config.INDICATOR_WARMUP
