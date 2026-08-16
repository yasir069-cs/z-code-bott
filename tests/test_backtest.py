"""Phase 13 tests — backtest evaluation on synthetic candle paths."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest
import config


def _sig_row(**over):
    base = dict(timestamp="2026-08-15T19:05:00+05:30", coin="BTC/USDT", signal="BUY",
                entry=100.0, SL=98.0, TP=104.0, RR=2.0, reason="test", ai_used=True)
    base.update(over)
    return base


def _path(start_utc, steps):
    """steps: list of (high, low, close) tuples on consecutive 5m candles."""
    idx = pd.date_range(start_utc, periods=len(steps), freq="5min", tz="UTC")
    rows = []
    o = 100.0
    for h, l, c in steps:
        rows.append({"timestamp": idx[0] if not rows else rows[-1]["timestamp"] + timedelta(minutes=5),
                     "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
        o = c
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.date_range(start_utc, periods=len(steps), freq="5min", tz="UTC")
    return frame.set_index("timestamp")


def _fetcher(frame):
    def fetch(symbol, start, end):
        window = frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]
        return None if window.empty else window
    return fetch


def test_buy_tp_hit_is_win():
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(100.5, 99.8, 100.2), (104.5, 100.1, 104.2), (105, 104, 104.8)])
    out = backtest.evaluate_signal(_sig_row(), _fetcher(frame))
    assert out["outcome"] == "WIN" and out["r"] == 2.0


def test_buy_sl_hit_is_loss():
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(100.5, 99.8, 100.2), (100.2, 97.9, 98.1)])
    out = backtest.evaluate_signal(_sig_row(), _fetcher(frame))
    assert out["outcome"] == "LOSS" and out["r"] == -1.0


def test_both_hit_same_candle_counts_as_loss():
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(104.5, 97.9, 100.0)])
    out = backtest.evaluate_signal(_sig_row(), _fetcher(frame))
    assert out["outcome"] == "LOSS"


def test_no_hit_is_open_with_unrealized_r():
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(101, 99, 100.5), (101.5, 99.5, 101.0)])
    out = backtest.evaluate_signal(_sig_row(), _fetcher(frame))
    assert out["outcome"] == "OPEN" and out["r"] == 0.5  # 101-100 / (100-98)


def test_sell_sl_hit_is_loss():
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(100.2, 99.8, 99.9), (104.1, 100.0, 103.5)])
    out = backtest.evaluate_signal(_sig_row(signal="SELL", entry=100.0, SL=102.0, TP=96.0),
                                   _fetcher(frame))
    assert out["outcome"] == "LOSS"


def test_run_backtest_summary(tmp_path, monkeypatch):
    log_path = tmp_path / "signals_log.csv"
    log_path.write_text(
        "timestamp,coin,signal,entry,SL,TP,RR,reason,ai_used\n"
        "2026-08-15T19:05:00+05:30,BTC/USDT,BUY,100,98,104,2.0,r,True\n"
        "2026-08-15T19:10:00+05:30,ETH/USDT,HOLD,50,,, ,r,True\n"
        "2026-08-15T19:15:00+05:30,ETH/USDT,BUY,50,49,52,2.0,r,False\n",
        encoding="utf-8",
    )
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(100.6, 99.9, 100.2), (104.4, 100.1, 104.1),
                          (50.6, 49.9, 50.2), (49.0, 48.95, 49.2)])
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_path)
    report = backtest.run_backtest(log_path, _fetcher(frame))
    assert report["total_signals"] == 2 and report["holds_logged"] == 1
    assert report["wins"] == 1 and report["losses"] == 1
    assert report["win_rate"] == 50.0
    assert report["total_r"] == 1.0  # +2 -1
