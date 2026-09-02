"""Interactive LLM Chat Assistant for Telegram User Queries.

Allows users to chat with the bot in Telegram and ask questions about:
  - The bot's decision hierarchy (structure/S-R/liquidity/risk first, 1H -> 15M -> 5M)
  - Indicators (RSI, EMA21, daily VWAP, Bollinger Bands, ATR) — secondary confirmation only
  - Recent signal performance and logs from signals_log.csv
  - General cryptocurrency trading, risk management, and market insights

Uses the configured AI provider (AgentRouter/DeepSeek v4 by default) with rich context.
"""
import csv
from datetime import datetime
import logging
import time
from typing import Optional

import config
import ai_decision

log = logging.getLogger("chat_assistant")


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


def _health_lines() -> str:
    """Subsystem health block for /status. Best-effort per line — one broken
    subsystem must never take the whole /status down."""
    lines = []

    def _add(label: str, value: str):
        lines.append(f"{label}: {value}")

    try:
        from main import get_coordinator
        h = get_coordinator().health()
        _add("Scheduler", "HEALTHY" if h["error_streak"] < 3
             else f"DEGRADED ({h['error_streak']} errors in a row)")
        _add("Active scan", f"YES ({h['active_scan_id']})" if h["active"] else "NO")
        if h["last_scan_at"]:
            _add("Last scan", f"{h['last_scan_at']} ({h['last_duration_s']}s, "
                              f"{h['last_signals']} signal(s))")
    except Exception as exc:
        _add("Coordinator", f"UNAVAILABLE ({type(exc).__name__})")

    try:
        import liquidation
        s = liquidation.stream_status()
        if s["status"] == "FRESH":
            _add("Liquidations", "HEALTHY")
        elif s["status"] == "STALE":
            age = f" ({s['age_s']:.0f}s)" if s["age_s"] else ""
            _add("Liquidations", f"STALE{age}")
        else:
            _add("Liquidations", "DISCONNECTED")
    except Exception as exc:
        _add("Liquidations", f"UNAVAILABLE ({type(exc).__name__})")

    try:
        from main import _ai_worker
        s = _ai_worker.status()
        _add("AI", f"{s['last_status']}"
             + (f" ({s['pending']} pending)" if s["pending"] else ""))
    except Exception as exc:
        _add("AI", f"UNAVAILABLE ({type(exc).__name__})")

    try:
        import telegram_bot
        _add("Telegram", "HEALTHY" if telegram_bot._listener_running()
             else "LISTENER DOWN")
    except Exception:
        _add("Telegram", "UNKNOWN")

    try:
        from scanner import cache_stats
        c = cache_stats()
        _add("OHLCV cache", f"{c['entries']} frames, {c['hit_rate']:.0%} hits")
    except Exception:
        pass

    return "\n".join(lines)


def get_bot_status_summary() -> str:
    """Return current bot operational status."""
    now_ist = datetime.now(config.TZ)
    hhmm = now_ist.strftime("%H:%M")
    in_session = config.SESSION_START <= hhmm < config.SESSION_END
    status = "🟢 ACTIVE (Scanning Market)" if in_session else "🌙 SLEEPING (Outside Trading Window)"

    # AI budget + OHLCV cache health (best-effort — /status must never break).
    # Liquidation stream health already has its own line in _health_lines().
    extra = ""
    try:
        from ai_decision import budget_status
        b = budget_status()
        extra += f"\nAI Budget: {b['used']}/{b['limit']} used today ({b['remaining']} left)"
    except Exception:
        pass

    health = _health_lines()
    extra += _ai_audit_lines()

    return (
        f"🟢 BOT HEALTH\n\n"
        f"Current Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST\n"
        f"Session Hours: {config.SESSION_START} to {config.SESSION_END} IST\n"
        f"Status: {status}\n"
        f"Active Model: {config.AI_MODEL}\n"
        f"Volume Filter: >= ${config.VOLUME_MIN_USDT:,} USDT\n\n"
        f"{health}"
        f"{extra}"
    )


def _ai_audit_lines() -> str:
    """Is the AI stage actually answering, and was any of it used?

    `/status` used to prove the AI was "on" by naming the model, which answered a
    different question: a provider returning 200 with prose in every reply looked
    identical here to a working audit. The last batch's answered/expected counts,
    its LONG/SHORT/NO_TRADE tally and the failure text are what tell those apart,
    and the contract line says in one place that the answer is audited and never
    applied. Import is deferred and every access guarded: /status must survive a
    half-initialised bot, and chat_assistant is imported by main itself.
    """
    lines = ""
    try:
        from main import _ai_worker
        st = _ai_worker.status()
    except Exception:
        return lines
    tally = st.get("tally") or {}
    if st.get("expected"):
        head = f"{st.get('last_status', '?')} — {st.get('answered', 0)}/{st.get('expected', 0)} setups answered"
        if tally:
            head += (f" | LONG {tally.get('LONG', 0)} SHORT {tally.get('SHORT', 0)}"
                     f" NO_TRADE {tally.get('NO_TRADE', 0)} no-answer {tally.get('NO_ANSWER', 0)}")
            head += (f" | agreement agree {tally.get('AGREE', 0)}"
                     f" veto {tally.get('VETO_PROPOSED', 0)}"
                     f" signal {tally.get('SIGNAL_PROPOSED', 0)}"
                     f" disagree {tally.get('DISAGREE', 0)}")
    else:
        head = f"{st.get('last_status', 'IDLE')} (no batch this session yet)"
    lines += f"\nAI Audit: {head}"
    if st.get("last_error"):
        lines += f"\nAI Audit last error: {str(st['last_error'])[:120]}"
    try:
        import ai_decision
        caps = ai_decision.provider_caps()
        mode = ("response_format=json_object enforced" if caps.get("json_object")
                else "json_object rejected by the provider (prompt-only contract)")
    except Exception:
        mode = "contract state unavailable"
    lines += f"\nAI Contract: audit-only, never applied to a shipped signal | {mode}"
    return lines + "\n"


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
    """Send user query to the AI provider and return the assistant's reply."""
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

    # The provider call goes through ai_decision.complete_chat, i.e. the SAME
    # transport the decisions use: browser-like User-Agent (the provider's WAF
    # challenges the bare python-requests UA), retry with backoff, the fallback
    # model, the endpoint resolved per call, and the per-IST-day budget. Chat used
    # to hand-roll a single requests.post with none of that — it failed on the
    # first 503 AND its requests were invisible to a daily cap they were spending.
    try:
        reply = ai_decision.complete_chat(
            messages,
            max_tokens=600, temperature=0.3,
            deadline=time.monotonic() + config.AI_CHAT_DEADLINE_SECONDS,
        ).strip()
        return reply or ("I received your message, but the AI generated an empty "
                        "response. Please ask again.")
    except ai_decision.AIDecisionError as exc:
        # one line in the log for the owner, a safe summary for the user
        log.warning("AI assistant call failed: %s", exc)
        return ("⏳ The AI assistant is busy or the provider is not answering right "
                "now. Please try again in a moment — /status, /signals and /strategy "
                "work without it.")
    except Exception as exc:            # the bot must never die on a chat reply
        log.error("AI assistant failed unexpectedly: %s", exc, exc_info=True)
        return ("⚠️ The AI assistant hit an unexpected error and could not answer. "
                "The details are in the bot log.")
