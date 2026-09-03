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


def test_horizon_hours_bounds_the_fill_window():
    """A TP that only prints ~1h45m after the signal is a WIN under the default
    24h horizon but must score OPEN under a 1h horizon — proving --horizon-hours
    actually bounds how far a fill is allowed to happen (E4)."""
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)  # ts_utc + 5m
    flat = (101.0, 99.5, 100.2)          # never touches SL(98) or TP(104)
    spike = (104.5, 100.0, 104.2)        # hits TP(104)
    frame = _path(start, [flat] * 20 + [spike] + [flat] * 4)  # spike at +100m

    long_h = backtest.evaluate_signal(_sig_row(), _fetcher(frame), horizon_hours=24)
    assert long_h["outcome"] == "WIN" and long_h["r"] == 2.0

    short_h = backtest.evaluate_signal(_sig_row(), _fetcher(frame), horizon_hours=1)
    assert short_h["outcome"] == "OPEN"  # spike is past the 1h window


def test_run_backtest_reports_horizon(tmp_path, monkeypatch):
    """run_backtest threads horizon_hours through into the report so write_report
    can print it — a silent cap on fills would otherwise look like real OPENs."""
    log_path = tmp_path / "signals_log.csv"
    log_path.write_text(
        "timestamp,coin,signal,entry,SL,TP,RR,reason,ai_used\n"
        "2026-08-15T19:05:00+05:30,BTC/USDT,BUY,100,98,104,2.0,r,True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_path)
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(101, 99.5, 100.2)])
    report = backtest.run_backtest(log_path, _fetcher(frame), horizon_hours=6)
    assert report["horizon_hours"] == 6


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


# ── Strategy-replay mode ─────────────────────────────────────────────────────
# These prove the replay HARNESS (slicing, scoring, cooldown, safe-degrade),
# using stub deciders so they don't depend on decision.decide's thresholds
# (those live in test_decision.py). The last test wires the REAL core through.
from conftest import make_candles


def _upframe(n, freq, start="2026-08-14 00:00"):
    """A gently rising OHLCV frame of n candles at the given pandas freq."""
    return make_candles([100 + i * 0.4 for i in range(n)], start=start, freq=freq, wick=0.05)


def _long(entry=100.0, quality=70.0):
    """A LONG that clears the live alert floor (ALERT_QUALITY_MIN) — replay
    scores an alertable setup, so the fixture has to look like one."""
    return {"decision": "LONG", "entry": entry, "sl": entry - 2, "tp": entry + 4,
            "rr": 2.0, "setup_quality": quality}


def _ohlcv(df):
    """DataFrame -> ccxt fetch_ohlcv list ([ms, o, h, l, c, v])."""
    return [[int(ts.timestamp() * 1000), r["open"], r["high"], r["low"], r["close"], r["volume"]]
            for ts, r in df.iterrows()]


class _FakeExchange:
    """Returns canned OHLCV per timeframe; records calls; no network."""
    def __init__(self, per_tf):
        self._per_tf = per_tf
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return list(self._per_tf.get(timeframe, []))


def test_slice_frames_excludes_unclosed_htf_candle():
    """A 1H candle is usable only once its hour has fully closed — the 10:00
    candle is IN at c5=11:00 but OUT at c5=10:55 (no look-ahead)."""
    frames = {
        "1h": make_candles([10, 11, 12, 13, 14], start="2026-08-14 08:00", freq="1h"),
        "5m": make_candles([1] * 40, start="2026-08-14 08:00", freq="5min"),
    }
    closed = backtest._slice_frames(frames, pd.Timestamp("2026-08-14 11:00", tz="UTC"))
    assert closed["1h"].index[-1] == pd.Timestamp("2026-08-14 10:00", tz="UTC")
    forming = backtest._slice_frames(frames, pd.Timestamp("2026-08-14 10:55", tz="UTC"))
    assert forming["1h"].index[-1] == pd.Timestamp("2026-08-14 09:00", tz="UTC")


