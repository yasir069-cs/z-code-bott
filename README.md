# Crypto Signal Bot

Signals-only crypto market-intelligence bot. Scans the full Binance **USDT-M
futures** market every 5 minutes during the evening session (**18:00 – 23:00
IST**), scores each coin top-down (1H context → 15M confirmation → 5M entry)
with a **graded confluence model**, asks **OpenRouter (NVIDIA Nemotron)** for the
final BUY/SELL/HOLD plus SL/TP/RR, alerts on Telegram, and logs every signal to
CSV.

**This bot never trades.** No Binance API keys, no order endpoints — public
market data only.

> The entry strategy is defined by the owner's handwritten note, transcribed and
> made authoritative in **[strategy_spec.md](strategy_spec.md)**. When code and
> that file disagree, the file is right. This README describes how the code
> currently implements it.

## Install & run

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env            # fill in TELEGRAM_TOKEN / TELEGRAM_CHAT_ID /
                                  # OPENROUTER_API_KEY (all optional for testing)

python main.py                    # production: APScheduler, 18:00–23:00 IST
python main.py --once             # DEMO/TEST: one full scan cycle right now
```

Without keys the bot still runs: AI failures take the documented Python fallback
(alerts tagged "AI Unavailable — Indicator based signal") and Telegram alerts are
logged instead of sent.

## AI layer

- **Provider:** OpenRouter. Primary model `nvidia/nemotron-3-ultra-550b-a55b:free`,
  secondary fallback `deepseek/deepseek-chat-v3.1:free`, then the Python fallback.
- **Batched:** all candidates from one scan go in a single request (sorted by
  confluence, chunked at 12), returning a JSON **array** keyed by symbol — so the
  free tier's 50-requests/day cap stops binding and scan time collapses from
  ~50s to ~10s.
- **Retry:** 3 attempts per model with exponential backoff on 429 / 5xx / timeout
  / malformed JSON before falling to the next model.
- `AI_MAX_TOKENS = 2000` (a batch needs the room; 300 used to truncate answers
  mid-`reason`). Reasoning mode stays **off** on purpose — with a token cap the
  model spends the budget thinking and never emits the JSON.
- Per-IST-day budget accounting (`AI_DAILY_BUDGET = 50`), surfaced in `/status`,
  with a one-time Telegram notice when the budget is exhausted.

## DEMO / TEST modes (safe, no trading)

| Command | What it does |
|---|---|
| `python main.py --once` | One real production scan cycle on live data |
| `python scripts/e2e_demo.py [SYMBOL]` | Forces the gates open on real candles and runs decision → guard → alert → CSV end-to-end (writes `signals_log_demo.csv`) |
| `python scripts/funnel_check.py` | Live funnel stats: coins → sweeps → 1H → 15M → 5M |
| `python scripts/schedule_check.py` | Prints all scheduler fire times for the next 24h (expects 60 scans, 18:00:15…22:55:15) |
| `python backtest.py --horizon-hours 24` | Backtests `signals_log.csv` against real Binance history → `backtest_report.txt` |
| `python -m pytest tests/ -q` | Full unit test suite (no network needed) |

## Required `.env` variables

```
TELEGRAM_TOKEN=<from @BotFather>        # optional; alerts logged if missing
TELEGRAM_CHAT_ID=<your chat id>         # optional
OPENROUTER_API_KEY=<from openrouter.ai> # optional; fallback used if missing
AI_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free      # optional override
AI_MODEL_FALLBACK=deepseek/deepseek-chat-v3.1:free   # optional override
ACCOUNT_BALANCE=1000                    # optional; USDT balance for position sizing
RISK_PER_TRADE_PCT=2.0                  # optional; max % risk per trade
LOG_LEVEL=INFO                          # optional
```

## Pipeline (every 5 minutes, 60 scans/session)

```
ALL USDT-M FUTURES PAIRS (fetch_tickers, dynamic)
→ drop stablecoin / leveraged-token bases (EXCLUDED_BASES + UP/DOWN/BULL/BEAR)
→ 24h volume ≥ $50M USDT
→ duplicate guard checked FIRST (cooling-down coins cost zero fetches)
→ scoring.py, top-down, each timeframe scored 0–100:
    1H context   (weight 0.40): zone + RSI + volume + Bollinger + sweep
    15M confirm  (weight 0.30): RSI + volume + Bollinger
    5M entry     (weight 0.30): RSI + volume + Bollinger
  EMA21 & VWAP are HARD gates on every timeframe (they define direction).
  confluence = 0.40·1H + 0.30·15M + 0.30·5M   gates: 1H≥55, 15M≥50, 5M≥50, total≥55
→ rank by confluence, batch the survivors to OpenRouter/Nemotron
    → BUY/SELL/HOLD + entry/SL/TP/RR + confidence + reason   fail → Python fallback
