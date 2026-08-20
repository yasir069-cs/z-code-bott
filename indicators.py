"""Phase 2 — Indicators.

ALL indicator math is delegated to pandas-ta (rules.md: never manual).
  RSI(14), EMA21, daily VWAP, Bollinger Bands (20, 2), ATR(14).
Stores the last 10 RSI values for trend analysis plus the candle/volume
context needed by the filters and the Claude prompt.
"""
import logging
from typing import Optional

import pandas as pd
import pandas_ta as ta

import config

log = logging.getLogger("indicators")


def _col_starting_with(frame: pd.DataFrame, prefix: str) -> str:
    matches = [c for c in frame.columns if c.startswith(prefix)]
    if not matches:
        raise ValueError(f"pandas-ta returned no column starting with {prefix!r}: {list(frame.columns)}")
    return matches[0]


def rsi_trend_up(history: list[float]) -> bool:
    """RSI trend UP = currently rising AND forming a higher low.

    Example from the spec: 50 -> 55 -> 51 -> 56 is bullish because the
    dip to 51 holds above the prior low of 50 and RSI turns up again.
    Requires at least 6 data points for a meaningful comparison.
    """
    h = history[-10:]
    if len(h) < 6:
        return False
    rising = h[-1] > h[-2]
    # Compare the min of the recent 3 values against the min of the prior 3
    higher_low = min(h[-3:]) > min(h[-6:-3])
    return rising and higher_low


def rsi_trend_down(history: list[float]) -> bool:
    """RSI trend DOWN = currently falling AND forming a lower high.
    Requires at least 6 data points for a meaningful comparison.
    """
    h = history[-10:]
    if len(h) < 6:
        return False
    falling = h[-1] < h[-2]
    # Compare the max of the recent 3 values against the max of the prior 3
    lower_high = max(h[-3:]) < max(h[-6:-3])
    return falling and lower_high


def compute_indicators(df: pd.DataFrame) -> Optional[dict]:
    """Compute all indicators on a closed-candle OHLCV frame.

    Returns a snapshot dict of the last candle, or None when there is
    not enough data / an indicator is not yet defined (e.g. fresh listing).
    """
    if df is None or len(df) < config.BB_LENGTH + 5:
        return None

    close, high, low = df["close"], df["high"], df["low"]
    volume = df["volume"]

    rsi = ta.rsi(close, length=config.RSI_LENGTH)
    ema = ta.ema(close, length=config.EMA_LENGTH)
    bb = ta.bbands(close, length=config.BB_LENGTH, std=config.BB_STD, ddof=0)
    # ddof=0 (population std) matches TradingView's ta.stdev
    atr = ta.atr(high, low, close, length=config.ATR_LENGTH)
    try:
        vwap = ta.vwap(high, low, close, volume, anchor=config.VWAP_ANCHOR)
    except (TypeError, ValueError) as exc:  # non-datetime index etc.
        log.warning("VWAP failed: %s", exc)
        return None
    # ta.vwap() may return a DataFrame in some pandas-ta versions; extract the Series
    if isinstance(vwap, pd.DataFrame):
        vwap = vwap.iloc[:, 0]

    bbl_col = _col_starting_with(bb, "BBL_")
    bbm_col = _col_starting_with(bb, "BBM_")
    bbu_col = _col_starting_with(bb, "BBU_")

    last = -1
    values = {
        "rsi": rsi.iloc[last], "rsi_prev": rsi.iloc[last - 1],
        "ema21": ema.iloc[last],
        "bb_lower": bb[bbl_col].iloc[last], "bb_mid": bb[bbm_col].iloc[last],
        "bb_upper": bb[bbu_col].iloc[last],
        "vwap": vwap.iloc[last],
        "atr": atr.iloc[last],
    }
    if any(pd.isna(v) for v in values.values()):
        log.debug("indicator warm-up incomplete, skipping frame")
        return None

    rsi_history = [float(x) for x in rsi.dropna().tail(config.RSI_HISTORY).tolist()]
    if len(rsi_history) < 4:
        return None

    range_high = float(high.tail(config.CANDLE_LIMIT).max())
    range_low = float(low.tail(config.CANDLE_LIMIT).min())
    price = float(close.iloc[last])

    return {
        "timestamp": df.index[last],
        "open": float(df["open"].iloc[last]),
        "high": float(high.iloc[last]),
        "low": float(low.iloc[last]),
        "close": price,
        "volume": float(volume.iloc[last]),
        "volume_prev": float(volume.iloc[last - 1]),
        "volume_avg20": float(volume.tail(config.SWEEP_WINDOW).mean()),
        "volume_trend": [float(v) for v in volume.tail(5).tolist()],
        "rsi": float(values["rsi"]),
        "rsi_prev": float(values["rsi_prev"]),
        "rsi_history": rsi_history,
        "ema21": float(values["ema21"]),
        "vwap": float(values["vwap"]),
        "bb_lower": float(values["bb_lower"]),
        "bb_mid": float(values["bb_mid"]),
        "bb_upper": float(values["bb_upper"]),
        "atr": float(values["atr"]),
        "range_high": range_high,
        "range_low": range_low,
        "range_pos": (price - range_low) / (range_high - range_low) if range_high > range_low else 0.5,
        "swing_low_20": float(low.tail(config.SWEEP_WINDOW).min()),
        "swing_high_20": float(high.tail(config.SWEEP_WINDOW).max()),
    }