def test_replay_never_sees_a_future_candle():
    """Core safety property: at every decision bar the decider only sees candles
    whose close-time is <= that bar's close-time, across all timeframes."""
    frames = {"1h": _upframe(40, "1h"), "15m": _upframe(80, "15min"), "5m": _upframe(120, "5min")}
    seen = []

    def spy(fr):
        c5 = fr["5m"].index[-1] + backtest._TF_DURATION["5m"]
        for tf, df in fr.items():
            if len(df):
                assert (df.index + backtest._TF_DURATION[tf]).max() <= c5
        seen.append(c5)
        return {"decision": "NO_TRADE"}

    out = backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=spy)
    assert out == []            # NO_TRADE is silent
    assert len(seen) == 120     # every 5m bar evaluated (no cooldown skips on NO_TRADE)


def test_replay_scores_long_through_fill_sim():
    """When the core says LONG, replay builds a sig and scores it with the same
    fill simulator — a TP-tagging future path returns WIN at the core RR."""
    closes = [100.0] * 5 + [100.0, 104.2] + [104.0] * 3   # spike tags TP(104) one bar after entry
    entry_df = make_candles(closes, start="2026-08-14 00:00", freq="5min", wick=0.05)
    frames = {"1h": _upframe(10, "1h"), "15m": _upframe(20, "15min"), "5m": entry_df}
    target = entry_df.index[5]

    def stub(fr):
        return _long() if fr["5m"].index[-1] == target else {"decision": "NO_TRADE"}

    out = backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=stub)
    assert len(out) == 1
    assert out[0]["signal"] == "BUY" and out[0]["outcome"] == "WIN" and out[0]["r"] == 2.0


def test_replay_cooldown_spaces_emissions():
    """An always-LONG core must not fire every 5m bar — the duplicate-guard
    cooldown (DUPLICATE_COOLDOWN_MIN) spaces emissions out."""
    frames = {"1h": _upframe(10, "1h"), "15m": _upframe(20, "15min"), "5m": _upframe(30, "5min")}
    out = backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=lambda fr: _long())
    cd = timedelta(minutes=config.DUPLICATE_COOLDOWN_MIN)
    stamps = [datetime.fromisoformat(r["timestamp"]) for r in out]
    assert 0 < len(out) < 30
    assert all(b - a >= cd for a, b in zip(stamps, stamps[1:]))


def test_replay_all_no_trade_returns_empty():
    frames = {"1h": _upframe(10, "1h"), "15m": _upframe(20, "15min"), "5m": _upframe(30, "5min")}
    out = backtest.replay_strategy("X", frames, decide_fn=lambda fr: {"decision": "NO_TRADE"})
    assert out == []


def test_run_strategy_backtest_fetches_all_tfs(monkeypatch):
    per_tf = {"1h": _ohlcv(_upframe(40, "1h")), "15m": _ohlcv(_upframe(80, "15min")),
              "5m": _ohlcv(_upframe(120, "5min"))}
    ex = _FakeExchange(per_tf)
    monkeypatch.setattr(backtest.decision_core, "decide", lambda fr: _long())
    report = backtest.run_strategy_backtest("BTC/USDT:USDT", ex, horizon_hours=24)
    assert {tf for _, tf, _ in ex.calls} == {config.TF_HTF, config.TF_SETUP, config.TF_ENTRY}
    assert report["total_signals"] >= 1
    assert set(report).issuperset({"win_rate", "avg_rr", "total_r", "results"})


def test_run_strategy_backtest_safe_degrade_on_missing_tf():
    ex = _FakeExchange({"1h": _ohlcv(_upframe(40, "1h")), "15m": _ohlcv(_upframe(80, "15min"))})
    report = backtest.run_strategy_backtest("X/USDT:USDT", ex)   # no 5m data
    assert report["total_signals"] == 0 and report["results"] == []


