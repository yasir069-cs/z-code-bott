# Architecture — Crypto Signal Bot

> Entry logic is authoritative in **[strategy_spec.md](strategy_spec.md)**. This
> file describes the runtime architecture that implements it.

## Tech stack

- **Language:** Python 3.12
- **Exchange:** CCXT — Binance **USDT-M futures**, public mode, no API key
- **Indicators:** pandas-ta (RSI 14, EMA21, daily VWAP, Bollinger 20/2, ATR 14)
- **Scoring:** `scoring.py` — graded 0–100 confluence per timeframe
- **AI decision:** OpenRouter — `nvidia/nemotron-3-ultra-550b-a55b:free` (primary),
  `deepseek/deepseek-chat-v3.1:free` (fallback), batched + retried
- **Alerts / chat:** python-telegram-bot (`alerts.py` sender, `telegram_bot.py`
  long-lived chat listener, `chat_assistant.py` LLM Q&A)
- **Scheduler:** APScheduler `BlockingScheduler`, timezone Asia/Kolkata (IST)
- **Logs:** Python logging → signals appended to `signals_log.csv`

## Schedule

- **Window:** 18:00 – 23:00 IST, every 5 minutes.
- **One** `CronTrigger(hour="18-22", minute="*/5", second=15)` → **60 scans/session**.
  The `second=15` offset publishes the just-closed 5M candle; a single job means no
  hour-boundary overlap. `max_instances=1`, `misfire_grace_time=120`, `coalesce=True`.
- **Outside the window:** zero market/AI calls. A session-start and session-end
  Telegram message bracket each evening; session-end reports a health line (scans
  ran, signals sent) so silence is confirmed-healthy, not a dead bot.

## Full flow (every 5 minutes)

```
STEP 1 — Universe
  fetch_tickers() → all USDT-M futures pairs
  drop stablecoin / leveraged-token bases (EXCLUDED_BASES + UP/DOWN/BULL/BEAR)
  drop 24h volume < $50M USDT
  → active liquid coins

STEP 2 — Duplicate guard FIRST
  coin signalled in the last 15 min? → skip before any OHLCV fetch

STEP 3 — 1H context (weight 0.40)   [scoring.score_1h]
  cached OHLCV (1h TTL) + indicators
  HARD gates: EMA21 side, VWAP side, RSI band, overbought/oversold, BB squeeze,
              zone in the wrong half (BUY range_pos > 0.60)
  graded:    zone (25) · RSI (20) · volume (15) · Bollinger (15) · sweep (25)
  score < MIN_SCORE_1H (55) → drop coin, never fetch 15M/5M

STEP 4 — 15M confirmation (weight 0.30)   [scoring.score_ltf]
  same direction must hold; graded RSI (40) · volume (30) · Bollinger (30)
  score < MIN_SCORE_15M (50) → reject

STEP 5 — 5M entry (weight 0.30)   [scoring.score_ltf]
  graded RSI (40) · volume (30) · Bollinger (30); its close is the entry price
  score < MIN_SCORE_5M (50) → reject

  confluence = 0.40·1H + 0.30·15M + 0.30·5M ;  gate: total ≥ MIN_CONFLUENCE (55)

STEP 6 — AI decision (batched)   [ai_decision.nemotron_decisions]
  survivors ranked by confluence, chunked at 12, sent as ONE request per chunk
  → JSON array keyed by symbol: BUY/SELL/HOLD + entry/SL/TP/RR + confidence + reason
  retry 3× (429/5xx/timeout/bad-JSON) → secondary model → Python fallback
  per-IST-day budget (50); geometry validated per element (BUY: sl<entry<tp)

STEP 7 — Sizing
  funding-rate-aware leverage + position size = RISK_PER_TRADE_PCT of ACCOUNT_BALANCE

STEP 8 — Confidence cap
  no sweep → confidence capped at NO_SWEEP_CONFIDENCE_CAP (69, just below HIGH)

STEP 9 — Alert + log
  BUY/SELL → Telegram (via the long-lived listener Bot); HOLD → silent
  every decision → signals_log.csv (20 columns, append-only)

  Hard deadline: past SCAN_DEADLINE_SECONDS (240) the scan stops AI work,
  Python-fallbacks the rest, notifies Telegram — never bleeds into the next slot.
```

## Liquidation sweep detection (`filter_1h.detect_sweep`)

```
BULLISH (for BUY):  lower wick pierces the 20-candle swing low, body closes back
                    ABOVE it, wick > 2× body, volume spike vs the 20-candle avg
BEARISH (for SELL): upper wick pierces the 20-candle swing high, body closes back
                    BELOW it, wick > 2× body, volume spike vs the 20-candle avg
```

The sweep is **scored, not gated**: full weight when fresh (age ≤ 2 candles),
decaying with age, 0 when absent or stale. Absence caps confidence and is labelled
in the alert — see the sanctioned-deviations table in strategy_spec.md.

## Timeliness architecture

- **OHLCV TTL cache** (`scanner.py`), keyed `(symbol, timeframe)`, TTL = one candle
  period — cuts steady-state fetches from ~152/scan to ~11–50.
- **Concurrent fetch** — `ThreadPoolExecutor(max_workers=8)` with a shared
  token-bucket limiter so workers can't stampede the Binance rate limit.
- **Deadline** threaded through fetch + AI stages via `time.monotonic()`.

## Folder structure

```
crypto-bot/
├── strategy_spec.md     # AUTHORITATIVE handwritten-note transcription
├── PRD.md · Architecture.md · rules.md · memory.md
│
├── main.py              # entry, scheduler, scan loop, sizing, sig assembly
├── config.py            # all settings: session, weights, gates, AI, exclusions
├── scanner.py           # universe scan + volume/base filters + OHLCV cache + concurrency
├── indicators.py        # RSI, EMA21, VWAP, BB, ATR, range_pos, swings (pandas-ta)
├── scoring.py           # graded confluence engine (zone/RSI/vol/BB/sweep, gates)
├── filter_1h.py         # 1H context + detect_sweep
├── filter_15m.py        # 15M confirmation
├── filter_5m.py         # 5M entry
├── ai_decision.py       # OpenRouter batch + retry + per-day budget
├── fallback.py          # Python-only decision when AI is unavailable
├── duplicate_guard.py   # 15 min cooldown per coin
├── alerts.py            # Telegram alert formatting + sending
├── telegram_bot.py      # long-lived chat listener (commands + Q&A)
├── chat_assistant.py    # OpenRouter-backed assistant for user questions
├── logger.py            # signals_log.csv writer + header migration
├── backtest.py          # backtesting engine (--horizon-hours)
├── .env                 # TELEGRAM_TOKEN, OPENROUTER_API_KEY, sizing
└── requirements.txt
```

## AI capacity

Batching means one request carries a whole scan's candidates instead of one per
coin, so the free tier's 50/day cap rarely binds. `AI_MAX_TOKENS = 2000`,
reasoning disabled (documented starvation issue), retry/backoff on transient
errors, and a secondary model before the Python fallback.
