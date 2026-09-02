# rules.md — Coding Rules

> Decision authority is **[price_action_spec.md](price_action_spec.md)** (primary);
> the indicator note **[strategy_spec.md](strategy_spec.md)** is the secondary
> confirmation layer. These are the engineering rules the code must obey.

## Libraries to use
- `ccxt` → exchange data only (Binance USDT-M futures, public mode)
- `pandas-ta` → ALL indicator calculations (never manual math)
- `requests` → the AI provider's HTTP endpoint (`AI_BASE_URL`, AgentRouter by
  default) for the AI explanation, the opinion audit and the chat assistant
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
- Only CLOSED candles, and never invent history: a frame shorter than the
  requested window is served at its real length when it still covers
  `FRAME_MIN_CANDLES` (40) candles — a fresh listing is readable, just with less
  indicator warm-up — and dropped (with one WARNING) below that. Rejecting on
  "fewer rows than asked for" made every newly listed perp permanently invisible.

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

## AI transport rules (shared by every provider call)
- The endpoint is `chat_completions_url()`, resolved on **every call**. A
  module-level constant frozen from `config.AI_BASE_URL` at import kept posting to
  the old host whenever the base URL changed later.
- Headers come from one place: auth, JSON accept, and the browser-like
  `User-Agent` the provider's WAF requires (bare `python-requests` gets
  challenged). Every caller reuses it rather than copying the dict.
- `stream: False` is explicit — some gateways default to SSE and the reply then
  fails JSON parsing.

## AI rules (ai_decision.py — explanation + background audit)
- The LLM **never decides** what is emitted. It turns a finished decision into
  prose and, in the background, records an independent opinion; a slow, failed, or
  missing LLM is cosmetic.
- The audit answer is graded against the emitted verdict in `ai_opinions.csv`
  (`AGREE` / `DISAGREE` / `VETO_PROPOSED` / `SIGNAL_PROPOSED` / `NO_ANSWER`) and
  `final_decision` always names what actually shipped. An opinion can earn trust
  over time; it cannot buy a veto.
- `--force-llm` is the only harness where a verdict is applied, and only after the
  hard gates re-validate it (data validity, level sanity, stop width, min R/R,
  quality floor).
- Provider AgentRouter (`AI_BASE_URL`). Primary `AI_MODEL`
  (`deepseek-v4-flash`), optional `AI_MODEL_FALLBACK` (empty by default), then the
  local template. Explanations are written by the local template **today**.
- **Batch** candidates (sorted by `setup_quality`, chunked at `AI_BATCH_MAX=20`)
  into one request → JSON array keyed by symbol.
- **Retry** `AI_RETRY_MAX=3` per model with exponential backoff on 429/5xx/timeout/bad-JSON.
- `AI_MAX_TOKENS=2000`; `AI_REASONING_ENABLED=False` (token cap starves JSON if on).
- Per-IST-day budget `AI_DAILY_BUDGET=50`; notify Telegram once when exhausted —
  the worker calls `ai_decision.budget_exhausted_notice()` after every batch, since
  retries spend the budget too and silence would otherwise be indistinguishable
  from "nothing worth alerting".
- Prompt fidelity: what the model is told must match what the code measured. The
  scheduled scan carries `feat_1h` into the bundle (the 1H sweep is real, not
  "none detected"); liquidity flags are named by the pool they swept
  (`long_ready` = a swept SELL-side pool, i.e. supports LONG); `range_pos` is
  labelled against `CANDLE_LIMIT` (50), not the 20-candle swing window.

## Fallback rules (fallback.py)
- The **decision** never falls back — the deterministic core always decides.
- LLM unavailable → `explanation_fallback(decision)` writes a local template from
  the decision's structured evidence.
- Alert footer: `Decision by deterministic core · explanation generated locally`.

## Duplicate guard rules
- Two independent windows, both reset at session end (23:00 IST):
  - **Alert cooldown** `DUPLICATE_COOLDOWN_MIN` (15): checked for every candidate
    before its 15M/5M fetch, recorded only for alerted BUY/SELL. A HOLD must never
    occupy it — that would mute a real setup appearing minutes later.
  - **HOLD log cooldown** `HOLD_LOG_COOLDOWN_MIN` (30): an identical rejection
    (same verdict + same blocking reason) is appended once per window per coin; a
    changed verdict or reason is new information and is always written.

