PRD.md — Crypto Signal Bot
What to Build
A crypto market scanning bot that scans the FULL exchange every 5 minutes during NY
session (6:30 PM - 9:30 PM IST), filters coins using a custom multi-indicator top-down
strategy, uses Claude AI for final BUY/SELL/HOLD decision + SL/TP calculation, and
sends alerts via Telegram.
Targeted Users
Solo trader (Yasir) — personal use only
Core Features
1. Full exchange scan (1000+ USDT pairs) every 5 minutes
2. Top-down analysis: 1H → 15M → 5M
3. Liquidation sweep detection on every coin
4. Python filter (indicators check)
5. Claude AI → BUY/SELL/HOLD + SL + TP + Reason
6. Telegram alerts with full details
7. No duplicate signals (20 min cooldown per coin)
8. Signal logs saved to file (for future backtesting)
9. Bot active: 6:30 PM - 9:30 PM IST only
10. If Claude fails → Python indicator based fallback decision
Out of Scope
Auto trade execution
Web dashboard
Multi-user
