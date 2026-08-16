Architecture.md — Crypto Signal Bot
Tech Stack
Language: Python 3.11+
Exchange: CCXT (Binance public mode, no API key needed)
Indicators: pandas-ta (RSI, EMA21, VWAP, Bollinger Bands)
AI Decision: Anthropic Claude API (claude-sonnet-4-6)
Alerts: python-telegram-bot
Scheduler: APScheduler (6:30 PM - 9:30 PM IST)
Logs: Python logging module → signals saved to signals_log.csv
Bot Schedule
START: 6:30 PM IST (13:00 UTC) — NY Session Open
END: 9:30 PM IST (16:00 UTC) — London-NY Overlap End
INTERVAL: Every 5 minutes
SLEEP: 9:30 PM → 6:30 PM next day (complete silence)
Total scans per session: 36
Full Flow (Every 5 Minutes)
6:30 PM IST — Bot Starts
│
├── STEP 1: Full Exchange Scan
│   ├── fetch_tickers() → ALL USDT pairs (1000+ coins)
│   ├── Remove coins with 24h volume < $5M (dead coins)
│   └── Result: 200-300 active liquid coins
│
├── STEP 2: 1H Analysis — Context
│   ├── Fetch last 50 candles (1H OHLCV)
│   ├── Calculate indicators via pandas-ta:
│   │   ├── RSI (14)
│   │   ├── EMA21
│   │   ├── VWAP
│   │   └── Bollinger Bands
│   │
│   ├── BUY Context Check:
│   │   ├── Price in BOTTOM zone (lower 30% of last 50 candles range)?
│   │   ├── RSI 50 → 70 range (crossing up from below)?
│   │   ├── EMA21 below price (price above EMA)?
│   │   ├── VWAP below price?
│   │   ├── Volume increasing?
│   │   ├── BB lower band — price near or touched?
│   │   └── Liquidation Sweep below detected?
│   │       (wick pierced recent swing low + body closed back above)
│   │       (wick > 2x body size + volume spike on that candle)
│   │
│   ├── SELL Context Check:
│   │   ├── Price in TOP zone (upper 30% of last 50 candles range)?
│   │   ├── RSI 50 → 35 range (dropping from above)?
│   │   ├── EMA21 above price (price below EMA)?
│   │   ├── VWAP above price?
│   │   ├── Volume increasing?
│   │   ├── BB upper band — price near or touched?
│   │   └── Liquidation Sweep above detected?
│   │       (wick pierced recent swing high + body closed back below)
│   │       (wick > 2x body size + volume spike on that candle)
│   │
│   └── 
 Context fail → skip coin, don't check 15M/5M
│       
 Context pass → move to 15M
│
├── STEP 3: 15M Analysis — Confirmation
│   ├── Fetch last 50 candles (15M OHLCV)
│   ├── Calculate same indicators (RSI, EMA21, VWAP, BB)
│   │
│   ├── BUY Confirmation (need 4/5):
│   │   ├── RSI 50-70 
│   │   ├── EMA21 below price 
│   │   ├── VWAP below price 
│   │   ├── Volume increasing vs previous candle 
│   │   └── BB lower band touch/near 
│   │
│   ├── SELL Confirmation (need 4/5):
│   │   ├── RSI 35-50 
│   │   ├── EMA21 above price 
│   │   ├── VWAP above price 
│   │   ├── Volume increasing vs previous candle 
│   │   └── BB upper band touch/near 

│   │
│   └── 
 Less than 4/5 → reject
│       
 4/5 or 5/5 → move to 5M
