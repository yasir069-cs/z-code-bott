# Crypto Signal Bot

Signals-only crypto market intelligence bot. Scans the full Binance USDT
market every 5 minutes during the NY session (6:30 PM – 9:30 PM IST),
filters coins top-down (1H context → 15M confirmation → 5M entry) with a
liquidation-sweep requirement, asks OpenRouter (NVIDIA Nemotron 3 Ultra) for the final BUY/SELL/HOLD
plus SL/TP/RR, alerts on Telegram, and logs every signal to CSV.

**This bot never trades.** No Binance API keys, no order endpoints —
public market data only.

## Install & run

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env            # fill in TELEGRAM_TOKEN / TELEGRAM_CHAT_ID /
                                  # ANTHROPIC_API_KEY (all optional for testing)

python main.py                    # production: APScheduler, 18:30–21:30 IST
python main.py --once             # DEMO/TEST: one full scan cycle right now
```

Without keys the bot still runs: AI failures take the documented
Python fallback (alerts tagged "AI Unavailable - Indicator based signal")
and Telegram alerts are logged instead of sent.

AI provider: OpenRouter, model `nvidia/nemotron-3-ultra-550b-a55b:free`,
300-token answers, JSON-only. Reasoning mode is disabled on purpose: with
the 300-token cap the model spends the whole budget thinking and never
emits the JSON (`finish_reason=length`); verified in
`scripts/openrouter_diagnose.py` (Tests A-D).

## DEMO / TEST modes (safe, no trading)

| Command | What it does |
|---|---|
| `python main.py --once` | One real production scan cycle on live data |
| `python scripts/e2e_demo.py [SYMBOL]` | Forces the filter gates open on real candles and runs decision → guard → alert → CSV log end-to-end (writes `signals_log_demo.csv`, not the production log) |
| `python scripts/funnel_check.py` | Live funnel stats: coins → sweeps → 1H → 15M → 5M |
| `python scripts/schedule_check.py` | Prints all scheduler fire times for the next 24h |
| `python backtest.py` | Backtests `signals_log.csv` against real Binance history → `backtest_report.txt` |
| `python -m pytest tests/ -q` | Full unit test suite (no network needed) |

## Required `.env` variables

```
TELEGRAM_TOKEN=<from @BotFather>       # optional; alerts logged if missing
TELEGRAM_CHAT_ID=<your chat id>        # optional
OPENROUTER_API_KEY=<from openrouter.ai>  # optional; fallback used if missing
AI_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free   # optional override
LOG_LEVEL=INFO                         # optional
```

## Pipeline (every 5 minutes, 36 scans/session)

```
ALL USDT PAIRS (fetch_tickers, dynamic)
→ 24h volume ≥ $5M
→ 1H context: bottom/top 30% zone + RSI 50→70 up / 35→50 down + EMA21 + VWAP
  + volume rising + near BB + valid liquidation sweep (all 7)   fail → skip coin
→ 15M confirmation: 4/5 conditions                              fail → reject
→ 5M entry: RSI range + RSI trend (higher lows / lower highs) + EMA21 + VWAP
  + volume + BB bounce/rejection (all)                          fail → reject
→ OpenRouter/Nemotron (only now): BUY/SELL/HOLD + entry/SL/TP/RR + reason  fail → Python fallback
→ duplicate guard (20 min per coin, reset 21:30 IST)
→ Telegram alert (BUY/SELL only — HOLD stays silent)
→ signals_log.csv (append-only: every BUY/SELL/HOLD)
```

Liquidation sweep (all 4 required): wick pierces the 20-candle swing low/high,
body closes back beyond it, wick > 2× body, volume > 1.5× 20-candle average.

## Example Telegram alert

```
🟢 BUY SIGNAL — BTC/USDT
Entry: 63058.5
SL: 62946.6
TP: 63282.3
RR: 1:2.00
Reason: Sweep below 62946 reclaimed; 5M RSI 50→55→51→56 higher low; volume rising
🤖 AI: nvidia/nemotron-3-ultra-550b-a55b:free
```

Fallback alerts replace the last line with:
`⚠️ AI Unavailable - Indicator based signal`

## Files

Exactly the Architecture.md layout: `main.py` (entry + scheduler), `config.py`,
`scanner.py`, `indicators.py`, `filter_1h.py` (+ sweep detection),
`filter_15m.py`, `filter_5m.py`, `ai_decision.py`, `fallback.py`,
`duplicate_guard.py`, `alerts.py`, `logger.py`, `backtest.py`,
`requirements.txt`, `.env`. Plus `tests/` (77 tests) and `scripts/`
(phase/demo checks). Spec docs (PRD/Architecture/rules/phases/memory) live
in the repo root; `memory.md` is the living progress tracker.

## Accuracy notes

- pandas-ta does ALL indicator math; `bbands(ddof=0)` matches TradingView's
  population-std bands.
- Each timeframe fetches 50 strategy candles + 250 warm-up candles so the
  Wilder RSI/EMA recursions converge to TradingView values (verified to
  <1e-6 against reference implementations in `tests/test_indicators.py`).
- Only closed candles are used — the forming candle is dropped at fetch time.

## Safety

Signals only. No `create_order` anywhere, no Binance keys (public mode),
no web dashboard, no multi-user. The scheduler makes zero market/AI API
calls outside 18:30–21:30 IST.
