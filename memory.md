memory.md — Progress Tracker

Current Phase
✅ ALL 13 PHASES COMPLETE (2026-08-16) — bot implemented, tested (77 unit tests),
   and validated live against Binance public data.

All Decisions Finalized
Exchange: Binance (CCXT public, free)
Scan: Full exchange (1000+ USDT pairs)
Interval: Every 5 minutes
Timer: 6:30 PM - 9:30 PM IST (NY Session)
Timeframes: 1H → 15M → 5M (top-down)
Indicators: RSI(14), EMA21, VWAP, BB
Liq Sweep: wick>2x body + volume spike + body reversal
AI: OpenRouter Nemotron 3 Ultra → BUY/SELL/HOLD + SL/TP/RR + confidence + reason
Fallback: Python decision if OpenRouter fails ("AI Unavailable" tagged)
Duplicate: 20 min cooldown per coin (reset 9:30 PM IST)
Logs: signals_log.csv (append only)

Environment notes (final)
Python 3.12.10 · pandas-ta 0.4.71b0 · pandas 3.0.5 · numpy 2.2.6 · ccxt 4.5.73 · OpenRouter/Nemotron (provider swapped from Anthropic 2026-08-16)
pandas-ta bbands called with ddof=0 (population std) to match TradingView.
Indicator fetch uses 50 strategy candles + 250 warm-up candles so Wilder/EMA
recursions converge to TradingView values exactly.

BUY Strategy (from handwritten notes)
RSI: 50 → 70 (trending up)
EMA21: price above
VWAP: price above
Volume: increasing
BB: near lower band
Context: Bottom zone + Liquidation Sweep
Timeframe: 1H → 15M → 5M

SELL Strategy (from handwritten notes)
RSI: 50 → 35 (trending down)
EMA21: price below
VWAP: price below
Volume: increasing
BB: near upper band
Context: Top zone + Liquidation Sweep
Timeframe: 1H → 15M → 5M

Phase Status
✅ Phase 1: Data Foundation — scanner.py (live-verified: 5 coins × 3 TFs clean)
✅ Phase 2: Indicators — indicators.py (values match Wilder/TV reference math)
✅ Phase 3: Liquidation Sweep — filter_1h.py detect_sweep (crafted + live tests)
✅ Phase 4: 1H Filter — filter_1h.py analyze_1h
✅ Phase 5: 15M Filter — filter_15m.py (4/5 rule)
✅ Phase 6: 5M Filter — filter_5m.py (RSI trend, higher lows/lower highs)
✅ Phase 7: AI decision — ai_decision.py (OpenRouter nvidia/nemotron-3-ultra-550b-a55b:free, strict JSON, max 300 tokens, reasoning disabled — with the 300-token cap, enabled reasoning starves the JSON output)
✅ Phase 8: Fallback — fallback.py (swing SL, 1:2 RR, tagged alert)
✅ Phase 9: Duplicate Guard — duplicate_guard.py (20 min, reset 21:30 IST)
✅ Phase 10: Telegram — alerts.py (HOLD silent)
✅ Phase 11: Signal Logger — logger.py (signals_log.csv, append only)
✅ Phase 12: Scheduler — main.py (36 scans 18:30–21:25 IST, verified fire times)
✅ Phase 13: Backtesting — backtest.py (live-verified vs real Binance history)

Interpretation decisions (documented)
- Duplicate guard applies to every decision (BUY/SELL/HOLD): a coin that
  signaled anything in the last 20 min is skipped silently, HOLD included
  (prevents log spam; alerts were already covered by rules.md).
- "Near BB" = candle range overlaps ±0.25% (15M/5M) / ±0.5% (1H) band
  neighborhood — a price far BEYOND the band is not "near" it.
- Fallback SL: sweep level if valid, else 1H swing, else entry ∓ 1.5×ATR
  (only when the swing is on the wrong side of entry — degenerate case).
- Warm-up fetch (250 extra candles) is a data-accuracy addition; the
  strategy itself only ever looks at the last 50 candles per timeframe.
