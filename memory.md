# memory.md — Progress Tracker

## Current state
The bot now decides on a **deterministic price-action & market-context core**
(**[price_action_spec.md](price_action_spec.md)**, primary authority) on branch
`reconstruct/spec-alignment`. Market structure / S/R / liquidity / risk decide
LONG/SHORT/NO_TRADE; RSI/EMA/VWAP/Bollinger are **secondary confirmation only**.
The owner's handwritten note (**[strategy_spec.md](strategy_spec.md)**) is the
documented secondary layer. The LLM writes the **explanation** and audits the
verdict in the background — it never decides. **533 unit tests pass.**

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

## Credential + .env hardening (2026-09-02, after the owner pasted the live .env)
- 🔑 **The live Telegram token is in public Git history** — `tests/test_credentials.py`
  pasted it verbatim as a fixture (commit `18474b4`, on `main`). History cannot be
  un-leaked; **the token must be revoked at @BotFather.** The OpenRouter key seen in
  the chat is NOT in the repo (verified across all commits) but is pasted-in-chat
  compromised: rotate at openrouter.ai/api-keys. Its prefix is now in
  `_EXPOSED_OPENROUTER_KEYS`, so every boot warns until it is replaced.
- ✅ The fixture uses synthetic values derived from the config prefixes, and a new
  test scans every tracked file for a full-shaped secret so this cannot recur.
- ✅ `config.check_config_warnings()` (logged by main at startup as `CONFIG:`)
  catches the bug that explains the silent AI failures: `AI_BASE_URL` had been
  saved as a **markdown link** (`[https://openrouter.ai/api/v1](…)`) — requests
  then raised MissingSchema, `_post_once` treated it as transient, retried 3x, and
  each attempt consumed AI_DAILY_BUDGET. Also flags an empty AI_MODEL, a fallback
  equal to the primary, and a model with no key.
- ℹ️ Production model right now = whatever `.env` pins: `nvidia/nemotron-3-ultra-550b-a55b:free`
  at `openrouter.ai/api/v1` with fallback `z-ai/glm-5.2:free`; the repo default
  (`deepseek-v4-flash` @ AgentRouter, no fallback) applies only if those lines are
  removed. Check with `journalctl -u crypto-signal-bot | grep "Bot starting"`.

## Live-log hardening, part 2 (same session — the leftovers from the audit)
- ✅ **Owner gate fails closed** — `_is_owner` returned True whenever
  `TELEGRAM_CHAT_ID` was unset, so any stranger who found the bot could start a
  24/7 `/scan_on` session and spend the AI budget. The chat id is now an
  allow-list (comma-separated), an unset one authorises nobody, and a refusal says
  whether the cause is configuration. `tests/test_telegram_owner_gate.py` covers
  it (verified: fails when the guard is reverted).
- ✅ **One AI transport** — the endpoint is resolved per call
  (`chat_completions_url()`) instead of frozen at import, and the Telegram chat
  assistant now rides `ai_decision.complete_chat`: the browser-like UA the
  provider's WAF requires, the retry ladder, the fallback model, and — for the
  first time — budget accounting, because assistant traffic was spending the daily
  cap invisibly. `scripts/openrouter_diagnose.py` points at the live URL too.
- ✅ **Backtest honesty** — `--strategy` applies the live alert floor
  (`ALERT_QUALITY_MIN`) so it reports alerts the owner would have received rather
  than every decision (sub-floor ones counted as `logged_only`), its report lists
  what it cannot reproduce (funding, OI), and `evaluate_signal` derives a missing
  RR from the levels instead of defaulting it to 2.0 — which had been crediting
  rows with no RR as 2R wins.
- ✅ **`liquidation` nearby-SR block** read `zone["distance"]`, a key
  `support_resistance` never emits, so every entry was a permanent None. Distance
  is now derived from the zone mid against the traded price and the block is
  omitted entirely when there is no price to measure from.