│
├── STEP 4: 5M Analysis — Entry
│   ├── Fetch last 50 candles (5M OHLCV)
│   ├── Calculate indicators + store last 10 RSI values
│   │
│   ├── BUY Entry:
│   │   ├── RSI between 50-70
│   │   ├── RSI trend UP (higher lows in last 3 candles)
│   │   ├── EMA21 below price
│   │   ├── VWAP below price
│   │   ├── Volume increasing
│   │   └── BB lower band bounce
│   │
│   ├── SELL Entry:
│   │   ├── RSI between 35-50
│   │   ├── RSI trend DOWN (lower highs in last 3 candles)
│   │   ├── EMA21 above price
│   │   ├── VWAP above price
│   │   ├── Volume increasing
│   │   └── BB upper band rejection
│   │
│   └── Result: 5-15 final candidates pass all 3 timeframes
│
├── STEP 5: Claude AI Decision
│   ├── For each candidate, send to Claude:
│   │   ├── Coin + current price + entry price
│   │   ├── 1H: zone (bottom/top) + liq sweep details
│   │   ├── 15M: all indicator values
│   │   ├── 5M: last 10 RSI candles history
│   │   ├── RSI trend direction explicitly stated
│   │   ├── EMA21, VWAP, BB values (all 3 timeframes)
│   │   ├── Volume trend (last 5 candles)
│   │   └── Recent swing high/low + ATR value
│   │
│   ├── Claude decides:
│   │   ├── BUY / SELL / HOLD
│   │   ├── SL price (based on swing low/high + ATR)
│   │   ├── TP price (next resistance/support)
│   │   ├── RR Ratio
│   │   └── One line reason
│   │
│   ├── Claude Edge Cases:
│   │   ├── RSI 50→55→51→56 (higher low = bullish → BUY)
│   │   ├── RSI ok but volume dropping → HOLD
│   │   ├── 5M signal but 1H context weak → HOLD
│   │   └── Liq sweep old (5+ candles ago) → HOLD
│   │
│   ├── HOLD → No alert sent, just logged
│   │
│   └── If Claude API fails:
│       ├── Use Python filter result as decision
│       ├── SL = recent swing low/high (automatic)
│       ├── TP = 2x SL distance (fixed 1:2 RR)
│       └── Alert tagged: 
 AI Unavailable
│
├── STEP 6: Duplicate Guard
│   ├── Same coin signaled within last 20 min? → Skip
│   └── Tracker resets at 9:30 PM daily
│
├── STEP 7: Telegram Alert
│   ├── BUY/SELL alert with full details
│   └── HOLD → silent (only logged)
│
└── STEP 8: Signal Log
    └── Save to signals_log.csv:
        coin, signal, entry, SL, TP, RR, reason, timestamp
9:30 PM IST — Bot Sleeps 
Liquidation Sweep Detection Logic
BULLISH SWEEP (for BUY):
 Lower wick pierced below recent swing low (last 20 candles)
 Candle BODY closed back ABOVE that swing low
 Lower wick size > 2x candle body size
 Volume on that candle > 1.5x average volume (last 20 candles)
BEARISH SWEEP (for SELL):
 Upper wick pierced above recent swing high (last 20 candles)
 Candle BODY closed back BELOW that swing high
 Upper wick size > 2x candle body size
 Volume on that candle > 1.5x average volume (last 20 candles)
All 4 conditions = Valid Liquidation Sweep 
Folder Structure
crypto-bot/
├── PRD.md
├── Architecture.md
├── rules.md
├── phases.md
├── design.md
├── memory.md
│
├── main.py              # Entry point + APScheduler timer
├── config.py            # All settings (times, thresholds, keys)
├── scanner.py           # Full exchange scan + volume filter
├── indicators.py        # RSI, EMA21, VWAP, BB via pandas-ta
├── filter_1h.py         # 1H context + liquidation sweep
├── filter_15m.py        # 15M confirmation check
├── filter_5m.py         # 5M entry + RSI trend direction
├── ai_decision.py       # Claude API → BUY/SELL/HOLD + SL/TP
├── fallback.py          # Python-only decision if Claude fails
├── duplicate_guard.py   # 20 min cooldown per coin
├── alerts.py            # Telegram formatted alert sender
├── logger.py            # signals_log.csv writer
├── backtest.py          # Future backtesting engine
├── .env                 # TELEGRAM_TOKEN, ANTHROPIC_API_KEY
└── requirements.txt
API Cost
36 scans/session × ~10 Claude calls = ~360 calls/day
Cost per call: ~$0.001
Daily: ~$0.36
Monthly: ~$10/month max