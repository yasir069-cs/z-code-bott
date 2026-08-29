# Deployment — free forever options

The bot needs: Python 3.11+, ~512MB RAM, outbound HTTPS only (Binance,
OpenRouter, Telegram), and it self-schedules 18:30–21:30 IST (server clock
timezone does not matter — Asia/Kolkata is hardcoded in the scheduler).

## Option 1 — Oracle Cloud "Always Free" (recommended cloud)

Genuinely free forever (not a trial). Limits are far above what this bot
uses. A card is required at signup but never charged.

1. Sign up: https://www.oracle.com/cloud/free/ → create an "Always Free"
   VM (Ampere A1 or VM.Standard.E2.1.Micro, Ubuntu 22.04/24.04 image).
2. Open outbound internet (default) and SSH in.
3. On the VM:
   ```bash
   git clone https://github.com/yasir069-cs/z-code-bott.git && cd z-code-bott
   bash deploy/setup_linux.sh
   nano .env                 # paste your keys
   .venv/bin/python main.py --once     # one live test scan
   sudo cp deploy/crypto-signal-bot.service /etc/systemd/system/
   sudo nano /etc/systemd/system/crypto-signal-bot.service  # fix User + paths
   sudo systemctl daemon-reload
   sudo systemctl enable --now crypto-signal-bot
   journalctl -u crypto-signal-bot -f   # watch it live
   ```
   Auto-restarts on crash/reboot. Logs also in `bot.log` and signals in
   `signals_log.csv`.

## Option 2 — Your own PC (zero limits, zero cost)

Works today — the bot is already installed. The PC must be on during
18:30–21:30 IST. Auto-start it with Task Scheduler (run once, as admin):

```
schtasks /create /tn "CryptoSignalBot" /tr "C:\Users\AAQIB\.zcode\workspace\default\deploy\start_bot.bat" /sc daily /st 18:25
```

## Option 3 — Google Cloud free tier (backup choice)

Free-forever e1-micro (1GB RAM, US regions) — enough for this bot.
Same setup steps as Oracle after VM creation.

## Notes for ALL options

- Never commit `.env`; rotate any key/token you pasted in chats.
- Test the deployment: `python scripts/send_test_alert.py` (Telegram),
  `python main.py --once` (one real scan cycle).
- No trading endpoints are used anywhere — signals only.
