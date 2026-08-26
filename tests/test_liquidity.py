"""Deterministic tests for the liquidity & sweep detector."""
import numpy as np
import pandas as pd

import config
import liquidity
from conftest import make_candles


def frame(rows, start="2026-08-14 10:00", freq="1h"):
    """Build an OHLCV frame from explicit (open, high, low, close, volume) rows."""
    arr = np.array(rows, dtype="float64")
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
         "close": arr[:, 3], "volume": arr[:, 4]},
        index=idx,
    )


def zigzag(controls, seg=5):
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# 20 flat base candles: swing low = 101.5, swing high = 103.5, avg volume 1000
BASE = [(102.0, 103.5, 101.5, 103.0, 1000.0)] * 20
# sweep candle: pierces below 101.5, closes back above, long lower wick, vol spike
SWEEP = (103.0, 103.1, 99.5, 102.0, 2500.0)
# confirmation candle: bullish, holds above the reclaimed level
CONFIRM = (102.0, 105.2, 101.8, 105.0, 1200.0)


# ------------------------------------------------------------- confirmed sweep

def test_confirmed_sell_side_sweep_makes_long_ready():
    df = frame(BASE + [SWEEP, CONFIRM])
    out = liquidity.analyze(df)
    assert out["buy_sweep"] is not None
    assert out["buy_sweep"]["side"] == "sell-side"
    assert out["buy_sweep"]["reclaimed"] is True
    assert out["buy_sweep"]["confirmed"] is True
    assert out["long_ready"] is True


def test_sweep_without_confirmation_is_not_ready():
    # sweep is the final candle -> nothing has confirmed it yet
    df = frame(BASE + [SWEEP])
    out = liquidity.analyze(df)
    assert out["buy_sweep"] is not None            # the sweep is detected...
    assert out["buy_sweep"]["confirmed"] is False  # ...but unconfirmed
    assert out["long_ready"] is False              # so not tradable (mandatory confirm)


def test_bearish_close_does_not_confirm_a_long():
    bearish_follow = (102.0, 102.5, 100.0, 100.5, 1200.0)  # closes down, no hold
    df = frame(BASE + [SWEEP, bearish_follow])
    out = liquidity.analyze(df)
    assert out["buy_sweep"]["confirmed"] is False
    assert out["long_ready"] is False


# --------------------------------------------------------------- equal levels

def test_equal_lows_form_a_stop_pool():
    df = make_candles(zigzag([105, 100, 105, 100, 105, 100, 105]), wick=0.1)
    out = liquidity.analyze(df)
    assert out["equal_lows"]
    assert out["equal_lows"][0]["count"] >= config.LIQ_MIN_EQUAL


# ------------------------------------------------------------- no sweep / safe

def test_clean_trend_without_volume_spike_has_no_sweep():
    closes = zigzag([100, 104, 103, 108, 107, 112, 111, 116])
    df = make_candles(closes, volumes=[1000.0] * (len(closes)))
    out = liquidity.analyze(df)
    assert out["sweep"] is None
    assert out["long_ready"] is False and out["short_ready"] is False


def test_thin_data_degrades_safely():
    out = liquidity.analyze(make_candles([100, 101, 102, 103, 104]))
    assert out["sweep"] is None
    assert out["long_ready"] is False and out["short_ready"] is False
