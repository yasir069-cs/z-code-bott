"""Send one harmless TEST alert to Telegram through the production alert
path (alerts.py formatting + sending). Uses ONLY the bot configured in
TELEGRAM_TOKEN. Never places trades.

Usage: python scripts/send_test_alert.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

config.setup_logging()

import alerts


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise SystemExit("Telegram not configured - set TELEGRAM_TOKEN / TELEGRAM_CHAT_ID in .env")

    sig = {"coin": "TEST/USDT", "signal": "BUY", "entry": 100.0, "SL": 98.0,
           "TP": 104.0, "RR": 2.0, "ai_used": False,
           "reason": "TEST ALERT - no real signal, delivery check only"}
    sent = alerts.send_alert(sig)
    print(f"test alert: {'DELIVERED' if sent else 'NOT delivered (see log)'}")


if __name__ == "__main__":
    main()
