# memory.md — Progress Tracker

## Current state
Reconstruction against the owner's handwritten note (**[strategy_spec.md](strategy_spec.md)**,
authoritative) on branch `reconstruct/spec-alignment`. The pre-existing bot ran
but only partially implemented the note; this work aligns code to the note,
makes alerts correct + on time, and hardens the data → LLM layer. **210 unit
tests pass.**

## All decisions finalized
- **Exchange:** Binance USDT-M **futures** (CCXT public, no keys, signals-only)
- **Universe:** all USDT-M pairs, minus stablecoin/leveraged bases, 24h volume ≥ **$50M**
- **Session:** 18:00 – 23:00 IST, every 5 min at `:15s` → **60 scans/session**
- **Timeframes:** 1H → 15M → 5M (top-down)
- **Indicators:** RSI(14), EMA21, daily VWAP, Bollinger(20,2), ATR(14) — pandas-ta
- **Scoring:** graded confluence 0–100/tf, weighted 0.40/0.30/0.30; EMA21 & VWAP
  hard gates; graded zone; age-decayed sweep (heavy weight, labelled, not gated)
- **AI:** OpenRouter — Nemotron primary, DeepSeek fallback, **batched + retried**,
  2000 tokens, reasoning off, 50/day budget
- **Fallback:** Python decision from confluence when AI unavailable ("AI Unavailable" tag)
- **Duplicate:** **15 min** cooldown per coin (reset at session end)
- **Sizing:** funding-rate-aware leverage + 2%-risk position size
- **Logs:** `signals_log.csv`, 20 columns, append-only, startup header migration

## Environment
Python 3.12.10 · pandas-ta 0.4.71b0 · pandas 3.0.5 · numpy 2.2.6 · ccxt 4.5.73 ·
OpenRouter/Nemotron. `bbands(ddof=0)` (population std, TradingView). 50 strategy
candles + 250 warm-up candles so Wilder/EMA recursions converge exactly.

## Strategy (from the handwritten note — see strategy_spec.md)
**BUY:** RSI 50→70 rising · price above EMA21 · price above VWAP · volume increasing ·
near Bollinger · bottom→in-between zone + liquidation sweep · TF 1H/15M/5M.
**SELL:** mirror — RSI 50→35 falling · below EMA21 · below VWAP · volume increasing ·
near Bollinger · top→in-between zone + liquidation sweep · TF 1H/15M/5M.

## Reconstruction phase status
- ✅ **Phase 0** — clone, branch, baseline (85/9), preserve server's 4 tuned config values, strategy_spec.md.
- ✅ **Phase 1** — `scoring.py` confluence engine; filter_1h/15m/5m delegate to it (fixes A1–A7, D).
- ✅ **Phase 2** — timeliness: single cron job, `:15` offset, OHLCV cache, concurrent fetch, duplicate-guard-first, 240s deadline (B1–B7).
- ✅ **Phase 3** — AI batching (JSON array), retry/backoff, secondary model, 2000 tokens, real zone in prompt, per-day budget (C1–C4, A2).
- ✅ **Phase 4** — alert/log integrity: populate confidence/indicators/sweep/confluence; generic CSV row; header migration; `--horizon-hours` (D, E1–E4).
- ✅ **Phase 5** — robustness: stablecoin blacklist, session health watchdog, richer `/status`, reuse the long-lived Telegram Bot.
- ✅ **Phase 6** — docs rewritten to reality (this file, README, Architecture, rules, PRD); tests added (zone, sweep, batch, retry, migration, populated-alert render, `--horizon-hours`); `schedule_check.py` fixed. **210 tests pass.**

## Interpretation decisions (documented)
- Sweep is heavily weighted, not a hard gate — owner's decision; requiring it
  produced whole sessions with zero alerts. No-sweep setups are labelled and
  confidence-capped at 69.
- Zone "in-between" extends to `range_pos` 0.60 (midpoint); beyond → reject.
- Volume "increase" graded: `> previous` full, `> 20-avg` partial.
- Duplicate guard applies to every decision (BUY/SELL/HOLD) to prevent log spam.
- Warm-up fetch (250 candles) is a data-accuracy addition; the strategy only ever
  looks at the last 50 candles per timeframe.
- Full rationale for each deviation lives in strategy_spec.md's deviations table.

## Deploy (off-session, before 18:00 IST)
Commit the server's 4 tuned config values, `git pull` the branch, run the header
migration (verify `signals_log.csv.v1.bak` + new 20-col header), `main.py --once`
for one live batched scan, then `sudo systemctl restart crypto-signal-bot`.
Rollback: `git checkout a62a08e && systemctl restart` — `.v1.bak` keeps history.