## Signal log rules
- Append every BUY/SELL/HOLD to `signals_log.csv`; never delete or truncate.
- Row built generically from `config.CSV_COLUMNS` (28 columns).
- On startup, `migrate_csv_header()` archives a stale-header file to `.vN.bak`;
  `ai_opinions.csv` reconciles its header the same way before appending.
- CSV first, Telegram second: the row is persisted before delivery, and a write
  error is contained per row (`main._persist` → `log_failed`) so one bad row can
  never cost the rest of the scan its alerts.
- Log level: BUY/SELL at INFO, HOLD at DEBUG — the audit trail keeps the row, the
  journal keeps the events.

## Risk-gate rules (risk_gate.py)
- SL comes off structure, padded by `RISK_SL_BUFFER_ATR`; TP comes off the nearest
  **usable** opposing zone, padded back by `RISK_TARGET_ZONE_PAD_ATR`.
- "Usable" means the zone lies beyond the entry and the padded level stays on the
  right side of it. Zones are scanned nearest-first (`RISK_TARGET_SCAN_ZONES`) and
  an unusable one is skipped, not fatal — the price of the old nearest-only rule was
  21/37 live coins rejected as "no achievable target" while a valid level sat a few
  ATR deeper.
- A level that cannot be reached is reported as `no_clear_target` with **no** TP;
  a TP is never fabricated on the wrong side of the entry for a row that HOLDs.
- `into_opposing_zone` (price already pressing into the zone) stays a hard reject:
  that is no room, not a measurement gap.

## Telegram command rules (telegram_bot.py)
- `/scan_on` and `/scan_off` are **owner-only**, and the check **fails closed**:
  `TELEGRAM_CHAT_ID` is the allow-list (comma-separated for several chats), and an
  unset value authorises *nobody*. It used to authorise *everybody* — "no
  restriction if chat_id not configured" handed a 24/7 scan session and the AI
  daily budget to anyone who found the bot.
- A refusal names its reason: unconfigured says set `TELEGRAM_CHAT_ID`,
  non-owner says access denied. Both paths return before any command side effect,
  so a regression in the guard shows up as a failed assertion, not a live scan
  started from a test.
- Read-only commands (`/status`, `/signals`, `/strategy`, `/help`, `/clear`, the
  chat assistant) stay open to whoever can reach the bot; only *control* is gated.

## Chat assistant rules (chat_assistant.py)
- The assistant shares `ai_decision`'s transport (`ai_decision.complete_chat`) —
  same endpoint resolved per call, same browser-like User-Agent, same retry
  ladder and the same daily budget. Hand-rolling a second `requests.post` gave it
  none of that: it died on the first 503 and its requests were invisible to the
  cap they were spending.
- Chat consumes the AI budget and says so via `budget_status()`; it is a provider
  request like any other, not free.
- A failed call answers with a clear "not answering right now", never an invented
  reply. `/status`, `/signals`, `/strategy` work without the AI entirely.

## Secrets and .env rules
- Never paste a credential into this repo — not in docs, not in a test fixture.
  The leak-watchdog in `config.py` stores **prefixes only**
  (`_EXPOSED_TELEGRAM_TOKENS` / `_EXPOSED_OPENROUTER_KEYS`); a full value in the
  list would be the very leak it exists to detect. `test_credentials.py` once held
  the live bot token verbatim, which is how it reached public Git history — a test
  now scans every tracked file for a full-shaped secret so it cannot come back.
- Rotate first, then extend the prefix list with the leaked value's prefix, so
  startup keeps warning until every host is updated. The warning silences itself
  once `.env` carries a new credential.
- Startup validates the AI config (`config.check_config_warnings()`) and logs
  `CONFIG:` lines for a malformed `AI_BASE_URL` (quotes, spaces, a copied
  markdown link), an empty `AI_MODEL`, a fallback equal to the primary, and a
  model with no key. Those are silent-failure modes: the visible symptom is an
  `ai_opinions.csv` full of FAILED rows while the daily budget keeps burning.

## Error handling
- Exchange fetch fails → retry with backoff → skip coin.
- AI fails → `fallback.py` local explanation (the decision is unaffected).
- Telegram fails → log error, continue the bot (never crash on a send).
- Rate limit → exponential backoff (1s, 2s, 4s).
