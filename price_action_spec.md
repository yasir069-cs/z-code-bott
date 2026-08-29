# Price-Action & Market-Context Specification — PRIMARY AUTHORITY

> This file is the **primary source of truth** for *what decides a trade*.
>
> The bot decides on **market structure and market context first**; technical
> indicators (RSI, EMA21, VWAP, Bollinger, MACD) are **secondary confirmation
> only** and can never trigger or veto a trade on their own. The owner's
> handwritten indicator note lives in [`strategy_spec.md`](strategy_spec.md) and
> now documents that **secondary** layer.
>
> When code and this file disagree, **this file is right and the code is wrong**
> for the *decision hierarchy*. Concrete thresholds are named constants in
> [`config.py`](config.py); the values quoted here are current-as-written and
> the constant name is the authority if they ever drift.

## Design principle

> **`NO_TRADE` is a valid — and preferred — outcome whenever multi-factor
> confluence is insufficient. The system never forces a trade.**

A coin below VWAP can still go **LONG** if structure plus a reclaimed liquidity
sweep say so. A textbook indicator alignment printing into the underside of a
major resistance with poor reward returns **NO_TRADE**. Indicators inform; they
do not command.

## The decision hierarchy (highest priority first)

The deterministic core [`decision.decide()`](decision.py) evaluates evidence in
this order and returns `LONG`, `SHORT`, or `NO_TRADE` with full evidence and, for
`NO_TRADE`, the exact reasons.

| # | Layer | Module | Role |
|---|---|---|---|
| 1 | **Market structure** | `market_structure.py` | Fractal swings; HH/HL vs LH/LL; trend / range / consolidation; BOS; CHoCH; displacement + retest. *Primary direction.* |
| 2 | **Support / Resistance** | `support_resistance.py` | Auto horizontal **zones (ranges, not single prices)**; strength by touches / rejection wicks / volume; major vs minor; session H/L. |
| 3 | **Liquidity & sweeps** | `liquidity.py` | Equal highs/lows (stop pools). A long needs a **sell-side sweep + reclaim + bullish confirmation** (mirror for a short). **Post-sweep confirmation is mandatory** — a touch alone is not an entry. |
| 4 | **Price action & volume** | `price_action.py` | Rejection wicks, engulfing, displacement, failed breakout, breakout-retest, absorption. A **no-volume breakout is weak**. |
| 5 | **Trendlines / channels** | `trendlines.py` | Auto-fit from swings (min touches, valid slope), break + retest. **Confluence only — never standalone.** |
| 6 | **Multi-timeframe** *(mandatory)* | `mtf.py` | 1H bias → 15M setup → 5M entry. **Reject / reduce** when the entry TF opposes the higher-timeframe bias. |
| 7 | **Crypto-futures context** | `futures_context.py` | Open Interest + funding, read **with context** (price vs OI-change). **Safe-degrade** when data is missing — never fabricates values. |
| 8 | **Risk / Reward gate** *(mandatory)* | `risk_gate.py` | Structure-based invalidation/SL; target from the **nearest opposing zone**; configurable min R/R. `NO_TRADE` on poor R / wide stop / nearby opposing zone / wide spread. |
| 9 | **Technical indicators** *(secondary)* | `scoring.py` → `indicator_confirmation()` | RSI/EMA/VWAP/Bollinger fold into `setup_quality` as a **bounded** sub-score. They confirm; they cannot rescue a setup below the primary floor, nor gate a structurally valid one. |

## How the core combines them — [`decision.decide()`](decision.py)

