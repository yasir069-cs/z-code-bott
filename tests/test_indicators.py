"""Phase 2 tests — pandas-ta indicator values vs independent reference math.

The reference implementations below are TEST-ONLY verification code
(production must use pandas-ta per rules.md) and follow the same
definitions TradingView uses: Wilder-smoothed RSI, EMA adjust=False,
BB = SMA20 +/- 2*std, VWAP reset daily, ATR = Wilder-smoothed TR.
"""
import numpy as np
import pandas as pd

import config
from conftest import make_candles
from indicators import compute_indicators, rsi_trend_down, rsi_trend_up


def ref_rsi_wilder(close, length=14):
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:length].mean()
    avg_loss = loss[:length].mean()
    for i in range(length, len(delta)):
        avg_gain = (avg_gain * (length - 1) + gain[i]) / length
        avg_loss = (avg_loss * (length - 1) + loss[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def ref_ema(close, length=21):
    alpha = 2 / (length + 1)
    ema = close[0]
    for px in close[1:]:
        ema = alpha * px + (1 - alpha) * ema
    return ema


def ref_bb(close, length=20, std_mult=2.0):
    tail = close[-length:]
    mid = tail.mean()
    sd = tail.std(ddof=0)  # population std, matches pandas-ta/TradingView
    return mid - std_mult * sd, mid, mid + std_mult * sd


def ref_vwap_daily(df):
    grouped = df.groupby(df.index.date)
    last_day = sorted(grouped.groups)[-1]
    d = grouped.get_group(last_day)
    tp = (d["high"] + d["low"] + d["close"]) / 3
    return float((tp * d["volume"]).cumsum().iloc[-1] / d["volume"].cumsum().iloc[-1])


def ref_atr_wilder(df, length=14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    atr = tr[:length].mean()
    for x in tr[length:]:
        atr = (atr * (length - 1) + x) / length
    return atr


def _frame():
    rng = np.random.default_rng(42)
    n = 320  # >= 50 strategy candles + warm-up so recursions converge
    closes = 100 + np.cumsum(rng.normal(0, 0.8, n))
    return make_candles(closes, seed=3, freq="1h")


def test_rsi_matches_reference():
    snap = compute_indicators(_frame())
    ref = ref_rsi_wilder(_frame()["close"].values)
    assert abs(snap["rsi"] - ref) < 1e-6


def test_ema21_matches_reference():
    snap = compute_indicators(_frame())
    ref = ref_ema(_frame()["close"].values, 21)
    assert abs(snap["ema21"] - ref) < 1e-6


def test_bollinger_matches_reference():
    snap = compute_indicators(_frame())
    lo, mid, hi = ref_bb(_frame()["close"].values)
    assert abs(snap["bb_lower"] - lo) < 1e-6
    assert abs(snap["bb_mid"] - mid) < 1e-6
    assert abs(snap["bb_upper"] - hi) < 1e-6


def test_vwap_daily_reset_matches_reference():
    # 3 days of hourly candles -> VWAP must anchor to the last UTC day only
    rng = np.random.default_rng(11)
    closes = 50 + np.cumsum(rng.normal(0, 0.3, 72))
    df = make_candles(closes, start="2026-08-12 00:30", freq="1h", seed=5)
    snap = compute_indicators(df)
    assert abs(snap["vwap"] - ref_vwap_daily(df)) < 1e-9


def test_atr_matches_reference():
    snap = compute_indicators(_frame())
    ref = ref_atr_wilder(_frame())
    assert abs(snap["atr"] - ref) < 1e-6


def test_rsi_history_stores_last_10():
    snap = compute_indicators(_frame())
    assert len(snap["rsi_history"]) == config.RSI_HISTORY
    assert all(0 <= v <= 100 for v in snap["rsi_history"])


def test_range_position():
    snap = compute_indicators(_frame())
    assert 0.0 <= snap["range_pos"] <= 1.0
    assert snap["range_high"] >= snap["swing_high_20"]
    assert snap["range_low"] <= snap["swing_low_20"]


def test_insufficient_data_returns_none():
    assert compute_indicators(_frame().head(10)) is None


# ---------------------------------------------------------------- RSI trend
def test_rsi_trend_up_recognizes_higher_low():
    # spec example: 50 -> 55 -> 51 -> 56 is bullish (higher low)
    hist = [44, 47, 50, 55, 51, 56]
    assert rsi_trend_up(hist) is True


def test_rsi_trend_up_rejects_lower_low():
    hist = [60, 55, 51, 48]  # falling with lower lows
    assert rsi_trend_up(hist) is False


def test_rsi_trend_down_recognizes_lower_high():
    hist = [56, 51, 55, 50]  # 55 lower than 56, then turns down
    assert rsi_trend_down(hist) is True


def test_rsi_trend_requires_rising_last_step():
    hist = [50, 55, 51, 52, 49]  # last step down kills the "up" trend
    assert rsi_trend_up(hist) is False


def test_rsi_trend_too_short():
    assert rsi_trend_up([50, 55]) is False
    assert rsi_trend_down([50, 45]) is False
