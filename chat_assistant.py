"""Interactive LLM Chat Assistant for Telegram User Queries.

Allows users to chat with the bot in Telegram and ask questions about:
  - The bot's trading strategy (1H -> 15M -> 5M top-down filters)
  - Indicators used (RSI, EMA21, daily VWAP, Bollinger Bands, ATR, sweeps)
  - Recent signal performance and logs from signals_log.csv
  - General cryptocurrency trading, risk management, and market insights

Uses OpenRouter (NVIDIA Nemotron or configured model) with rich context.
"""
import csv
from datetime import datetime
import logging
from typing import Optional

import requests

import config

log = logging.getLogger("chat_assistant")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def get_recent_signals_summary(limit: int = 5) -> str:
    """Read recent signals from signals_log.csv."""
    if not config.SIGNALS_LOG_FILE.exists():
        return "No signals logged yet."

    try:
        with open(config.SIGNALS_LOG_FILE, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if not rows:
                return "No signals recorded yet."
            recent = rows[-limit:]
            lines = []
            for r in recent:
                ts = r.get("timestamp", "N/A")
                coin = r.get("coin", "N/A")
                sig = r.get("signal", "N/A")
                entry = r.get("entry", "N/A")
                sl = r.get("SL", "N/A")
                tp = r.get("TP", "N/A")
                rr = r.get("RR", "N/A")
                lines.append(f"- {ts} | {coin} | {sig} | Entry: {entry} | SL: {sl} | TP: {tp} | RR: {rr}")
            return "\n".join(lines)
    except Exception as exc:
        log.warning("Could not read signals log: %s", exc)
        return "Could not load signals log."


def get_bot_status_summary() -> str:
    """Return current bot operational status."""
    now_ist = datetime.now(config.TZ)
    hhmm = now_ist.strftime("%H:%M")
    in_session = config.SESSION_START <= hhmm < config.SESSION_END
    status = "🟢 ACTIVE (Scanning Market)" if in_session else "🌙 SLEEPING (Outside Trading Window)"

    # AI budget + OHLCV cache health (best-effort — /status must never break).
    extra = ""
    try:
        from ai_decision import budget_status
        b = budget_status()
        extra += f"\nAI Budget: {b['used']}/{b['limit']} used today ({b['remaining']} left)"
    except Exception:
        pass
    try:
        from scanner import cache_stats
        c = cache_stats()
        extra += f"\nOHLCV Cache: {c['entries']} frames, {c['hit_rate']:.0%} hit rate"
    except Exception:
        pass

    return (
        f"Current Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST\n"
        f"Session Hours: {config.SESSION_START} to {config.SESSION_END} IST\n"
        f"Status: {status}\n"
        f"Active Model: {config.AI_MODEL}\n"
        f"Volume Filter: >= ${config.VOLUME_MIN_USDT:,} USDT"
        f"{extra}"
    )


def _build_system_prompt() -> str:
    recent_signals = get_recent_signals_summary(5)
    bot_status = get_bot_status_summary()

    return f"""You are the official Senior Crypto AI Assistant for this automated Crypto Signal Bot.
Your job is to assist users in Telegram with queries about the bot's strategy, signals, indicators, crypto markets, and trading principles.

=== BOT ARCHITECTURE & STRATEGY CONTEXT ===
1. Strategy Flow:
   - 1H Context: Evaluates macro trend using 3 core pillars:
     * RSI: 40-75 and rising for BUY / 28-55 and falling for SELL
     * EMA21: Close > EMA21 for BUY / Close < EMA21 for SELL
     * Daily VWAP: Close > VWAP for BUY / Close < VWAP for SELL
     * Volume & Bollinger Bands confirmation
     * Liquidation Sweep: Optional smart-money pattern (wicks sweeping swing levels with high volume).
   - 15M Confirmation: Requires at least 4 out of 5 checks (RSI, EMA21, VWAP, Volume, BB).
   - 5M Entry Timing: Requires at least 5 out of 7 checks (RSI trend, higher-lows/lower-highs, EMA21, VWAP, volume > 20-avg, BB bounce/rejection).
   - AI Decision Engine: OpenRouter ({config.AI_MODEL}) analyzes complete top-down context, sets entry, SL, TP, and enforces minimum 1:2 Risk-to-Reward.
   - Safety: Bot generates SIGNALS ONLY (never executes automated trades or holds private keys).

2. Operational Details:
{bot_status}

3. Recent Signals:
{recent_signals}

=== GUIDELINES FOR RESPONDING ===
- Be friendly, professional, clear, and concise.
- Format responses nicely for Telegram using simple Markdown (bolding with *, bullet points with -).
- Always include a brief risk disclaimer when discussing specific crypto trades.
- If asked about the bot's status or strategy, explain clearly based on the context above.
- Answer in the user's language (Hindi, Urdu, English, Hinglish, etc.).
"""


def ask_crypto_assistant(user_query: str, chat_history: Optional[list] = None) -> str:
    """Send user query to OpenRouter and return the assistant's reply."""
    if not config.OPENROUTER_API_KEY:
        return (
            "⚠️ <b>AI Assistant Offline:</b> OPENROUTER_API_KEY is not configured in `.env`.\n\n"
            "You can still use commands like `/status`, `/signals`, and `/strategy`."
        )

    messages = [{"role": "system", "content": _build_system_prompt()}]

    if chat_history:
        for msg in chat_history[-6:]:  # include last few turns
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                messages.append(msg)

    messages.append({"role": "user", "content": user_query})

    payload = {
        "model": config.AI_MODEL,
        "messages": messages,
        "max_tokens": 600,
        "temperature": 0.3,
    }

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
        if response.status_code != 200:
            log.error("OpenRouter chat error: %s", response.text[:500])
            return "⚠️ Sorry, the AI service encountered an error. Please try again in a moment."

        data = response.json()
        reply = (data["choices"][0]["message"].get("content") or "").strip()
        return reply or "I received your message, but the AI generated an empty response. Please ask again."

    except requests.exceptions.Timeout:
        log.warning("OpenRouter chat request timed out")
        return "⏳ Request timed out. The AI model is taking longer than expected. Please try again."
    except Exception as exc:
        log.error("Error calling OpenRouter chat: %s", exc)
        return f"⚠️ Unable to reach AI Assistant: {exc}"
