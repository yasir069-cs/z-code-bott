# rules.md — Coding Rules

> Strategy authority is **[strategy_spec.md](strategy_spec.md)**. These are the
> engineering rules the code must obey.

## Libraries to use
- `ccxt` → exchange data only (Binance USDT-M futures, public mode)
- `pandas-ta` → ALL indicator calculations (never manual math)
- `requests` → OpenRouter HTTP (AI decision + chat assistant)
- `python-telegram-bot` → alerts + chat listener
- `APScheduler` → timer (never `sleep()` loops)
- `python-dotenv` → load `.env` secrets
- `logging` → all logs (never `print`)
- `csv` → signal log file

## Hard rules
- **No auto trade execution — signals only.** No `create_order`, no Binance API keys.
- No paid exchange APIs — CCXT public mode only.
- No hardcoded secrets — always `.env`.
- No bare `except` — catch specific exceptions.
- No manual indicator math — pandas-ta only.
- Never call the AI without the Python scoring result first.
- Never send a duplicate alert within `DUPLICATE_COOLDOWN_MIN` (15 min) for the same coin.
- A scan must never bleed past `SCAN_DEADLINE_SECONDS` (240) into the next slot.

## Scheduler rules
- Timezone: Asia/Kolkata (IST = UTC+5:30, no DST).
- Active: 18:00 – 23:00 IST only, every 5 minutes.
- **One** cron job with `second=15` offset; `max_instances=1`, `misfire_grace_time=120`,
  `coalesce=True`. No overlapping jobs.
- Outside hours: zero market/AI calls.

## Scanner rules
- Always fetch ALL USDT-M futures pairs (never hardcode a list).
- Exclude stablecoin bases (`EXCLUDED_BASES`) and leveraged tokens (UP/DOWN/BULL/BEAR).
- Volume filter: skip coins with 24h volume < `$50M` USDT.
- Check the duplicate guard **before** fetching OHLCV for a coin.
- OHLCV TTL cache keyed `(symbol, timeframe)`; concurrent fetch capped at
  `FETCH_MAX_WORKERS` with a shared token bucket.

## Indicator rules
- RSI period 14 · EMA period 21 · VWAP daily · Bollinger 20/2 · ATR 14.
- `bbands(ddof=0)` (population std, TradingView-compatible).
- 50 strategy candles + 250 warm-up candles per timeframe; only closed candles.

## Scoring rules (scoring.py)
- **Hard gates** (fail → drop coin): EMA21 side, VWAP side, RSI band, overbought/
  oversold, BB bandwidth < `BB_BANDWIDTH_MIN`, zone in the wrong half.
- **Graded** 0→full: zone (1H 25), RSI (1H 20 / LTF 40), volume (1H 15 / LTF 30),
  Bollinger (1H 15 / LTF 30), sweep (1H 25, age-decayed).
- `confluence = 0.40·score_1h + 0.30·score_15m + 0.30·score_5m`.
- Gates: `MIN_SCORE_1H=55`, `MIN_SCORE_15M=50`, `MIN_SCORE_5M=50`, `MIN_CONFLUENCE=55`.
- Sweep is weighted, **not** a hard gate; its absence caps confidence at
  `NO_SWEEP_CONFIDENCE_CAP` (69) and is labelled in the alert.

## AI rules (ai_decision.py)
- Provider OpenRouter. Primary `AI_MODEL`, secondary `AI_MODEL_FALLBACK`, then Python.
- **Batch** candidates (sorted by confluence, chunked at `AI_BATCH_MAX=12`) into one
  request → JSON array keyed by symbol.
- **Retry** `AI_RETRY_MAX=3` per model with exponential backoff on 429/5xx/timeout/bad-JSON.
- `AI_MAX_TOKENS=2000`; `AI_REASONING_ENABLED=False` (token cap starves JSON if on).
- Per-IST-day budget `AI_DAILY_BUDGET=50`; notify Telegram once when exhausted.
- Returns BUY/SELL/HOLD + SL + TP + RR + confidence + one-line reason; validate
  geometry per element (BUY: `sl < entry < tp`). HOLD → log only, no alert.

## Fallback rules
- AI unavailable → Python decision from the scoring result.
- SL = sweep level / swing / entry ∓ 1.5×ATR; TP = 1:2 RR minimum.
- Confidence derived from confluence; `rsi_bounce_detected` preserved.
- Alert footer: `AI Unavailable — Indicator based signal` (em dash).

## Duplicate guard rules
- Track last signal timestamp per coin; same coin within 15 min → skip silently.
- Applies to every decision (BUY/SELL/HOLD) to prevent log spam.
- Reset at session end.

## Signal log rules
- Append every BUY/SELL/HOLD to `signals_log.csv`; never delete or truncate.
- Row built generically from `config.CSV_COLUMNS` (20 columns).
- On startup, `migrate_csv_header()` archives a stale-header file to `.v1.bak`.

## Error handling
- Exchange fetch fails → retry with backoff → skip coin.
- AI fails → `fallback.py` decision.
- Telegram fails → log error, continue the bot (never crash on a send).
- Rate limit → exponential backoff (1s, 2s, 4s).
