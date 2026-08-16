#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VM (Oracle Cloud Always Free, GCP, etc.)
# Usage: bash deploy/setup_linux.sh
set -euo pipefail

cd "$(dirname "$0")/.."

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "!! Fill in .env (OPENROUTER_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) before starting."
fi

.venv/bin/python -m pytest tests/ -q

cat <<'EOF'

Setup complete.
1) Fill .env with your keys
2) Test one scan:  .venv/bin/python main.py --once
3) Install the service:
   sudo cp deploy/crypto-signal-bot.service /etc/systemd/system/
   (edit User=/path inside), then:
   sudo systemctl daemon-reload && sudo systemctl enable --now crypto-signal-bot
EOF