def test_replay_real_core_is_wired_and_look_ahead_safe():
    """Smoke test: the DEFAULT decider is the live decision.decide. Replaying it
    over trending history runs without error and yields only valid outcomes."""
    frames = {config.TF_HTF: _upframe(60, "1h"), config.TF_SETUP: _upframe(120, "15min"),
              config.TF_ENTRY: _upframe(180, "5min")}
    out = backtest.replay_strategy("BTC/USDT:USDT", frames)   # real decision.decide
    assert isinstance(out, list)
    for r in out:
        assert r["signal"] in ("BUY", "SELL")
        assert r["outcome"] in ("WIN", "LOSS", "OPEN", "NO_DATA")


# ── what the replay is allowed to count as a signal ──────────────────────────

def test_replay_applies_the_live_alert_floor():
    """A LONG below ALERT_QUALITY_MIN is log-only in production, so scoring it
    here would report alerts the owner never received."""
    closes = [100.0] * 8 + [104.2] + [104.0] * 3
    entry_df = make_candles(closes, start="2026-08-14 00:00", freq="5min", wick=0.05)
    frames = {"1h": _upframe(10, "1h"), "15m": _upframe(20, "15min"), "5m": entry_df}
    target = entry_df.index[5]

    def stub(fr):
        if fr["5m"].index[-1] != target:
            return {"decision": "NO_TRADE"}
        return _long(quality=config.ALERT_QUALITY_MIN - 1)

    stats: dict = {}
    out = backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=stub, stats=stats)
    assert out == []
    assert stats["logged_only"] == 1 and stats["alerted"] == 0

    # the same setup at the floor is scored, and the floor is overridable
    def at_floor(fr):
        if fr["5m"].index[-1] != target:
            return {"decision": "NO_TRADE"}
        return _long(quality=config.ALERT_QUALITY_MIN)

    assert len(backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=at_floor)) == 1
    assert len(backtest.replay_strategy("BTC/USDT:USDT", frames, decide_fn=stub,
                                       min_quality=0)) == 1


def test_missing_rr_is_derived_from_the_levels_not_defaulted():
    """The old `RR or 2.0` credited every row without an RR as a 2R win. The R a
    TP hit actually delivers is |TP-entry| / |entry-SL| — geometry, no guess."""
    start = datetime(2026, 8, 15, 13, 40, tzinfo=timezone.utc)
    frame = _path(start, [(103.5, 99.8, 100.2), (104.5, 100.1, 104.2)])
    row = _sig_row(TP=103.0, RR=None)          # 3 reward / 2 risk -> 1.5R
    out = backtest.evaluate_signal(row, _fetcher(frame))
    assert out["outcome"] == "WIN" and out["RR"] == 1.5 and out["r"] == 1.5

    # a blank string from the CSV behaves like a missing value
    assert backtest.evaluate_signal(_sig_row(TP=103.0, RR=""), _fetcher(frame))["RR"] == 1.5
    # and a real logged RR is honoured verbatim
    assert backtest.evaluate_signal(_sig_row(TP=103.0, RR=3.0),
                                    _fetcher(frame))["RR"] == 3.0


def test_strategy_report_declares_what_it_cannot_reproduce(tmp_path):
    """--strategy reads like "the live bot on history", so the report has to name
    the live inputs it cannot reconstruct."""
    per_tf = {"1h": _ohlcv(_upframe(40, "1h")), "15m": _ohlcv(_upframe(80, "15min")),
              "5m": _ohlcv(_upframe(120, "5min"))}
    ex = _FakeExchange(per_tf)
    report = backtest.run_strategy_backtest("BTC/USDT:USDT", ex, horizon_hours=24)
    assert report["mode"] == "strategy-replay"
    assert {"logged_only", "bars_evaluated", "limitations"} <= set(report)
    assert report["bars_evaluated"] == 120
    joined = " ".join(report["limitations"])
    assert "funding" in joined and "open-interest" in joined and "alert floor" in joined

    out = tmp_path / "report.txt"
    backtest.write_report(report, out)
    written = out.read_text()
    assert "Replay limits" in written and "log-only, below the alert floor" in written
