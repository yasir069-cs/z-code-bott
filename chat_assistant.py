"""Interactive LLM Chat Assistant for Telegram User Queries.

Allows users to chat with the bot in Telegram and ask questions about:
  - The bot's decision hierarchy (structure/S-R/liquidity/risk first, 1H -> 15M -> 5M)
  - Indicators (RSI, EMA21, daily VWAP, Bollinger Bands, ATR) — secondary confirmation only
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

OPENROUTER_URL = f"{config.AI_BASE_URL}/chat/completions"


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
    status = "🟢 ACTIVE (Scanning 24/7)"

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
        "Session Hours: 24/7 (every 5 minutes)\n"
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
1. Strategy Flow (price-action & market-context first — a deterministic core decides,
   indicators only confirm):
   - A deterministic Python core decides LONG / SHORT / NO_TRADE. NO_TRADE is a valid,
     preferred outcome when multi-factor confluence is insufficient — the bot never
     forces a trade.
   - Decision priority, highest first:
       1) Market structure — HH/HL vs LH/LL, trend/range, BOS, CHoCH, displacement+retest
       2) Support/Resistance — auto horizontal ZONES (ranges, not single prices), major/minor
       3) Liquidity & sweeps — equal highs/lows; a long needs a sell-side sweep + reclaim +
          bullish confirmation (mirror for a short). Post-sweep confirmation is mandatory.
       4) Price action & volume — rejection wicks, engulfing, displacement, failed breakout,
          breakout-retest; a no-volume breakout is weak
       5) Trendlines/channels — confluence only, never standalone
       6) Multi-timeframe (mandatory) — 1H bias -> 15M setup -> 5M entry; reject/reduce when
          the entry TF opposes the higher-timeframe bias
       7) Crypto-futures context — Open Interest + funding, interpreted with context; the bot
          degrades safely when data is missing (never fabricates values)
       8) Risk/Reward gate (mandatory) — structure-based SL, target from the nearest opposing
          zone, configurable minimum R/R; NO_TRADE on poor R / wide stop / nearby opposing zone
       9) Indicators (RSI/EMA/VWAP/Bollinger) — SECONDARY confirmation ONLY; they can never
          trigger or veto a trade on their own.
   - Timeframes: 1H = HTF bias, 15M = setup, 5M = entry (its close is the prospective entry).
   - The core owns entry/SL/TP/RR and a 0-100 setup-quality score. The AI ({config.AI_MODEL})
     or a local template only writes the natural-language EXPLANATION — it can never change
     or drop a signal.
   - Safety: SIGNALS ONLY — never executes trades, holds no exchange API keys.

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
