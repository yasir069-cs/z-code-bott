"""Interactive Telegram Bot with LLM Chat Assistant.

Runs 24/7 in a background thread alongside APScheduler market scans.
Handles commands (/start, /help, /status, /signals, /strategy, /ask)
and answers any general crypto or bot queries using the LLM engine.
"""
import asyncio
import logging
import threading
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from chat_assistant import (
    ask_crypto_assistant,
    get_bot_status_summary,
    get_recent_signals_summary,
)
import config
import ondemand  # on-demand scan control

log = logging.getLogger("telegram_bot")

# In-memory short-term chat history per user {chat_id: [{"role": "user"|"assistant", "content": str}]}
_CHAT_HISTORIES: dict[int, list[dict]] = {}
_MAX_HISTORY_LEN = 8

_bot_thread: Optional[threading.Thread] = None
_bot_app: Optional[Application] = None
# The listener's event loop, published so alerts.send_telegram_text can reuse
# the long-lived Bot running on it instead of building a new Bot per message.
_bot_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_history(chat_id: int) -> list[dict]:
    return _CHAT_HISTORIES.setdefault(chat_id, [])


def _append_history(chat_id: int, role: str, content: str) -> None:
    history = _get_history(chat_id)
    history.append({"role": role, "content": content})
    if len(history) > _MAX_HISTORY_LEN:
        _CHAT_HISTORIES[chat_id] = history[-_MAX_HISTORY_LEN:]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    welcome_text = (
        "👋 <b>Welcome to Crypto Signal Bot AI!</b>\n\n"
        "I am your automated crypto market scanning and AI assistant bot.\n\n"
        "🤖 <b>What I do:</b>\n"
        "• Scan all USDT-M Futures pairs every 5 min between <b>18:00 - 23:00 IST</b>\n"
        "• Score coins top-down 1H → 15M → 5M into a 0–100 confluence rating\n"
        "• Generate verified BUY/SELL futures alerts with AI SL/TP (min 1:2 RR)\n"
        "• <b>24/7 AI Chat:</b> Ask me any question about crypto, market trends, or our strategy!\n\n"
        "📌 <b>Quick Commands:</b>\n"
        "/status — Live bot status & schedule\n"
        "/signals — Recent trade signals\n"
        "/strategy — Explain indicator strategy\n"
        "/help — View all commands & tips\n"
        "/scan_on — 🟢 Start manual scan anytime\n"
        "/scan_off — 🔴 Stop manual scan\n\n"
        "💬 <i>Just send any message to chat with the AI assistant!</i>"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "📖 <b>Crypto Signal Bot — Help & Commands</b>\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/status</code> — Current scanning status, IST time, active AI model\n"
        "• <code>/signals</code> — Show recent 5 BUY/SELL/HOLD signals\n"
        "• <code>/strategy</code> — Detailed explanation of the 3-layer trading strategy\n"
        "• <code>/ask [question]</code> — Ask AI any question directly\n"
        "• <code>/clear</code> — Reset your chat history with the AI\n"
        "• <code>/scan_on</code> — 🟢 Manually start scanning (any time, outside 18-23 session)\n"
        "• <code>/scan_off</code> — 🔴 Stop the manual scan session\n\n"
        "💡 <b>Chatting with AI:</b>\n"
        "You don't even need commands — just type your question normally! For example:\n"
        "• <i>\"How does the 1H VWAP and EMA filter work?\"</i>\n"
        "• <i>\"What are liquidation sweeps and why are they useful?\"</i>\n"
        "• <i>\"Give me a quick risk management tip for futures trading.\"</i>\n"
        "• <i>\"Explain RSI higher low pattern in simple terms.\"</i>"
    )
    if update.message:
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    status_text = f"📊 <b>Bot Status Overview:</b>\n\n<pre>{get_bot_status_summary()}</pre>"
    if update.message:
        await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /signals command."""
    signals_text = f"📈 <b>Recent Logged Signals:</b>\n\n{get_recent_signals_summary(5)}"
    if update.message:
        await update.message.reply_text(signals_text)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /strategy command."""
    strat_text = (
        "🎯 <b>Top-Down Confluence Strategy</b>\n\n"
        "Each timeframe is scored 0–100 from the strategy conditions; the weighted\n"
        "total is <b>Confluence = 0.40·1H + 0.30·15M + 0.30·5M</b> (gate ≥ 55).\n\n"
        "<b>1️⃣ 1H Macro Context (weight 0.40):</b>\n"
        "• <b>Zone:</b> bottom→in-between of the range (BUY) / top→in-between (SELL)\n"
        "• <b>RSI:</b> 50→70 rising (BUY) / 50→35 falling (SELL)\n"
        "• <b>EMA21 & VWAP:</b> price above (BUY) / below (SELL) — <i>hard gates</i>\n"
        "• <b>Volume & Bollinger:</b> graded confirmation\n"
        "• <b>Liquidation Sweep:</b> heaviest single weight, age-decayed — labelled, "
        "not a hard gate (no-sweep setups alert with confidence capped just below HIGH)\n\n"
        "<b>2️⃣ 15M Confirmation (weight 0.30):</b>\n"
        "• Same direction holds on graded RSI + Volume + Bollinger\n\n"
        "<b>3️⃣ 5M Entry (weight 0.30):</b>\n"
        "• Graded RSI + Volume + Bollinger; its close becomes the entry price\n\n"
        "<b>4️⃣ AI Final Decision (Nemotron, batched):</b>\n"
        "• Receives the full scored context, sets ATR-based SL/TP, enforces min 1:2 RR, confidence score."
    )
    if update.message:
        await update.message.reply_text(strat_text, parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset conversation memory."""
    if update.effective_chat:
        _CHAT_HISTORIES.pop(update.effective_chat.id, None)
    if update.message:
        await update.message.reply_text("🧹 Chat memory cleared. You can start a fresh conversation!")


def _is_owner(update: Update) -> bool:
    """Check if the sender is the bot owner (TELEGRAM_CHAT_ID)."""
    if not config.TELEGRAM_CHAT_ID:
        return True  # no restriction if chat_id not configured
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    return chat_id == config.TELEGRAM_CHAT_ID


async def cmd_scan_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan_on — start on-demand scan session anytime."""
    if not _is_owner(update):
        if update.message:
            await update.message.reply_text("🚫 Access denied. Only the bot owner can use this command.")
        return

    if update.message:
        await update.message.reply_text("⏳ Starting on-demand scan... Please wait.", parse_mode=ParseMode.HTML)

    import threading
    def _start_in_thread():
        import scanner as sc
        import duplicate_guard as dg
        from main import run_scan
        result = ondemand.start_ondemand_scan(
            run_scan_fn=run_scan,
            make_exchange_fn=sc.make_exchange,
            fetch_funding_fn=sc.fetch_funding_rates,
            DuplicateGuardClass=dg.DuplicateGuard,
        )
        status = result.get("status")
        if status == "started":
            msg = (
                "🟢 <b>On-Demand Scan Started!</b>\n\n"
                "⚡ Scanning every <b>5 minutes</b> \u2014 active until you send /scan_off\n"
                "🔔 You will receive BUY/SELL alerts as signals are found."
            )
        elif status == "already_running":
            msg = "⚠️ On-demand scan is <b>already running</b>. Send /scan_off to stop it first."
        else:
            msg = "❌ Could not start scan. Check bot logs."
        import alerts as alerts_mod
        alerts_mod.send_telegram_text(msg)

    threading.Thread(target=_start_in_thread, daemon=True, name="ScanOnCmd").start()


async def cmd_scan_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan_off — stop on-demand scan session."""
    if not _is_owner(update):
        if update.message:
            await update.message.reply_text("🚫 Access denied. Only the bot owner can use this command.")
        return

    result = ondemand.stop_ondemand_scan()
    status = result.get("status")
    if status == "stopped":
        msg = (
            "🔴 <b>On-Demand Scan Stopped.</b>\n"
            "Bot will resume its permanent session at <b>18:00 IST</b> as usual."
        )
    elif status == "not_running":
        msg = "ℹ️ No on-demand scan is currently running."
    else:
        msg = "❌ Could not stop scan. Check bot logs."

    if update.message:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all non-command user text messages using the LLM."""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else 0

    # Show typing indicator while calling LLM
    try:
        await update.message.chat.send_action("typing")
    except Exception:
        pass

    # Retrieve short-term history
    history = _get_history(chat_id)
    reply = ask_crypto_assistant(user_text, chat_history=history)

    # Save to history
    _append_history(chat_id, "user", user_text)
    _append_history(chat_id, "assistant", reply)

    try:
        await update.message.reply_text(reply)
    except Exception as exc:
        log.warning("Failed sending Markdown reply, retrying as plain text: %s", exc)
        await update.message.reply_text(reply)


def build_telegram_application() -> Optional[Application]:
    """Create and configure the Telegram Application instance."""
    if not config.TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — Telegram chat assistant disabled.")
        return None

    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("ask", handle_message))
    app.add_handler(CommandHandler("scan_on", cmd_scan_on))
    app.add_handler(CommandHandler("scan_off", cmd_scan_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


def _run_listener_loop(app: Application) -> None:
    """Loop runner for background daemon thread."""
    global _bot_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop  # publish for alerts.send_telegram_text to reuse
    try:
        log.info("Telegram Chat Assistant listener started (polling live)")
        loop.run_until_complete(app.initialize())
        loop.run_until_complete(app.start())
        loop.run_until_complete(app.updater.start_polling(drop_pending_updates=True))
        loop.run_forever()
    except Exception as exc:
        log.error("Telegram chat listener error: %s", exc)
    finally:
        try:
            if app.updater and app.updater.running:
                loop.run_until_complete(app.updater.stop())
            if app.running:
                loop.run_until_complete(app.stop())
            loop.run_until_complete(app.shutdown())
        except Exception:
            pass
        _bot_loop = None  # stop alerts from submitting onto a closed loop
        loop.close()


def start_bot_listener() -> bool:
    """Start the interactive Telegram Bot listener in a background daemon thread."""
    global _bot_thread, _bot_app

    if _bot_thread and _bot_thread.is_alive():
        log.info("Telegram listener already running.")
        return True

    app = build_telegram_application()
    if not app:
        return False

    _bot_app = app
    _bot_thread = threading.Thread(target=_run_listener_loop, args=(app,), daemon=True, name="TelegramChatBot")
    _bot_thread.start()
    return True