## AI output contract (2026-09-02, after the .env fix — HTTP 200 but `ai_used=False`)
Restarting with a clean `AI_BASE_URL` made the provider reachable (`AI provider HTTP
status: 200`), yet every row still logged `ai_used=False`: `nvidia/nemotron-3-ultra`
answered with analysis prose, the extractor only stripped a fence at the very start
and sliced first-`[`-to-last-`]`, and the ladder replayed the identical doomed
request `AI_RETRY_MAX` times per model. `news_analysis.py` had already solved this on
the same gateway (`response_format` + a 400 retry); the decision batch path simply
never got it. Now: the batch prompt requires one object `{"decisions": [ … ]}` and
JSON mode is requested from the provider (learned-off on the first 400 naming the
field); `_extract_json`/`_extract_json_array` skip prose anywhere, honour quotes
while bracket-matching, and salvage a truncated block to its last complete element;
a parse failure is retried with a correction turn and, when `finish_reason=length`,
with a bigger `max_tokens` (new `AI_JSON_MODE`, `AI_MAX_TOKENS_RETRY_CAP`, both
`.env`-overridable); `parse_verdict` also unwraps a batch envelope, so a single-setup
answer in batch shape is understood instead of dropped. The audit outcome is one
`AI AUDIT` line per scan plus `AIOpinionWorker.status()`. The gates were NOT touched
(owner's instruction, still in force) and `ai_used` still means "a model verdict
replaced the deterministic one" — False on scheduled scans is correct.
`scripts/target_fix_compare.py` (new) answers the deploy question with a count:
`RISK_TARGET_SCAN_ZONES` off then on, `decision.decide` twice over identical
candles, reporting setups cleared by the reward side alone and asserting the risk
side (`stop_too_wide`/`no_structure_stop`) did not move. When candles cannot be
fetched it says "nothing measured" rather than printing zeroes that read like a
result (verified from this sandbox, where Binance refuses `exchangeInfo` at the
HTTP layer even though TCP 443 connects — so live numbers come from the server).
`scripts/why_no_signals.py` default path fixed (it resolved `/signals_log.csv` when
run from a copy; now config → cwd → known locations, plus `--log`). `.gitignore`
covers `signals_log*.csv*` / `ai_opinions.csv*` / `*.bak`, and a test asserts no
tracked file is a log or a rotation — `main` currently tracks `signals_log.csv.v1.bak`
(984 live rows), which the merge must drop with `git rm --cached`.

## Second live log (2026-09-02 12:07 **UTC** = 17:37 IST, PID 37708) — still zero alerts
Journalctl timestamps are UTC, so this scan is 40 minutes AFTER main's 16:19-16:57 IST
commits. The server's HEAD is `f7dcbda` — the aggressive floor loosening (`MIN_RR=1.2`,
30/35/25) — and NOT `94d7d9b`'s gate commenting, which is why the CSV rows still carry
`no_clear_target`/`poor_rr`/`no_structure_stop`. The scan still ended `signals 0 (holds 41)`.
Reading the last 41 rows of `signals_log.csv` (the reason codes, not the prose the
alert shows) shows why: the two removed codes were never the whole gate —
`stop_too_wide`, `target_too_close` and `into_opposing_zone` are still live, and
`insufficient_primary_evidence`/`low_setup_quality` are recorded by `decision.decide`,
not by the risk gate. Removing a gate cannot help while the layers beside it still
veto. The reward side is the part this branch repairs (zone scan, wrong-side
protection, `target_note`); the risk side (`stop_too_wide` from a `last_swing_high`
farther than 3 ATR) is untouched on both branches and is the next thing to measure —
`scripts/why_no_signals.py` prints how many alerts each layer would free, so the
decision is taken from the log rather than by feel.
Deployed-code tells in that same log, for the record: `exchange returned 28 rows
(< 300), skipping`, `funnel 41/41/41`, INFO-level `Signal logged: … HOLD`, and no
`CONFIG:`/`SECURITY:` lines — none of the branch's fixes were on the box; the server
was running `main`. Owner-side `.env` fix confirmed applied: `AI_BASE_URL=https://
openrouter.ai/api/v1` (it had held a markdown link, which failed every AI call and
burned `AI_DAILY_BUDGET` on retries).

## Merged into `main` (PR #2 merged 2026-09-02 13:29 UTC, `main` = `9a2b571`) — what the deployed build now is
Merged with `gh pr merge 2 --merge` and **no `--delete-branch`**; `main`'s tree is byte-identical
to this branch and the suite is 533 passed. Rollback reference for the server: `f7dcbda` was what
it last ran, `84b910b` was `main` before the merge. `origin/README-AGENT.md` is the only unmerged
branch left and carries no work; PR #1 closed unmerged as superseded.
`main` now equals this branch: the reachable-target gate, the restored
`no_clear_target`/`poor_rr`/`no_structure_stop` checks, the AI output contract
(JSON mode + tolerant extraction + correction/escalation retries), the audit-only
log lines, the security hygiene and the diagnostics. Kept from `main`'s own commits:
`AI_MAX_TOKENS=8000`, `AI_TIMEOUT_SECONDS=90` and the penalty-stacking floor
(`setup_quality` combines penalties as `max(0.5, min(...))` instead of multiplying
0.55×0.90×0.70 into a 65% haircut). Reverted to spec and made `.env`-tunable
(`_env_number`, with `check_config_warnings()` reporting any override outside the
band): the floors `f7dcbda` loosened by 10-35%, `IND_CONFIRM_BONUS_MAX 10` /
`IND_CONFLICT_PENALTY_MAX 15` (indicators confirm, they do not trigger) and both RR
quality penalties (main zeroed them *while* disabling the RR gate — RR enforced
nowhere, which is now a named startup warning). `signals_log.csv.v1.bak` (984 live
rows) is untracked (`git rm --cached`, file left on disk) and the ignore rules now
cover every rotation. The five Sep-02 tuning guides were kept and banner-corrected:
the server ran `f7dcbda` (16:28 IST) at the time of the 12:07/12:20 UTC logs, so the
gate-commenting commits never executed on it and the "signals restored 0→2" claim was
never reproduced by the deployed build.

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
