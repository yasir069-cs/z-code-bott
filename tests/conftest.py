"""Shared test fixtures: synthetic OHLCV candle builders."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_candles(closes, start="2026-08-14 10:00", freq="1h", vol_base=1000.0,
                 vol_spread=0.1, wick=0.4, seed=7, volumes=None):
    """Build an OHLCV DataFrame from a close-price series.

    `volumes` overrides generated volume; `wick` scales the random high/low
    protrusion around the open/close body.
    """
    closes = np.asarray(closes, dtype="float64")
    n = len(closes)
    rng = np.random.default_rng(seed)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    spread = np.maximum(np.abs(closes - opens), 1e-9)
    highs = np.maximum(opens, closes) + wick * spread * np.abs(rng.normal(1.0, 0.2, n))
    lows = np.minimum(opens, closes) - wick * spread * np.abs(rng.normal(1.0, 0.2, n))
    if volumes is None:
        vols = vol_base * (1.0 + vol_spread * rng.normal(0, 1, n))
        vols = np.abs(vols)
    else:
        vols = np.asarray(volumes, dtype="float64")
        assert len(vols) == n
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


@pytest.fixture
def candle_builder():
    return make_candles
