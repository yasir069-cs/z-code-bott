# PRD.md — Crypto Signal Bot

## What to build
A signals-only bot that scans the **full Binance USDT-M futures** market every
5 minutes during the evening session (**18:00 – 23:00 IST**) and decides
**LONG / SHORT / NO_TRADE** with a **deterministic price-action & market-context
core** (market structure → S/R → liquidity → price action → trendlines → MTF →
futures context → risk gate; indicators only confirm). It reads top-down
(1H bias → 15M setup → 5M entry), sets entry/SL/TP/RR, and sends Telegram alerts.
An LLM (**AgentRouter / deepseek-v4-flash**) writes the **explanation only**, records
a background **opinion audit** it cannot act on, and can
never change or drop a decision. The decision hierarchy is defined in
**[price_action_spec.md](price_action_spec.md)** (primary authority); the owner's
handwritten indicator note **[strategy_spec.md](strategy_spec.md)** is the
secondary confirmation layer.

## Targeted users
Solo trader (Yasir) — personal use only. The owner can't watch the market all
day; the bot watches it and rings Telegram when a setup meets the bar.

## Core requirements
1. The bot must decide on **market structure & context first** (price_action_spec.md);
   indicators are secondary confirmation only.
2. Alerts must be **correct and on time** — never late, never bleed past a scan slot.
3. **`NO_TRADE` is valid and preferred** when confluence is thin — never force a trade.

## Core features
1. Full futures scan (all USDT-M pairs) every 5 minutes, 60 scans/session.
2. Stablecoin + leveraged-token exclusion; 24h volume ≥ $50M filter.
3. **Deterministic decision core** (`decision.decide`): market structure → S/R
   zones → liquidity & sweeps → price action & volume → trendlines → MTF
   (mandatory) → futures context → risk/reward gate (mandatory) → LONG/SHORT/NO_TRADE.
4. Liquidity as primary evidence: a long needs a **sell-side sweep + reclaim +
   mandatory confirmation** (mirror for a short); indicators (RSI/EMA/VWAP/BB) fold
   in only as a **bounded secondary** sub-score, never a gate.
5. Batched LLM **explanation** with retry, a secondary model, and a per-day budget;
   a local template writes the explanation when the LLM is unavailable.
6. Funding-rate-aware leverage + risk-based position size (2% of ACCOUNT_BALANCE).
7. Telegram alerts with a **Market Context (primary)** block (structure / HTF bias /
   target zone / liquidity), an **Indicators (secondary)** block, and trade levels.
   NO_TRADE → HOLD stays silent.
8. Duplicate guard (15 min cooldown per coin, reset at session end).
9. Append-only `signals_log.csv` (28 columns) with startup header migration.
10. Timeliness guarantees: single non-overlapping cron job, OHLCV cache,
    concurrent fetch, hard 240s per-scan deadline.
11. In-Telegram chat assistant + commands (`/status`, `/signals`, `/strategy`).

## Out of scope
- Auto trade execution (no `create_order`, no exchange API keys).
- Web dashboard.
- Multi-user.
- LLM reasoning mode (documented token-starvation issue; stays off).
