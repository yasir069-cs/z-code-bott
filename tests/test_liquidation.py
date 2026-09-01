"""Liquidation websocket cache and integration tests."""
import json
import csv
import time

import alerts
import ai_decision
import config
import liquidation
import logger


def _event(ts, side="SELL", price=99.0, notional=1000.0):
    return {"symbol": "BTCUSDT", "side": side, "price": price,
            "quantity": 1, "notional": notional, "timestamp": ts}


def test_cache_aggregates_windows_and_detects_burst(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(liquidation.time, "time", lambda: now)
    cache = liquidation.LiquidationCache()
    cache.set_connection(True)
    cache.add_event(_event(now - 60, "SELL", notional=100))
    cache.add_event(_event(now - 120, "SELL", notional=200))
    cache.add_event(_event(now - 180, "BUY", notional=300))
    cache.add_event(_event(now - 700, "BUY", notional=900))

    result = cache.summary("BTCUSDT", current_price=100)
    assert result["available"] is True
    assert result["windows"]["5m"] == {
        "long_notional": 300.0, "long_count": 2,
        "short_notional": 300.0, "short_count": 1, "burst": True,
    }
    assert result["windows"]["15m"]["short_count"] == 2
    assert result["windows"]["1h"]["short_count"] == 2
    assert result["freshness_seconds"] == 60.0
    assert result["event_price_context"][0]["price_delta_pct"] == -1.0


def test_missing_or_disconnected_data_degrades_without_values():
    cache = liquidation.LiquidationCache()
    result = cache.summary("BTCUSDT")
    assert result["available"] is False
    assert result["warning"]
    cache.set_connection(True)
    result = cache.summary("BTCUSDT")
    assert result["available"] is False
    assert result["windows"]["1h"]["long_count"] == 0


def test_stale_stream_degrades_available_but_keeps_history(monkeypatch):
    """Connected socket + no message for > LIQ_STALE_SECONDS: the summary is
    unavailable-with-warning (never presented as a fresh reading) while the
    cached events keep their per-window counts — historical facts stay."""
    now = 1_000_000.0
    monkeypatch.setattr(liquidation.time, "time", lambda: now)
    cache = liquidation.LiquidationCache()
    cache.set_connection(True)
    cache.add_event(_event(now - 60, "SELL", notional=100))
    assert cache.summary("BTCUSDT")["available"] is True   # FRESH while fed

    monkeypatch.setattr(liquidation.time, "time",
                        lambda: now + config.LIQ_STALE_SECONDS + 60)
    result = cache.summary("BTCUSDT")
    assert result["available"] is False
    assert result["stream_status"] == "STALE"
    assert "stale" in result["warning"]
    # the event is still inside the 1h window -> counts preserved, not zeroed
    assert result["windows"]["1h"]["long_count"] == 1
    assert result["windows"]["1h"]["long_notional"] == 100.0


def test_ccxt_symbol_finds_binance_stream_events(monkeypatch):
    """The scan pipeline passes ccxt symbols ('BTC/USDT:USDT') while the
    websocket caches Binance's raw form ('BTCUSDT') — the lookup must
    normalize both to the same key or every summary reads 'no events'."""
    now = 1_000_000.0
    monkeypatch.setattr(liquidation.time, "time", lambda: now)
    cache = liquidation.LiquidationCache()
    cache.set_connection(True)
    cache.add_event(_event(now - 30))
    result = cache.summary("BTC/USDT:USDT")
    assert result["available"] is True
    assert result["windows"]["5m"]["long_count"] == 1


def test_lookback_windows_are_config_driven(monkeypatch):
    """LIQUIDATION_WINDOWS in config.py controls which windows the summary
    aggregates — and an invalid override falls back to the defaults instead
    of producing empty or broken summaries."""
    now = 1_000_000.0
    monkeypatch.setattr(liquidation.time, "time", lambda: now)
    cache = liquidation.LiquidationCache()
    cache.set_connection(True)
    cache.add_event(_event(now - 60))
    cache.add_event(_event(now - 400))

    monkeypatch.setattr(config, "LIQUIDATION_WINDOWS",
                        {"2m": 120, "10m": 600})
    result = cache.summary("BTCUSDT")
    assert set(result["windows"]) == {"2m", "10m"}
    assert result["windows"]["2m"]["long_count"] == 1   # only the 60s event
    assert result["windows"]["10m"]["long_count"] == 2  # both events

    # invalid entries dropped; empty/wholly-invalid mapping -> defaults
    monkeypatch.setattr(config, "LIQUIDATION_WINDOWS",
                        {"5m": "bogus", "15m": -1, "1h": 3600})
    assert set(liquidation._configured_windows()) == {"1h"}
    monkeypatch.setattr(config, "LIQUIDATION_WINDOWS", {})
    assert liquidation._configured_windows() == liquidation._DEFAULT_WINDOWS


def test_selected_payload_and_prompt_include_liquidation_context():
    bundle = {
        "symbol": "BTC/USDT", "direction": "BUY", "current_price": 100,
        "entry_price": 99, "ind_1h": {"range_pos": .2, "rsi": 55,
        "rsi_prev": 50, "ema21": 99, "vwap": 99, "bb_lower": 98,
        "bb_mid": 100, "bb_upper": 102, "close": 99, "atr": 1,
        "swing_low_20": 98, "swing_high_20": 103, "volume_trend": []},
        "ind_15m": None,
        "ind_5m": {"rsi": 55, "rsi_prev": 50, "rsi_history": [50],
                    "ema21": 99, "vwap": 99, "bb_lower": 98,
                    "bb_mid": 100, "bb_upper": 102, "close": 99,
                    "atr": 1, "volume_trend": []},
        "liquidation": {"available": True, "freshness_seconds": 3,
                         "latest_event_timestamp": 999,
                         "windows": {"5m": {"long_count": 1, "short_count": 2,
                                            "long_notional": 150.0,
                                            "short_notional": 40.0,
                                            "burst": True}}},
    }
    prompt = ai_decision.build_prompt(bundle)
    assert "Websocket liquidation context" in prompt
    assert "long 1 ($150)" in prompt            # notional is shown, not just counts
    assert "short 2 ($40)" in prompt
    assert "BURST" in prompt                     # spike flag reaches the model
    assert "Do NOT make trade decisions based on liquidation spike alone" in prompt


def test_unavailable_liquidation_renders_warning_in_prompt():
    bundle = {
        "symbol": "BTC/USDT", "direction": "BUY", "current_price": 100,
        "entry_price": 99, "ind_1h": {"range_pos": .2, "rsi": 55,
        "rsi_prev": 50, "ema21": 99, "vwap": 99, "bb_lower": 98,
        "bb_mid": 100, "bb_upper": 102, "close": 99, "atr": 1,
        "swing_low_20": 98, "swing_high_20": 103, "volume_trend": []},
        "ind_15m": None,
        "ind_5m": {"rsi": 55, "rsi_prev": 50, "rsi_history": [50],
                    "ema21": 99, "vwap": 99, "bb_lower": 98,
                    "bb_mid": 100, "bb_upper": 102, "close": 99,
                    "atr": 1, "volume_trend": []},
        "liquidation": {"available": False,
                        "warning": "liquidation stream disconnected"},
    }
    prompt = ai_decision.build_prompt(bundle)
    assert "Liquidation stream: unavailable (liquidation stream disconnected)" in prompt
    assert "do not infer or fabricate" in prompt


def test_scan_signal_reason_includes_liquidation_context(monkeypatch):
    """run_scan's _build_sig appends the 1h liquidation counts to the alert
    reason text whenever the websocket summary is available — and never
    fabricates an 'in 1h' line when the 1h window is not configured."""
    summary = {"available": True, "warning": "",
               "latest_event_timestamp": 123,
               "windows": {"1h": {"long_count": 4, "long_notional": 120.0,
                                  "short_count": 1, "short_notional": 30.0,
                                  "burst": True}}}
    monkeypatch.setattr(liquidation, "get_summary",
                        lambda *a, **k: dict(summary))
    from main import _build_sig
    d = {"decision": "LONG", "setup_quality": 70.0, "entry": 100.0,
         "sl": 98.0, "tp": 104.0, "rr": 2.0, "htf_bias": "bullish",
         "no_trade_reasons": [], "data_warnings": [],
         "structure": {"atr": 1.0, "trend": "up", "bias": "bullish"},
         "sr": {}, "liquidity": {}}
    sig = _build_sig("BTC/USDT:USDT", d, "BUY", None, False, 0.0001, 100.0)
    assert "Liquidations: 4 long / 1 short events in 1h." in sig["reason"]
    assert sig["liquidation"]["available"] is True

    # no 1h window configured (custom windows) -> no fabricated 1h line
    monkeypatch.setattr(liquidation, "get_summary",
                        lambda *a, **k: {"available": True, "warning": "",
                                         "windows": {"10m": {"long_count": 2,
                                                             "short_count": 0}}})
    sig = _build_sig("BTC/USDT:USDT", d, "BUY", None, False, 0.0001, 100.0)
    assert "Liquidations:" not in sig["reason"]


def test_available_liquidation_is_rendered_in_alert_and_csv(tmp_path, monkeypatch):
    summary = {"available": True, "latest_event_timestamp": 123,
               "windows": {"1h": {"long_count": 2, "short_count": 1,
                                    "long_notional": 10, "short_notional": 20}}}
    sig = {"coin": "BTC/USDT", "signal": "BUY", "entry": 100, "SL": 98,
           "TP": 104, "RR": 2, "confidence": 70, "reason": "liq context",
           "ai_used": True, "liquidation": summary}
    assert "Liquidation Context" in alerts.format_alert(sig)
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", path)
    logger.log_signal(sig)
    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert json.loads(row["liquidation"]) == summary
