# rules.md — Coding Rules

> Decision authority is **[price_action_spec.md](price_action_spec.md)** (primary);
> the indicator note **[strategy_spec.md](strategy_spec.md)** is the secondary
> confirmation layer. These are the engineering rules the code must obey.

## Libraries to use
- `ccxt` → exchange data only (Binance USDT-M futures, public mode)
- `pandas-ta` → ALL indicator calculations (never manual math)
- `requests` → OpenRouter HTTP (AI explanation + chat assistant)
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
- The deterministic core (`decision.decide`) owns every LONG/SHORT/NO_TRADE
  decision. The LLM only explains a finished decision — never call it to *decide*.
- Indicators are **secondary**: they can never trigger or veto a trade on their own.
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

## Decision-core rules (decision.py — the brain)
- `decision.decide(frames, funding_rate, oi_df, cfg)` is the **single** decider,
  live and backtest. It returns `LONG` / `SHORT` / `NO_TRADE` + full evidence.
- Evaluate the hierarchy in priority order: market structure → S/R zones →
  liquidity & sweeps → price action & volume → trendlines → **MTF (mandatory)** →
  futures context → **risk/reward gate (mandatory)** → indicators (secondary).
- **`NO_TRADE` is valid and preferred** when multi-factor confluence is thin — a
  trade is never forced.
- Look-ahead-safe: detectors see only closed candles ≤ the decision bar.
- NO_TRADE gates (each a named constant): no directional bias, `counter_htf` when
  `MTF_REQUIRE_HTF_ALIGN`, primary `< QUALITY_PRIMARY_FLOOR` (45), quality
  `< QUALITY_MIN` (50), and risk-gate fails (`rr < MIN_RR` 1.5, stop
  `> RISK_MAX_STOP_ATR`, opposing zone `< RISK_MIN_TARGET_ATR`, spread too wide).
- `setup_quality` weights (sum 100): structure 25 · S/R 20 · liquidity 20 ·
  price action 15 · MTF 10 · trendline 5 · futures 5. Indicators add only a
  **bounded secondary term**, floored out below the primary floor.
- Futures context (OI + funding) is contextual and **safe-degrades**: missing data
  → `available=False` + a `data_warnings` entry, never a fabricated value.

## Secondary indicator scorer (scoring.py)
- Repurposed as `indicator_confirmation(snap, direction, cfg) → {score, agrees, notes}`.
- The graded component fns (zone / RSI / volume / Bollinger / sweep) are reused as
  a bounded confirmation sub-score — they **no longer hard-gate** a coin.
- Legacy sweep scoring: absence caps confidence at `NO_SWEEP_CONFIDENCE_CAP` (69,
  just below the STRONG tier) and is labelled in the alert. The **primary** sweep
  logic lives in `liquidity.py`
  (sweep + reclaim + mandatory confirmation, `LIQ_CONFIRM_REQUIRED`).
- Alert tiers (owner's rule, 2026-09-01): quality < 50 → ignored (log-only);
  50-60 → NORMAL alert; 60-70 → HIGH alert; 70+ → STRONGEST alert.

## AI rules (ai_decision.py — explanation only)
- The LLM **never decides** and never returns signal/levels. It turns a finished
  decision into prose; a slow, failed, or missing LLM is cosmetic.
- Provider OpenRouter. Primary `AI_MODEL`, secondary `AI_MODEL_FALLBACK`, then the
  local template. Explanations are written by the local template **today**.
- **Batch** candidates (sorted by `setup_quality`, chunked at `AI_BATCH_MAX=12`)
  into one request → JSON array keyed by symbol.
- **Retry** `AI_RETRY_MAX=3` per model with exponential backoff on 429/5xx/timeout/bad-JSON.
- `AI_MAX_TOKENS=2000`; `AI_REASONING_ENABLED=False` (token cap starves JSON if on).
- Per-IST-day budget `AI_DAILY_BUDGET=50`; notify Telegram once when exhausted.

## Fallback rules (fallback.py)
- The **decision** never falls back — the deterministic core always decides.
- LLM unavailable → `explanation_fallback(decision)` writes a local template from
  the decision's structured evidence.
- Alert footer: `Decision by deterministic core · explanation generated locally`.

## Duplicate guard rules
- Track last signal timestamp per coin; same coin within 15 min → skip silently.
- Applies to every decision (BUY/SELL/HOLD) to prevent log spam.
- Reset at session end.

## Signal log rules
- Append every BUY/SELL/HOLD to `signals_log.csv`; never delete or truncate.
- Row built generically from `config.CSV_COLUMNS` (28 columns).
- On startup, `migrate_csv_header()` archives a stale-header file to `.vN.bak`.

## Error handling
- Exchange fetch fails → retry with backoff → skip coin.
- AI fails → `fallback.py` local explanation (the decision is unaffected).
- Telegram fails → log error, continue the bot (never crash on a send).
- Rate limit → exponential backoff (1s, 2s, 4s).
