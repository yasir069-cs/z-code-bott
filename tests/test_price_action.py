"""Deterministic tests for the price-action & volume detector."""
import numpy as np
import pandas as pd

import config
import price_action as pa


def frame(rows, start="2026-08-14 10:00", freq="1h"):
    arr = np.array(rows, dtype="float64")
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
         "close": arr[:, 3], "volume": arr[:, 4]},
        index=idx,
    )


# 22 flat candles: range ~2, small body, avg volume 1000, range high 103 / low 101
BASE = [(102.0, 103.0, 101.0, 102.5, 1000.0)] * 22


def test_bullish_rejection_wick():
    last = (102.5, 102.9, 99.5, 102.8, 1000.0)   # long lower wick, tiny body
    out = pa.analyze(frame(BASE + [last]))
    assert out["rejection"] is not None
    assert out["rejection"]["dir"] == "bullish"


def test_bearish_engulfing():
    prev = (100.0, 102.1, 99.9, 102.0, 1000.0)   # bullish
    last = (102.7, 102.8, 99.4, 99.5, 1000.0)    # bearish body swallows it
    out = pa.analyze(frame(BASE + [prev, last]))
    assert out["engulfing"] is not None
    assert out["engulfing"]["dir"] == "bearish"


def test_bullish_displacement():
    last = (102.5, 108.1, 102.4, 108.0, 1500.0)  # body ~5.5 >> 1.5*ATR
    out = pa.analyze(frame(BASE + [last]))
    assert out["displacement"] is not None
    assert out["displacement"]["dir"] == "bullish"


def test_breakout_with_volume_is_strong():
    last = (102.5, 106.2, 102.4, 106.0, 2000.0)  # closes above range high on 2x vol
    out = pa.analyze(frame(BASE + [last]))
    assert out["breakout"] is not None
    assert out["breakout"]["dir"] == "bullish"
    assert out["breakout"]["volume_ok"] is True


def test_breakout_without_volume_is_weak():
    last = (102.5, 106.2, 102.4, 106.0, 900.0)   # same break, no volume
    out = pa.analyze(frame(BASE + [last]))
    assert out["breakout"] is not None
    assert out["breakout"]["volume_ok"] is False  # "no-volume breakout = weak"


def test_failed_breakout_is_a_trap():
    last = (102.5, 104.0, 102.4, 102.6, 1000.0)  # pokes above 103, closes back under
    out = pa.analyze(frame(BASE + [last]))
    assert out["failed_breakout"] is not None
    assert out["failed_breakout"]["dir"] == "bearish"


def test_weak_volume_flag():
    last = (102.0, 103.0, 101.0, 102.5, 300.0)
    out = pa.analyze(frame(BASE + [last]))
    assert out["volume_state"] == "weak"


def test_thin_data_degrades_safely():
    out = pa.analyze(frame([(100, 101, 99, 100.5, 1000)]))
    assert out["rejection"] is None and out["breakout"] is None
    assert out["signals"] == {"bullish": 0, "bearish": 0}
