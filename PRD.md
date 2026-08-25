# PRD.md — Crypto Signal Bot

## What to build
A signals-only bot that scans the **full Binance USDT-M futures** market every
5 minutes during the evening session (**18:00 – 23:00 IST**), scores each coin
top-down (1H → 15M → 5M) with a **graded confluence model**, uses an LLM
(**OpenRouter / NVIDIA Nemotron**) for the final BUY/SELL/HOLD + SL/TP/RR, and
sends Telegram alerts. The entry strategy is defined by the owner's handwritten
note, transcribed in **[strategy_spec.md](strategy_spec.md)** (authoritative).

## Targeted users
Solo trader (Yasir) — personal use only. The owner can't watch the market all
day; the bot watches it and rings Telegram when a setup meets the bar.

## Core requirements
1. The bot must follow the handwritten note (strategy_spec.md).
2. Alerts must be **correct and on time** — never late, never bleed past a scan slot.
3. The data → LLM combination must be strong and reliable.

## Core features
1. Full futures scan (all USDT-M pairs) every 5 minutes, 60 scans/session.
2. Stablecoin + leveraged-token exclusion; 24h volume ≥ $50M filter.
3. Top-down graded scoring: 1H context → 15M confirmation → 5M entry, weighted
   0.40 / 0.30 / 0.30, with EMA21 & VWAP as hard gates.
4. Graded zone ("bottom → in-between") and age-decayed liquidation-sweep scoring;
   sweep heavily weighted and labelled, not a hard gate.
5. Batched LLM decision with retry, a secondary model, and a per-day budget;
   Python fallback when the AI is unavailable.
6. Funding-rate-aware leverage + risk-based position size (2% of ACCOUNT_BALANCE).
7. Telegram alerts with confidence, confluence breakdown, indicator block, sweep
   label, and trade levels. HOLD stays silent.
8. Duplicate guard (15 min cooldown per coin, reset at session end).
9. Append-only `signals_log.csv` (20 columns) with startup header migration.
10. Timeliness guarantees: single non-overlapping cron job, OHLCV cache,
    concurrent fetch, hard 240s per-scan deadline.
11. In-Telegram chat assistant + commands (`/status`, `/signals`, `/strategy`).

## Out of scope
- Auto trade execution (no `create_order`, no exchange API keys).
- Web dashboard.
- Multi-user.
- LLM reasoning mode (documented token-starvation issue; stays off).
