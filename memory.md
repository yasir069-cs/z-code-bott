# memory.md — Progress Tracker

## Current state
The bot now decides on a **deterministic price-action & market-context core**
(**[price_action_spec.md](price_action_spec.md)**, primary authority) on branch
`reconstruct/spec-alignment`. Market structure / S/R / liquidity / risk decide
LONG/SHORT/NO_TRADE; RSI/EMA/VWAP/Bollinger are **secondary confirmation only**.
The owner's handwritten note (**[strategy_spec.md](strategy_spec.md)**) is the
documented secondary layer. The LLM writes the **explanation** and audits the
verdict in the background — it never decides. **456 unit tests pass.**

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
- **AI:** AgentRouter (`https://agentrouter.org/v1`) — `deepseek-v4-flash`, batched + retried,
  2000 tokens, reasoning off, 50/day budget — **explanation only, never decides**
- **Fallback:** local explanation template when the LLM is unavailable (the decision
  is never faked); footer "explanation generated locally"
- **Duplicate:** **15 min** alert cooldown per coin + **30 min** HOLD-log window per
  coin+reason (both reset at session end)
- **Sizing:** funding-rate-aware leverage + 2%-risk position size
- **Logs:** `signals_log.csv`, 28 columns, append-only, startup header migration;
  `ai_opinions.csv` records the background AI audit with an `agreement` grade

## Environment
Python 3.12.10 · pandas-ta 0.4.71b0 · pandas 3.0.5 · numpy 2.2.6 · ccxt 4.5.73 ·
AgentRouter (`deepseek-v4-flash`). `bbands(ddof=0)` (population std, TradingView). 50 strategy
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

## Live-log hardening (2026-09-02, from `journalctl -u crypto-signal-bot`)
- ✅ **risk_gate target scan** — the target came off `sr["nearest_support/resistance"]`
  only, so a zone price was already inside (mid below entry, top above it) killed the
  setup with "no achievable target" even when a usable level sat 5 ATR deeper:
  21/37 decisions in one session died that way and 11 more on RR computed against a
  padded level inside the same zone. Zones are now scanned nearest-first
  (`RISK_TARGET_SCAN_ZONES`) and a level may never sit on the wrong side of the
  entry. The old nearest-only behaviour is exactly what the flag at False gives.
- ✅ **Thin history served, not dropped** — `fetch_ohlcv` required the full
  300-candle window, so freshly listed perps (MARSCOIN 26 rows, 牛来 72) were
  discarded before the strategy saw them. Now `FRAME_MIN_CANDLES` (40) is the floor
  and a short frame logs one INFO note about reduced warm-up.
- ✅ **HOLD noise** — `signals_log.csv` grew ~2.2k HOLD rows/session and the INFO
  journal drowned in `Signal logged: … HOLD`. Deduped by verdict+reason per coin
  (`HOLD_LOG_COOLDOWN_MIN`), logged at DEBUG; the alert cooldown is untouched.
- ✅ **Persist isolation** — one `OSError` mid-persist used to abort the remaining
  coins' alerts; `_persist` now contains it and counts `log_failed`.
- ✅ **AI honesty** — `budget_exhausted_notice()` had no caller (the audit stage
  went silent mid-session and nobody was told); `ai_opinions.csv` gained
  `agreement` + header-drift archiving; the prompt's mislabelled sweep flags and
  the dropped 1H `feat` are fixed so the audit measures the real setup.
- ✅ **Scan summary** — dropped the duplicated `pass_5m` and the meaningless
  15M/5M funnel (they no longer gate anything), removed the dead `ai_used` /
  `llm_vetoed` counters that the scheduled path never set.
- ✅ **Docs** — README/Architecture/rules/memory said "OpenRouter/Nemotron" and
  "chunked at 12"; config/main docstrings described an LLM *decision* stage that the
  scheduled scan never runs. Both now describe the audit stage that exists.

## Interpretation decisions (documented)
- Sweep is heavily weighted, not a hard gate — owner's decision; requiring it
  produced whole sessions with zero alerts. No-sweep setups are labelled and
  confidence-capped at 69.
- Zone "in-between" extends to `range_pos` 0.60 (midpoint); beyond → reject.
- Volume "increase" graded: `> previous` full, `> 20-avg` partial.
- Duplicate guard (15 min) applies to alerted BUY/SELL only — a HOLD must never
  mute a later signal on the same coin. HOLD rows are instead deduped separately by
  verdict+reason within `HOLD_LOG_COOLDOWN_MIN` (30 min) and logged at DEBUG, so an
  always-on `/scan_on` session stops re-appending 37 identical rejections every
  5 minutes while the CSV keeps one row per setup state.
- Warm-up fetch (250 candles) is a data-accuracy addition; the strategy only ever
  looks at the last 50 candles per timeframe.
- Full rationale for each deviation lives in strategy_spec.md's deviations table.

## Deploy (off-session, before 18:00 IST)
Commit the server's 4 tuned config values, `git pull` the branch, run the header
migration (verify `signals_log.csv.vN.bak` + new 28-col header), `main.py --once`
for one live scan, then `sudo systemctl restart crypto-signal-bot`.
Rollback: `git checkout a62a08e && systemctl restart` — `.v1.bak` keeps history.