```
frames = { "1h": HTF, "15m": setup, "5m": entry }   # config.TF_HTF / TF_SETUP / TF_ENTRY

1. Frame adequacy      each TF >= _MIN_FRAME (8) closed candles, else NO_TRADE("insufficient_data")
2. Structure x3 + MTF  market_structure.analyze per TF -> mtf.combine -> htf_bias, direction, counter_htf
   └─ direction is None (no HTF bias / range)          -> NO_TRADE("no_directional_bias")
3. Detectors           support_resistance / liquidity / price_action / trendlines / futures_context
4. Indicators (sec.)   scoring.indicator_confirmation(snap, direction) -> {score, agrees, notes}
5. Setup quality       setup_quality.score(...) -> primary (0-100) + bounded indicator term
   └─ primary < QUALITY_PRIMARY_FLOOR (45)              -> NO_TRADE("insufficient_primary_evidence")
   └─ quality  < QUALITY_MIN (55)                       -> NO_TRADE("low_setup_quality")
6. MTF alignment       MTF_REQUIRE_HTF_ALIGN and entry opposes HTF -> NO_TRADE("counter_htf")
7. Risk/reward gate     risk_gate.evaluate(...) -> structure SL, target off nearest opposing zone, RR
   └─ rr < MIN_RR (1.5) / stop > RISK_MAX_STOP_ATR (3.0)
      / opposing zone < RISK_MIN_TARGET_ATR (1.0) / spread > RISK_MAX_SPREAD_PCT -> NO_TRADE(reason)

decision = direction  iff  no reason accumulated,  else  NO_TRADE
```

The returned dict carries: `decision`, `direction`, `setup_quality`/`confidence`,
`htf_bias`, `mtf`, `structure`, `sr`, `liquidity`, `price_action`, `trendline`,
`futures`, `indicators` (secondary), `quality`, `risk`, `entry`, `sl`, `tp`, `rr`,
`no_trade_reasons`, and `data_warnings`.

### Setup-quality weights (primary-weighted; indicators bounded)

Primary evidence weights (`config.QUALITY_W_*`, sum = 100):

| Structure | S/R | Liquidity | Price action | MTF | Trendline | Futures |
|---|---|---|---|---|---|---|
| 25 | 20 | 20 | 15 | 10 | 5 | 5 |

Indicators enter only as a **bounded secondary term** on top of the primary
score and are floored out entirely below `QUALITY_PRIMARY_FLOOR` — they cannot
manufacture a signal the primary evidence does not support.

## Timeframe roles (reused 1H / 15M / 5M — configurable)

- **1H — `config.TF_HTF`:** higher-timeframe bias / market context.
- **15M — `config.TF_SETUP`:** structure, S/R, liquidity, trendline validation.
- **5M — `config.TF_ENTRY`:** price-action trigger; its **close is the entry**.

No 4H fetch — the three existing frames are retained through `run_scan` and fed
to the core, so there is no added fetch cost.

## Who decides vs who explains

- **The deterministic Python core decides.** Same rules run in **live and
  backtest** (`backtest.py --strategy` steps history bar-by-bar through the very
  same `decision.decide`, look-ahead-safe).
- **The LLM explains only.** It receives the finished decision and writes prose;
  a slow, failed, or disabled LLM is **cosmetic** and can never change or drop a
  signal. When the LLM is unavailable, [`fallback.py`](fallback.py)
  `explanation_fallback()` writes a deterministic local template and the alert is
  tagged *"explanation generated locally"* (not the legacy "AI unavailable").

## Crypto-futures data (contextual, safe-degrading)

Verified keyless via ccxt: Open-Interest history + funding rates are fetched
(`OI_FETCH_ENABLED`, `OI_HISTORY_TIMEFRAME=5m`, `OI_HISTORY_LIMIT=24`).
Market-wide liquidation clusters are **not** public keyless — the candle-based
liquidity sweep is the actionable proxy. Basis / long-short ratio are left as
config-flagged, safe-degrading slots. Missing data → `futures.available = False`
+ a `data_warnings` entry; the decision still returns.

## NO_TRADE reasons (exact strings)

`insufficient_data` · `no_directional_bias` · `counter_htf` ·
`insufficient_primary_evidence` · `low_setup_quality` · plus risk-gate reasons
(`rr_below_min`, `stop_too_wide`, `opposing_zone_too_near`, `spread_too_wide`, …).
A `NO_TRADE` maps to **HOLD** at the signal boundary: logged, silent — no alert,
no cooldown.

## Out of scope / invariants (unchanged)

**Signals only.** The bot never places a trade (`create_order`), holds no
exchange API keys, and uses only public market data. Existing risk &
position-sizing limits are respected, never bypassed. Look-ahead / future
leakage is disallowed everywhere: detectors see only **closed candles ≤ the
decision bar**.