→ funding-rate-aware leverage + risk-based position size (2% of ACCOUNT_BALANCE)
→ duplicate guard record (15 min per coin, reset at session end)
→ Telegram alert (BUY/SELL only — HOLD stays silent)
→ signals_log.csv (append-only, 20 columns)
```

### Graded zone & sweep (the heart of the note)

- **Zone** is graded, not a 30% cliff: BUY `range_pos ≤ 0.30` scores full, `0.30–0.60`
  tapers, `> 0.60` (wrong half) is rejected. SELL mirrors on `1 − range_pos`.
- **Liquidation sweep** (wick pierces a 20-candle swing level, closes back inside,
  wick > 2× body, volume spike) carries the **heaviest single weight (25)** and
  decays with age. It is **not** a hard gate: a setup with everything else aligned
  but no sweep still alerts, at a reduced score, with confidence **capped at 69**
  (just below HIGH) and the alert labelled "Not detected (confidence capped)".

## Timeliness (never late)

- **One** cron job, `hour=18-22, minute=*/5, second=15` — the `:15` offset guarantees
  the just-closed 5M candle is published before the scan reads it, and a single
  job means no hour-boundary overlap. `max_instances=1`, `misfire_grace_time=120`,
  `coalesce=True` are set explicitly.
- **OHLCV TTL cache** keyed `(symbol, timeframe)` — 1H refetches hourly, 15M every
  15 min, 5M every scan — plus **concurrent fetching** (8 workers, shared token
  bucket under Binance's rate limit).
- **Hard per-scan deadline** (`SCAN_DEADLINE_SECONDS = 240`): past it the scan stops
  AI work, Python-fallbacks the rest, and Telegram-notifies — so a scan can never
  bleed into the next 5-minute slot.

## Example Telegram alert

```
🟢 BUY SIGNAL — BTC/USDT  |  LONG 📈
━━━━━━━━━━━━━━━━━━
📊 RSI Bounce at 50 detected ✅
📊 Indicators
├ RSI: 58.1 → 63.4 📈
├ EMA21: ✅ Above
├ VWAP: ✅ Above
└ Volume: 1.8x avg 💹

🌊 Liq Sweep: bullish detected

💰 Trade Levels
├ Entry:  63058.5
├ SL:     62946.6
├ TP:     63282.3
└ RR:     1:2.00

🔥 Confidence: HIGH
🎯 Confluence: 78/100  (1H 82 · 15M 74 · 5M 76)
📝 Sweep below 62946 reclaimed; 5M RSI higher low; volume rising
━━━━━━━━━━━━━━━━━━
🤖 AI: nvidia/nemotron-3-ultra-550b-a55b:free
```

Fallback alerts replace the footer with `⚠️ AI Unavailable — Indicator based signal`.

## signals_log.csv

Append-only, 20 columns: `timestamp, coin, signal, entry, SL, TP, RR, leverage,
position_size, funding_rate, confidence, confluence, score_1h, score_15m,
score_5m, sweep, sweep_age, rsi_bounce, reason, ai_used`. The row is built
generically from `config.CSV_COLUMNS`, so a field can never again be silently
dropped. At startup `logger.migrate_csv_header()` archives any file with a stale
header to `signals_log.csv.v1.bak` and starts a fresh, correctly-headed file —
history is preserved, never overwritten.

## Files

`main.py` (entry + scheduler + scan loop), `config.py`, `scanner.py` (scan +
OHLCV cache + concurrency + exclusions), `indicators.py`, `scoring.py` (confluence
engine), `filter_1h.py` (+ sweep detection), `filter_15m.py`, `filter_5m.py`,
`ai_decision.py` (batch + retry + budget), `fallback.py`, `duplicate_guard.py`,
`alerts.py`, `telegram_bot.py` (chat listener), `chat_assistant.py`, `logger.py`,
`backtest.py`, `requirements.txt`. Plus `tests/` (210 tests) and `scripts/`.
Spec docs live in the repo root; **[strategy_spec.md](strategy_spec.md)** is the
authority and `memory.md` tracks progress.

## Accuracy notes

- pandas-ta does ALL indicator math; `bbands(ddof=0)` matches TradingView's
  population-std bands.
- Each timeframe fetches 50 strategy candles + 250 warm-up candles so the Wilder
  RSI/EMA recursions converge to TradingView values (verified <1e-6 in
  `tests/test_indicators.py`).
- Only closed candles are used — the forming candle is dropped at fetch time.

## Safety

Signals only. No `create_order` anywhere, no Binance keys (public mode), no web
dashboard, no multi-user. The scheduler makes zero market/AI API calls outside
18:00 – 23:00 IST.
