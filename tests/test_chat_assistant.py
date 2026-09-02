"""Tests for the interactive Telegram LLM Chat Assistant."""
import csv
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import requests

import ai_decision
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


_ANSWER = {"choices": [{"message": {
    "content": "RSI is a momentum oscillator measuring speed of price changes."}}]}


def test_ask_crypto_assistant_success(monkeypatch):
    """The assistant rides the SAME transport as the decisions: JSON body,
    browser-like User-Agent (the provider WAF challenges bare python-requests),
    explicit non-streaming, and the configured model."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    ai_decision.reset_budget()

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "application/json"}
    resp.text = json.dumps(_ANSWER)
    resp.json.return_value = _ANSWER
    resp.raise_for_status.return_value = None

    with patch("requests.post", return_value=resp) as mock_post:
        reply = chat_assistant.ask_crypto_assistant("What is RSI?")
        assert "momentum oscillator" in reply
        mock_post.assert_called_once()

    sent = mock_post.call_args
    assert sent.args[0] == ai_decision.chat_completions_url()
    assert sent.kwargs["json"]["model"] == config.AI_MODEL
    assert sent.kwargs["json"]["stream"] is False
    assert sent.kwargs["json"]["max_tokens"] == 600      # a chat answer, not a batch
    assert "Mozilla" in sent.kwargs["headers"]["User-Agent"]
    assert sent.kwargs["headers"]["Authorization"] == "Bearer test-key"
    # chat spend is visible to the daily cap it is consuming
    assert ai_decision.budget_status()["used"] == 1
    ai_decision.reset_budget()


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
    assert "Market structure" in text and "NO_TRADE" in text and "secondary" in text


def test_assistant_retries_and_degrades_honestly(monkeypatch):
    """A 503 used to end the chat on the first attempt (no retry path of its
    own). Now it retries like every other AI call and, when the provider stays
    down, says so — it never invents an answer."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(config, "AI_RETRY_MAX", 3)
    monkeypatch.setattr(config, "AI_RETRY_BACKOFF_BASE", 0.0)
    ai_decision.reset_budget()

    resp = MagicMock(status_code=503, text="upstream down", headers={})
    resp.raise_for_status.side_effect = requests.HTTPError("503 Server Error")

    with patch("requests.post", return_value=resp) as mock_post:
        reply = chat_assistant.ask_crypto_assistant("What is RSI?")
    assert mock_post.call_count == config.AI_RETRY_MAX
    assert "try again" in reply
    assert "RSI is" not in reply                     # no fabricated answer
    assert ai_decision.budget_status()["used"] == config.AI_RETRY_MAX
    ai_decision.reset_budget()
