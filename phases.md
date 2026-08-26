phases.md — Development Phases

> **Historical build log (superseded).** This records the original indicator-checklist
> build sequence and is kept for provenance. The bot now decides on a deterministic
> price-action & market-context core — see **[price_action_spec.md](price_action_spec.md)**
> (primary), **[Architecture.md](Architecture.md)** (runtime), and the price-action-first
> phase status in **[memory.md](memory.md)**. "Claude AI Decision" below is also stale —
> the LLM runs on OpenRouter and now writes the **explanation only**.

Phase 1: Data Foundation
CCXT Binance public mode setup
fetch_tickers() → all USDT pairs
Volume filter ($5M minimum)
Fetch 1H + 15M + 5M OHLCV (last 50 candles each)
 Done when: all 3 timeframes print clean data for 5 coins
Phase 2: Indicators
RSI(14), EMA21, VWAP, BB via pandas-ta
Store last 10 RSI candles for trend
RSI trend direction: higher lows / lower highs
 Done when: values match TradingView exactly
Phase 3: Liquidation Sweep Detection
Detect swing highs/lows (last 20 candles)
Check wick pierced + body reversed + volume spike
Test on known sweep candles
 Done when: correctly identifies real sweeps
Phase 4: 1H Filter
Bottom/top zone detection
Liquidation sweep check
BUY/SELL context classification
 Done when: correctly filters 1000 → ~50 coins
Phase 5: 15M Filter
EMA, VWAP, Volume, BB confirmation
4/5 conditions check
 Done when: 50 → ~15 coins remain
Phase 6: 5M Filter
RSI range + trend direction
Entry conditions check
 Done when: 15 → 5-10 final candidates
Phase 7: Claude AI Decision
Anthropic SDK setup
Full context prompt (RSI history, all TFs, sweep info)
BUY/SELL/HOLD + SL + TP + RR + reason
Edge cases handled (RSI 55→51→56 = still bullish)
 Done when: accurate decisions on test data
Phase 8: Fallback System
Python-only decision when Claude fails
Auto SL/TP calculation (swing based + 1:2 RR)
 tag in alert
 Done when: fallback alert sends correctly
Phase 9: Duplicate Guard
20 min cooldown per coin
Memory dict tracking
Reset at 9:30 PM
 Done when: no duplicate alerts in testing
Phase 10: Telegram Alerts
BUY/SELL formatted alert
Entry, SL, TP, RR, AI reason
HOLD = silent (no alert)
 Done when: clean alert received on phone
Phase 11: Signal Logger
signals_log.csv created
Every BUY/SELL/HOLD logged
ai_used column (True/False)
 Done when: CSV saves correctly after each scan
Phase 12: Scheduler + Timer
APScheduler setup (Asia/Kolkata timezone)
Active: 6:30 PM - 9:30 PM IST
Every 5 minutes
Complete silence outside hours
 Done when: bot auto starts/stops on time
Phase 13: Backtesting
Read signals_log.csv
Match with historical price data
Win rate, avg RR, total signals, P&L
 Done when: report generates correctly