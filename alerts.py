"""Phase 10 — Telegram alerts.

Formatted BUY/SELL alerts via python-telegram-bot. HOLD stays silent.
Alert contains: coin, signal, entry, SL, TP, RR, reason and the AI status
line when the fallback was used. Telegram failure -> log the error and
continue the bot (rules.md).
"""
import asyncio
import html
import logging

import telegram

import config

log = logging.getLogger("alerts")

_FALLBACK_TAG = "AI Unavailable - Indicator based signal"


def format_alert(sig: dict) -> str:
    """Build the HTML alert message."""
    icon = "🟢" if sig["signal"] == "BUY" else "🔴"
    lines = [
        f"{icon} <b>{sig['signal']} SIGNAL — {html.escape(sig['coin'])}</b>",
        f"⚡ Market: <b>Futures (USDT-M Perpetual)</b>",
        f"Entry: <code>{sig['entry']:.6g}</code>",
        f"SL: <code>{sig['SL']:.6g}</code>",
        f"TP: <code>{sig['TP']:.6g}</code>",
        f"RR: <code>1:{sig['RR']:.2f}</code>",
        f"Reason: {html.escape(str(sig['reason']))}",
    ]
    if not sig.get("ai_used", False):
        lines.append(f"⚠️ <b>{_FALLBACK_TAG}</b>")
    else:
        lines.append(f"🤖 AI: {config.AI_MODEL}")
    return "\n".join(lines)


async def _send(text: str) -> None:
    bot = telegram.Bot(token=config.TELEGRAM_TOKEN)
    async with bot:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


def send_telegram_text(text: str) -> bool:
    """Send a custom text message to Telegram; never raises (failure is logged)."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured - message logged only:\n%s", text)
        return False
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(asyncio.wait_for(_send(text), timeout=20))
        finally:
            loop.close()
        log.info("Telegram notification sent")
        return True
    except (telegram.error.TelegramError, asyncio.TimeoutError, OSError) as exc:
        log.error("Telegram notification failed: %s - bot continues", exc)
        return False


def send_alert(sig: dict) -> bool:
    """Send one alert; never raises (failure is logged, bot continues)."""
    if sig["signal"] not in ("BUY", "SELL"):
        return False  # HOLD stays silent by design
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured - alert logged only:\n%s", format_alert(sig))
        return False
    text = format_alert(sig)
    success = send_telegram_text(text)
    if success:
        log.info("Telegram alert sent for %s %s", sig["coin"], sig["signal"])
    return success
