rules.md — Coding Rules
Libraries to Use
ccxt → exchange data only
pandas-ta → ALL indicator calculations (never manual)
anthropic → Claude API
python-telegram-bot → alerts
APScheduler → timer
python-dotenv → load .env secrets
logging → all logs (never print)
csv → signal log file
Hard Rules
No auto trade execution — signals only
No paid APIs — CCXT public mode only
No hardcoded API keys — always .env
No bare except — specific exceptions only
No sleep() loops — APScheduler only
No manual indicator math — pandas-ta only
Never call Claude without Python filter first
Never send duplicate alert within 20 min same coin
Scheduler Rules
Timezone: Asia/Kolkata (IST = UTC+5:30 exactly)
Active: 6:30 PM to 9:30 PM IST only
Interval: every 5 minutes
Outside hours: zero activity, zero API calls
Scanner Rules
Always fetch ALL USDT pairs (never hardcode list)
Volume filter: skip coins with 24h volume < $5M USDT
Fetch 1H, 15M, 5M candles (last 50 each)
Indicator Rules
RSI period: 14
EMA period: 21
VWAP: daily
Bollinger Bands: 20 period, 2 std dev
Always store last 10 RSI values for trend analysis
1H Filter Rules
Bottom zone: price in lower 30% of last 50 candle range
Top zone: price in upper 30% of last 50 candle range
Liq Sweep: wick > 2x body + volume spike > 1.5x avg + body reversal
Context fail → skip coin immediately
15M Filter Rules
BUY: EMA21 < price, VWAP < price, volume up, BB lower touch
SELL: EMA21 > price, VWAP > price, volume up, BB upper touch
Need 4/5 → else reject
5M Filter Rules
BUY: RSI 50-70 + RSI higher low (last 3 candles up)
SELL: RSI 35-50 + RSI lower high (last 3 candles down)
All 15M conditions must hold on 5M too
Claude AI Rules
Send: coin, price, 1H context, 15M values, 5M RSI history (10 candles)
Send: recent swing high/low + ATR
Send: RSI trend direction explicitly
Claude returns: BUY/SELL/HOLD + SL + TP + RR + one line reason
Max tokens: 300
HOLD → no Telegram alert, only log
Claude Fallback Rules
If Claude fails → use Python filter result
SL = recent swing low (BUY) or swing high (SELL)
TP = entry + 2x SL distance (1:2 RR fixed)
Alert must say: 
 AI Unavailable — Indicator based signal
Duplicate Guard Rules
Track last signal timestamp per coin in memory dict
Same coin within 20 min → skip silently
Reset all at 9:30 PM IST
Signal Log Rules
Save every BUY/SELL signal to signals_log.csv
Columns: timestamp, coin, signal, entry, SL, TP, RR, reason, ai_used
HOLD signals also logged (for future analysis)
Never delete log file — append only
Error Handling
Exchange fetch fails → retry 3x → skip coin
Claude fails → fallback.py decision
Telegram fails → log error, continue bot
Rate limit → exponential backoff (1s, 2s, 4s)