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
def test_same_coin_within_cooldown_is_duplicate():
    """Bounded by config.DUPLICATE_COOLDOWN_MIN rather than a literal, so
    retuning the cooldown can't silently invalidate this test again."""
    g = duplicate_guard.DuplicateGuard()
    now = _now()
    cooldown = config.DUPLICATE_COOLDOWN_MIN
    g.record("BTC/USDT", now)
    assert g.is_duplicate("BTC/USDT", now + timedelta(minutes=cooldown, seconds=-1)) is True
    assert g.is_duplicate("BTC/USDT", now + timedelta(minutes=cooldown, seconds=1)) is False


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
    for needle in ("Entry", "SL", "TP", "RR", "📝"):
        assert needle in text


def test_alert_fallback_tag():
    """alerts._FALLBACK_TAG uses an em dash; fallback.py's reason text uses a
    hyphen. They are different strings by design — assert the badge here."""
    text = alerts.format_alert(_sig(ai_used=False))
    assert alerts._FALLBACK_TAG in text


def test_alert_ai_model_line():
    text = alerts.format_alert(_sig(ai_used=True))
    assert "AI:" in text and "AI Unavailable" not in text


def test_alert_escapes_html_in_reason():
    text = alerts.format_alert(_sig(reason="<b>injection & stuff</b>"))
    assert "<b>injection" not in text


def test_populated_alert_renders_confidence_confluence_indicators_sweep():
    """E3 blind spot: run_scan forwards a fully-scored context, so assert the
    alert actually renders it — confidence HIGH (not the old always-LOW), the
    confluence line with per-timeframe breakdown, the indicator block and the
    sweep label. This is the exact content that used to be silently dropped."""
    sig = _sig(
        confidence=85.0, confluence=78.0,
        score_1h=82.0, score_15m=74.0, score_5m=76.0,
        indicators=dict(rsi_now=63.4, rsi_prev=58.1, price_above_ema=True,
                        price_above_vwap=True, volume_ratio=1.8),
        sweep=dict(detected=True, type="bullish", age=1),
        rsi_bounce_detected=True,
    )
    text = alerts.format_alert(sig)
    assert "STRONG" in text                      # confidence 85 -> STRONG tier (70+)
    assert "Confluence" in text and "78/100" in text
    for part in ("1H 82", "15M 74", "5M 76"):   # per-timeframe breakdown
        assert part in text
    for label in ("Indicators", "EMA21", "VWAP", "Volume"):
        assert label in text
    assert "bullish detected" in text           # sweep labelled, not "Not detected"
    assert "Bounce" in text                      # RSI-bounce badge fired


def test_no_sweep_alert_caps_confidence_and_labels_it():
    """Without a sweep, run_scan caps confidence at NO_SWEEP_CONFIDENCE_CAP
    (just below the STRONG tier) and the alert must say so — 'sweep required
    for the strongest alert' is a visible label, not a hidden number."""
    sig = _sig(confidence=config.NO_SWEEP_CONFIDENCE_CAP, confluence=66.0,
               sweep=dict(detected=False))
    text = alerts.format_alert(sig)
    assert "HIGH" in text                       # 59.0 -> below the 60 STRONG gate
    assert "Not detected" in text and "capped" in text


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


def test_migrate_archives_stale_header_and_preserves_history(tmp_path, monkeypatch):
    """E1/E3: a 9-column file written before the extra columns existed must be
    archived (not overwritten) and a fresh 20-column file started, so DictWriter
    never again shifts 20 values under a 9-field header. The old rows survive in
    the .v1.bak archive; the new log reads back keyed exactly by CSV_COLUMNS."""
    log_file = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_file)
    stale = ("timestamp,coin,signal,entry,SL,TP,RR,reason,ai_used\n"
             "2026-08-15T19:05:00+05:30,BTC/USDT,BUY,100,98,104,2.0,old row,True\n")
    log_file.write_text(stale, encoding="utf-8")

    logger.migrate_csv_header()

    bak = tmp_path / "signals_log.csv.v1.bak"
    assert bak.exists(), "stale file must be archived, never overwritten in place"
    assert bak.read_text(encoding="utf-8") == stale, "history preserved verbatim"
    assert not log_file.exists(), "the stale file was renamed away"

    logger.log_signal(_sig())  # fresh file gets the correct 20-column header
    rows = list(csv.DictReader(open(log_file, encoding="utf-8")))
    assert list(rows[0].keys()) == config.CSV_COLUMNS
    assert rows[0]["coin"] == "BTC/USDT"


def test_migrate_is_noop_when_header_current(tmp_path, monkeypatch):
    """A file already on the current header must be left untouched — migration
    must be idempotent so a restart never keeps spawning .vN.bak archives."""
    log_file = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_file)
    logger.log_signal(_sig())          # writes the current 20-column header
    before = log_file.read_text(encoding="utf-8")

    logger.migrate_csv_header()

    assert log_file.read_text(encoding="utf-8") == before
    assert not (tmp_path / "signals_log.csv.v1.bak").exists()


def test_migrate_no_file_is_safe(tmp_path, monkeypatch):
    """First-ever run: no log yet -> migration is a silent no-op, not an error."""
    log_file = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", log_file)
    logger.migrate_csv_header()        # must not raise
    assert not log_file.exists()
