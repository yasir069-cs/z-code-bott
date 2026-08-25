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

_FALLBACK_TAG = "AI Unavailable — Indicator based signal"

# Confidence display mapping
_CONF_META = {
    "high":   ("🔥", "HIGH"),
    "medium": ("⚡", "MEDIUM"),
    "low":    ("⚠️", "LOW"),
}


def _conf_label(confidence) -> tuple[str, str]:
    """Return (emoji, label) for a confidence value (float 0-100 or string)."""
    try:
        val = float(confidence)
        if val >= 70:
            return _CONF_META["high"]
        if val >= 45:
            return _CONF_META["medium"]
        return _CONF_META["low"]
    except (TypeError, ValueError):
        key = str(confidence).lower()
        return _CONF_META.get(key, ("⚡", str(confidence).upper()))


def format_alert(sig: dict) -> str:
    """Build the emoji-heavy HTML alert message."""
    is_buy   = sig["signal"] == "BUY"
    dir_icon = "🟢" if is_buy else "🔴"
    dir_word = "LONG 📈" if is_buy else "SHORT 📉"

    entry = sig.get("entry") or sig.get("Entry")
    sl    = sig.get("sl")    or sig.get("SL")
    tp    = sig.get("tp")    or sig.get("TP")
    rr    = sig.get("rr")    or sig.get("RR")

    # RSI bounce badge
    bounce_line = ""
    if sig.get("rsi_bounce_detected"):
        bounce_icon = "📊 RSI Bounce at 50 detected ✅" if is_buy else "📊 RSI Rejection at 50 detected ✅"
        bounce_line = f"\n{bounce_icon}"

    # Indicator summary (optional fields — present when bundle is passed through)
    ind = sig.get("indicators", {})
    rsi_now  = ind.get("rsi_now")
    rsi_prev = ind.get("rsi_prev")
    ema_above = ind.get("price_above_ema")
    vwap_above = ind.get("price_above_vwap")
    vol_ratio  = ind.get("volume_ratio")

    ind_lines = []
    if rsi_now is not None and rsi_prev is not None:
        arrow = "📈" if rsi_now > rsi_prev else "📉"
        ind_lines.append(f"├ RSI: <code>{rsi_prev:.1f} → {rsi_now:.1f}</code> {arrow}")
    if ema_above is not None:
        ind_lines.append(f"├ EMA21: {'✅ Above' if ema_above else '❌ Below'}")
    if vwap_above is not None:
        ind_lines.append(f"├ VWAP: {'✅ Above' if vwap_above else '❌ Below'}")
    if vol_ratio is not None:
        ind_lines.append(f"└ Volume: <code>{vol_ratio:.1f}x</code> avg 💹")

    ind_block = ("\n📊 <b>Indicators</b>\n" + "\n".join(ind_lines)) if ind_lines else ""

    # Liquidation sweep
    sweep = sig.get("sweep", {})
    if sweep.get("detected"):
        sweep_text = f"🌊 <b>Liq Sweep:</b> {html.escape(str(sweep.get('type', '')))} detected"
    else:
        sweep_text = "➖ <b>Liq Sweep:</b> Not detected <i>(confidence capped)</i>"

    # Confidence
    conf_emoji, conf_label = _conf_label(sig.get("confidence", 0))

    # Confluence line — present when run_scan forwards the scored context
    conf_val = sig.get("confluence")
    conf_line = ""
    if conf_val is not None:
        parts = []
        for label, key in (("1H", "score_1h"), ("15M", "score_15m"), ("5M", "score_5m")):
            v = sig.get(key)
            if v is not None:
                parts.append(f"{label} {v:.0f}")
        detail = f"  ({' · '.join(parts)})" if parts else ""
        conf_line = f"🎯 <b>Confluence:</b> <code>{conf_val:.0f}/100</code>{detail}\n"

    # Trade levels
    entry_str = f"<code>{entry:.6g}</code>" if entry else "—"
    sl_str    = f"<code>{sl:.6g}</code>"    if sl    else "—"
    tp_str    = f"<code>{tp:.6g}</code>"    if tp    else "—"
    rr_str    = f"<code>1:{rr:.2f}</code>"  if rr    else "—"

    # AI / fallback footer
    if sig.get("ai_used", False):
        footer = f"🤖 <i>AI: {html.escape(config.AI_MODEL)}</i>"
    else:
        footer = f"⚠️ <b>{_FALLBACK_TAG}</b>"

    reason_text = html.escape(str(sig.get("reason", "")))

    msg = (
        f"{dir_icon} <b>{sig['signal']} SIGNAL — {html.escape(sig['coin'])}</b>  |  {dir_word}\n"
        f"━━━━━━━━━━━━━━━━━━"
        f"{bounce_line}"
        f"{ind_block}\n\n"
        f"{sweep_text}\n\n"
        f"💰 <b>Trade Levels</b>\n"
        f"├ Entry:  {entry_str}\n"
        f"├ SL:     {sl_str}\n"
        f"├ TP:     {tp_str}\n"
        f"└ RR:     {rr_str}\n\n"
        f"{conf_emoji} <b>Confidence:</b> {conf_label}\n"
        f"{conf_line}"
        f"📝 <i>{reason_text}</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {config.SESSION_START}–{config.SESSION_END} IST  |  1H → 15M → 5M\n"
        f"{footer}"
    )
    return msg


async def _send(text: str) -> None:
    """One-shot send: build a Bot, send, tear it down.

    Fallback path — used before the chat listener's long-lived Bot exists
    (e.g. `--once`, or the first startup message) or if that listener is down.
    """
    bot = telegram.Bot(token=config.TELEGRAM_TOKEN)
    async with bot:
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


def _send_via_listener(text: str) -> bool:
    """Send using the long-lived Bot already running in the chat-listener's
    event loop, reusing its warm connection pool instead of constructing a new
    Bot and event loop on every message (finding G).

    Returns False when the listener is not available so the caller can fall
    back to the one-shot path. run_coroutine_threadsafe is thread-safe, so this
    is safe to call from any scheduler worker thread while the listener owns
    the loop.
    """
    try:
        import telegram_bot  # lazy import: avoids an import cycle at module load
    except Exception:
        return False
    app = getattr(telegram_bot, "_bot_app", None)
    loop = getattr(telegram_bot, "_bot_loop", None)
    if app is None or loop is None or not loop.is_running() or not getattr(app, "running", False):
        return False
    try:
        coro = app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)
        return True
    except Exception as exc:  # any failure -> caller uses the one-shot fallback
        log.warning("listener-loop send failed (%s); falling back to one-shot", exc)
        return False


def send_telegram_text(text: str) -> bool:
    """Send a custom text message to Telegram; never raises (failure is logged)."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured - message logged only:\n%s", text)
        return False
    # Preferred: reuse the listener's long-lived Bot + loop (warm pool).
    if _send_via_listener(text):
        log.info("Telegram notification sent")
        return True
    # Fallback: one-shot Bot + event loop.
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
