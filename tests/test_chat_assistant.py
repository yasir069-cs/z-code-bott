"""Tests for the interactive Telegram LLM Chat Assistant."""
import csv
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import chat_assistant
import config
import telegram_bot


def test_get_recent_signals_summary_empty(tmp_path, monkeypatch):
    test_csv = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", test_csv)
    summary = chat_assistant.get_recent_signals_summary()
    assert summary == "No signals logged yet."


def test_get_recent_signals_summary_with_data(tmp_path, monkeypatch):
    test_csv = tmp_path / "signals_log.csv"
    monkeypatch.setattr(config, "SIGNALS_LOG_FILE", test_csv)
    with open(test_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=config.CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({
            "timestamp": "2026-08-20T19:00:00",
            "coin": "BTC/USDT",
            "signal": "BUY",
            "entry": 65000,
            "SL": 64000,
            "TP": 67000,
            "RR": 2.0,
            "reason": "Test reason",
            "ai_used": True,
        })

    summary = chat_assistant.get_recent_signals_summary(5)
    assert "BTC/USDT" in summary
    assert "BUY" in summary
    assert "Entry: 65000" in summary


def test_get_bot_status_summary():
    status = chat_assistant.get_bot_status_summary()
    assert "Session Hours:" in status
    assert "Active Model:" in status
    assert "Volume Filter:" in status


def test_ask_crypto_assistant_without_key(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    reply = chat_assistant.ask_crypto_assistant("How does RSI work?")
    assert "AI Assistant Offline" in reply


def test_ask_crypto_assistant_success(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "RSI is a momentum oscillator measuring speed of price changes."}}]
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        reply = chat_assistant.ask_crypto_assistant("What is RSI?")
        assert "momentum oscillator" in reply
        mock_post.assert_called_once()


import asyncio


def test_cmd_start():
    update = MagicMock()
    update.message = AsyncMock()
    context = MagicMock()

    asyncio.run(telegram_bot.cmd_start(update, context))
    update.message.reply_text.assert_called_once()
    assert "Welcome to Crypto Signal Bot AI" in update.message.reply_text.call_args[0][0]


def test_cmd_status():
    update = MagicMock()
    update.message = AsyncMock()
    context = MagicMock()

    asyncio.run(telegram_bot.cmd_status(update, context))
    update.message.reply_text.assert_called_once()
    assert "Bot Status Overview" in update.message.reply_text.call_args[0][0]


def test_cmd_strategy():
    update = MagicMock()
    update.message = AsyncMock()
    context = MagicMock()

    asyncio.run(telegram_bot.cmd_strategy(update, context))
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Confluence" in text and "Liquidation Sweep" in text
