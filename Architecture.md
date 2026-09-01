# Architecture — Crypto Signal Bot

> The decision hierarchy is authoritative in **[price_action_spec.md](price_action_spec.md)**;
> the owner's indicator note in **[strategy_spec.md](strategy_spec.md)** is the
> secondary confirmation layer. This file describes the runtime architecture.

## Tech stack

- **Language:** Python 3.12
- **Exchange:** CCXT — Binance **USDT-M futures**, public mode, no API key
- **Decision core:** `decision.py` orchestrates deterministic detectors
  (`market_structure`, `support_resistance`, `liquidity`, `price_action`,
  `trendlines`, `mtf`, `futures_context`) → `setup_quality` → `risk_gate` →
  LONG / SHORT / NO_TRADE. Look-ahead-safe; identical in live and backtest.
- **Indicators (secondary):** pandas-ta (RSI 14, EMA21, daily VWAP, Bollinger 20/2,
  ATR 14) fold in via `scoring.indicator_confirmation` — a bounded confirmation
  sub-score, never a gate or the direction-chooser
- **AI (explanation only):** OpenRouter — `nvidia/nemotron-3-ultra-550b-a55b:free`
  (primary), `deepseek/deepseek-chat-v3.1:free` (fallback); a local template writes
  the prose today. The LLM never decides.
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

STEP 3 — Retain frames
  cached OHLCV (per-TF TTL) for 1H (HTF), 15M (setup), 5M (entry) — kept, not discarded,
  and handed as { "1h": HTF, "15m": setup, "5m": entry } to the decision core

STEP 4 — decision.decide(frames, funding, oi)   [deterministic core, look-ahead-safe]
  1 market structure per TF → mtf.combine → htf_bias + direction (+ counter_htf)
      direction is None (no HTF bias / range)         → NO_TRADE("no_directional_bias")
  2 support/resistance zones   3 liquidity & sweeps (reclaim + mandatory confirm)
  4 price action & volume      5 trendlines/channels (confluence only)
  6 multi-TF (mandatory): entry TF opposes HTF        → NO_TRADE("counter_htf")
  7 OI + funding via futures_context (contextual; safe-degrade, no fabricated data)
  9 indicators fold in as a BOUNDED secondary sub-score (scoring.indicator_confirmation)
  → setup_quality: primary < QUALITY_PRIMARY_FLOOR(45) → NO_TRADE("insufficient_primary_evidence")
                   quality  < QUALITY_MIN(50)          → NO_TRADE("low_setup_quality")
  8 risk_gate (mandatory): structure SL, target off nearest opposing zone
      rr < MIN_RR(1.5) / stop > 3·ATR / opposing zone < 1·ATR / wide spread → NO_TRADE(reason)
  → LONG / SHORT / NO_TRADE + entry/SL/TP/RR + setup_quality + full evidence + no_trade_reasons
    (NO_TRADE is valid & PREFERRED when confluence is thin — a trade is never forced)

STEP 5 — Rank
  tradable survivors ranked by setup_quality; the strongest get scarce resources first

STEP 6 — Explanation (cosmetic)   [fallback.explanation_fallback today; batched-LLM path retained]
  a finished decision → natural-language prose; a slow/failed/absent LLM never
  changes or drops a decision

STEP 7 — Sizing
  funding-rate-aware leverage + position size = RISK_PER_TRADE_PCT of ACCOUNT_BALANCE

STEP 8 — Confidence cap
  no sweep → confidence capped at NO_SWEEP_CONFIDENCE_CAP (69, just below STRONG)

