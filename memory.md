# memory.md — Progress Tracker

## Current state
The bot now decides on a **deterministic price-action & market-context core**
(**[price_action_spec.md](price_action_spec.md)**, primary authority) on branch
`reconstruct/spec-alignment`. Market structure / S/R / liquidity / risk decide
LONG/SHORT/NO_TRADE; RSI/EMA/VWAP/Bollinger are **secondary confirmation only**.
The owner's handwritten note (**[strategy_spec.md](strategy_spec.md)**) is the
documented secondary layer. The LLM writes the **explanation only**. **277 unit
tests pass.**

## All decisions finalized
- **Exchange:** Binance USDT-M **futures** (CCXT public, no keys, signals-only)
- **Universe:** all USDT-M pairs, minus stablecoin/leveraged bases, 24h volume ≥ **$50M**
- **Session:** 18:00 – 23:00 IST, every 5 min at `:15s` → **60 scans/session**
- **Timeframes:** 1H (HTF bias) → 15M (setup) → 5M (entry), roles configurable
- **Decision core:** `decision.decide` — structure → S/R → liquidity → price action
  → trendlines → **MTF (mandatory)** → futures context → **risk gate (mandatory)**
  → LONG/SHORT/NO_TRADE. `NO_TRADE` is valid & preferred; a trade is never forced
- **Indicators (secondary):** RSI(14), EMA21, daily VWAP, Bollinger(20,2), ATR(14)
  — pandas-ta; fold into `setup_quality` as a bounded confirmation term, never a gate
- **Liquidity (primary):** sweep + reclaim + mandatory confirmation (`LIQ_CONFIRM_REQUIRED`)
- **Futures context:** OI + funding, contextual, safe-degrade (no fabricated data)
- **AI:** OpenRouter — Nemotron primary, DeepSeek fallback, batched + retried,
  2000 tokens, reasoning off, 50/day budget — **explanation only, never decides**
- **Fallback:** local explanation template when the LLM is unavailable (the decision
  is never faked); footer "explanation generated locally"
- **Duplicate:** **15 min** cooldown per coin (reset at session end)
- **Sizing:** funding-rate-aware leverage + 2%-risk position size
- **Logs:** `signals_log.csv`, 28 columns, append-only, startup header migration

## Environment
Python 3.12.10 · pandas-ta 0.4.71b0 · pandas 3.0.5 · numpy 2.2.6 · ccxt 4.5.73 ·
OpenRouter/Nemotron. `bbands(ddof=0)` (population std, TradingView). 50 strategy
candles + 250 warm-up candles so Wilder/EMA recursions converge exactly.

## Decision hierarchy (primary — see price_action_spec.md)
Structure (HH/HL/LH/LL, BOS/CHoCH) → S/R zones → liquidity sweep + reclaim +
confirm → price action & volume → trendlines → MTF (1H→15M→5M, mandatory) →
OI/funding context → risk/reward gate (mandatory). Indicators confirm; they never
decide. Output: LONG / SHORT / **NO_TRADE** + entry/SL/TP/RR + evidence + reasons.

## Indicator note (secondary — see strategy_spec.md)
**BUY:** RSI 50→70 rising · above EMA21 · above VWAP · volume increasing · near
Bollinger · bottom→in-between zone + sweep. **SELL:** the mirror. These now fold in
as a bounded confirmation sub-score (`scoring.indicator_confirmation`), not a gate.

## Reconstruction phase status
- ✅ **Phase 0** — clone, branch, baseline (85/9), preserve server's 4 tuned config values, strategy_spec.md.
- ✅ **Phase 1** — `scoring.py` confluence engine; filter_1h/15m/5m delegate to it (fixes A1–A7, D).
- ✅ **Phase 2** — timeliness: single cron job, `:15` offset, OHLCV cache, concurrent fetch, duplicate-guard-first, 240s deadline (B1–B7).
- ✅ **Phase 3** — AI batching (JSON array), retry/backoff, secondary model, 2000 tokens, real zone in prompt, per-day budget (C1–C4, A2).
- ✅ **Phase 4** — alert/log integrity: populate confidence/indicators/sweep/confluence; generic CSV row; header migration; `--horizon-hours` (D, E1–E4).
- ✅ **Phase 5** — robustness: stablecoin blacklist, session health watchdog, richer `/status`, reuse the long-lived Telegram Bot.
- ✅ **Phase 6** — docs rewritten to reality (this file, README, Architecture, rules, PRD); tests added (zone, sweep, batch, retry, migration, populated-alert render, `--horizon-hours`); `schedule_check.py` fixed. **210 tests pass.**

## Price-action-first rebuild (Request B — structure decides, indicators confirm)
- ✅ **B1 Config + plumbing** — TF-role/structure/SR/liquidity/PA/trendline/MTF/futures/quality/risk constants; `scanner` OI history; frames retained through `run_scan`.
- ✅ **B2 Primary detectors** — `market_structure`, `support_resistance`, `liquidity`, `price_action`, `trendlines` (+ per-module tests).
- ✅ **B3 Context + scoring + gate** — `mtf`, `futures_context`, `setup_quality`, `risk_gate`; `scoring.py` repurposed to the secondary `indicator_confirmation`.
- ✅ **B4 Decision core** — `decision.decide()` wires the 9-layer hierarchy + NO_TRADE reasons + data warnings; the 3 filters became feature extractors; `test_decision.py` scenarios.
- ⏳ **B5 LLM = explanation only** — decision core owns every signal; `main` uses the local `explanation_fallback` today. Rewriting `ai_decision.py` to the explanation-only prompt/schema + wiring batched LLM prose is **deferred** (keeps the suite green).
- ✅ **B6 Alerts, log, backtest, docs** — 28-col CSV + migration; structured alert (market context primary + indicators secondary); `backtest.py --strategy` core replay (look-ahead-safe); `price_action_spec.md` + full doc refresh. **277 tests pass.**

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
migration (verify `signals_log.csv.vN.bak` + new 28-col header), `main.py --once`
for one live scan, then `sudo systemctl restart crypto-signal-bot`.
Rollback: `git checkout a62a08e && systemctl restart` — `.v1.bak` keeps history.
