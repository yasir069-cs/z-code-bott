"""Phase 9 + 10 + 11 tests — duplicate guard, Telegram formatting, CSV logger."""
import csv
from datetime import datetime, timedelta
from pathlib import Path

import duplicate_guard
import alerts
import logger
import config


def _now():
    return datetime(2026, 8, 16, 19, 0, 0)


# ------------------------------------------------------------ duplicate guard
def test_same_coin_within_20min_is_duplicate():
    g = duplicate_guard.DuplicateGuard()
    now = _now()
    g.record("BTC/USDT", now)
    assert g.is_duplicate("BTC/USDT", now + timedelta(minutes=19, seconds=59)) is True
    assert g.is_duplicate("BTC/USDT", now + timedelta(minutes=20, seconds=1)) is False


def test_different_coins_are_independent():
    g = duplicate_guard.DuplicateGuard()
    now = _now()
    g.record("BTC/USDT", now)
    assert g.is_duplicate("ETH/USDT", now + timedelta(minutes=5)) is False


def test_reset_clears_everything():
    g = duplicate_guard.DuplicateGuard()
    now = _now()
    g.record("BTC/USDT", now)
    g.reset()
    assert g.is_duplicate("BTC/USDT", now + timedelta(minutes=1)) is False
    assert g.tracked_count() == 0


def test_empty_guard_never_duplicates():
    g = duplicate_guard.DuplicateGuard()
    assert g.is_duplicate("BTC/USDT", _now()) is False


# ------------------------------------------------------------------- alerts
def _sig(**over):
    base = dict(coin="BTC/USDT", signal="BUY", entry=63000.0, SL=62700.0,
                TP=63600.0, RR=2.0, reason="sweep + RSI higher low", ai_used=True)
    base.update(over)
    return base


def test_alert_contains_all_fields():
    text = alerts.format_alert(_sig())
    assert "BUY SIGNAL" in text and "BTC/USDT" in text
    for needle in ("Entry", "SL", "TP", "RR", "Reason"):
        assert needle in text


def test_alert_fallback_tag():
    text = alerts.format_alert(_sig(ai_used=False))
    assert "AI Unavailable - Indicator based signal" in text


def test_alert_ai_model_line():
    text = alerts.format_alert(_sig(ai_used=True))
    assert "AI:" in text and "AI Unavailable" not in text


def test_alert_escapes_html_in_reason():
    text = alerts.format_alert(_sig(reason="<b>injection & stuff</b>"))
    assert "<b>injection" not in text


def test_send_skips_hold():
    assert alerts.send_alert(_sig(signal="HOLD")) is False


def test_send_without_config_returns_false(monkeypatch):
    monkeypatch.setattr("config.TELEGRAM_TOKEN", "")
    assert alerts.send_alert(_sig()) is False


# -------------------------------------------------------------------- logger
def test_logger_creates_header_and_appends(tmp_path, monkeypatch):
    log_file = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_file)
    logger.log_signal(_sig())
    logger.log_signal(_sig(coin="ETH/USDT", signal="HOLD", SL=None, TP=None,
                           RR=None, ai_used=False))
    rows = list(csv.DictReader(open(log_file, encoding="utf-8")))
    assert len(rows) == 2
    assert list(rows[0].keys()) == config.CSV_COLUMNS
    assert rows[0]["coin"] == "BTC/USDT" and rows[0]["ai_used"] == "True"
    assert rows[1]["signal"] == "HOLD" and rows[1]["ai_used"] == "False"
    assert rows[1]["SL"] == ""


def test_logger_appends_never_truncates(tmp_path, monkeypatch):
    log_file = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_file)
    for i in range(5):
        logger.log_signal(_sig(entry=63000 + i))
    rows = list(csv.DictReader(open(log_file, encoding="utf-8")))
    assert len(rows) == 5
    assert float(rows[0]["entry"]) == 63000.0 and float(rows[4]["entry"]) == 63004.0