STEP 9 — Alert + log (owner's tier system)
  BUY/SELL → Telegram (via the long-lived listener Bot); HOLD → silent
  quality < 50 → log-only (ignored); 50-60 NORMAL · 60-70 HIGH · 70+ STRONG
  every decision → signals_log.csv (28 columns, append-only)

  Hard deadline: past SCAN_DEADLINE_SECONDS (240) the scan stops AI work,
  Python-fallbacks the rest, notifies Telegram — never bleeds into the next slot.
```

## Liquidity & sweeps

**Primary (layer 3, `liquidity.py`):** equal highs/lows are stop pools. A long
needs a **sell-side sweep + reclaim + a mandatory bullish confirmation candle**
(mirror for a short) — a bare touch is not an entry (`LIQ_CONFIRM_REQUIRED`).
This is real evidence the core acts on, not a score tweak.

**Secondary (`filter_1h.detect_sweep` → `scoring.py`):** the legacy wick-based
sweep detector is retained as an indicator-confirmation input.

```
BULLISH: lower wick pierces the 20-candle swing low, body closes back ABOVE it,
         wick > 2× body, volume spike vs the 20-candle avg
BEARISH: upper wick pierces the 20-candle swing high, body closes back BELOW it,
         wick > 2× body, volume spike vs the 20-candle avg
```

With no fresh sweep the alert's confidence is **capped at 69**
(`NO_SWEEP_CONFIDENCE_CAP`, just below the STRONG tier) and labelled in the
alert — see the sanctioned-deviations table in strategy_spec.md.

## Timeliness architecture

- **OHLCV TTL cache** (`scanner.py`), keyed `(symbol, timeframe)`, TTL = one candle
  period — cuts steady-state fetches from ~152/scan to ~11–50.
- **Concurrent fetch** — `ThreadPoolExecutor(max_workers=8)` with a shared
  token-bucket limiter so workers can't stampede the Binance rate limit.
- **Deadline** threaded through fetch + AI stages via `time.monotonic()`.

## Folder structure

```
crypto-bot/
├── price_action_spec.md # PRIMARY authority — the decision hierarchy
├── strategy_spec.md     # SECONDARY — handwritten indicator note (confirmation layer)
├── PRD.md · Architecture.md · rules.md · memory.md
│
├── main.py              # entry, scheduler, scan loop, sizing, sig assembly
├── config.py            # all settings: session, TF roles, weights, gates, risk, AI
├── scanner.py           # universe scan + volume/base filters + OHLCV cache + OI history
│
│   # ── decision core (deterministic, look-ahead-safe) ──
├── decision.py          # the brain: orchestrates the 9-layer hierarchy → LONG/SHORT/NO_TRADE
├── market_structure.py  # swings, HH/HL/LH/LL, trend/range, BOS, CHoCH, displacement
├── support_resistance.py# auto horizontal zones (ranges), strength-scored, major/minor
├── liquidity.py         # equal highs/lows, sweep + reclaim + mandatory confirmation
├── price_action.py      # rejection wicks, engulfing, displacement, failed/retest breakout
├── trendlines.py        # auto-fit trendlines/channels, break+retest (confluence only)
├── mtf.py               # combine per-TF bias; reject/reduce when entry opposes HTF
├── futures_context.py   # OI + funding read contextually; safe-degrade when missing
├── setup_quality.py     # 0–100 quality from PRIMARY evidence + bounded indicator term
├── risk_gate.py         # structure SL, target off nearest opposing zone, RR / NO_TRADE
│
│   # ── secondary / support ──
├── indicators.py        # RSI, EMA21, VWAP, BB, ATR, range_pos, swings (pandas-ta)
├── scoring.py           # secondary indicator-confirmation scorer (repurposed graded fns)
├── filter_1h.py         # 1H feature extractor + detect_sweep
├── filter_15m.py        # 15M feature extractor
├── filter_5m.py         # 5M feature extractor
├── ai_decision.py       # OpenRouter batch + retry + budget (explanation transport)
├── fallback.py          # local explanation template (decision is never faked)
├── duplicate_guard.py   # 15 min cooldown per coin
├── alerts.py            # structured Telegram alert (market context primary + indicators)
├── telegram_bot.py      # long-lived chat listener (commands + Q&A)
├── chat_assistant.py    # OpenRouter-backed assistant for user questions
├── logger.py            # signals_log.csv writer (28 cols) + header migration
├── backtest.py          # logged-signal replay + --strategy core replay (look-ahead-safe)
├── .env                 # TELEGRAM_TOKEN, OPENROUTER_API_KEY, sizing
└── requirements.txt
```

## AI capacity (explanation only)

The deterministic core decides every signal; the LLM only turns a finished
decision into prose, so a slow, failed, or missing LLM is **cosmetic**. Today a
local template (`fallback.explanation_fallback`) writes the explanation and the
alert footer reads *"explanation generated locally"*. The batched transport is
retained for when the LLM prose is switched on: one request carries a whole
scan's candidates (sorted by setup_quality, chunked at 12), so the free tier's
50/day cap rarely binds. `AI_MAX_TOKENS = 2000`, reasoning disabled (documented
starvation issue), retry/backoff on transient errors, then a secondary model
before the local template.
