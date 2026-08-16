"""End-to-end DEMO of steps 5-8 (safe, signals only, never trades).

Uses REAL Binance candles + real indicators for BTC/USDT, forces the three
filter gates open (today's market may have no qualifying candidate), then
runs the real decision -> duplicate guard -> alert -> CSV log chain:

  * AI: no OPENROUTER_API_KEY -> real failure path -> fallback decision
    (SL = recent swing low, TP = 2x risk, alert tagged 'AI Unavailable').
  * If a key IS configured in .env, the real OpenRouter/Nemotron API is
    called instead.
  * Alert: formatted exactly as production (sent to Telegram when
    configured; otherwise printed + logged by alerts.py).
  * Logs to signals_log_demo.csv so the production log stays clean.
  * Runs the candidate twice to show the 20-minute duplicate guard.

Usage: python scripts/e2e_demo.py [SYMBOL]      (default BTC/USDT)
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

config.setup_logging()

# demo artifacts must not touch the production log
config.SIGNALS_LOG_FILE = config.BASE_DIR / "signals_log_demo.csv"

import alerts
import duplicate_guard
import filter_15m
import filter_1h
import filter_5m
import logger
import scanner
from indicators import compute_indicators
from main import _decide


def build_bundle(exchange, symbol: str) -> dict:
    """Real data + real indicators; filter gates forced open for the demo."""
    frames = scanner.fetch_all_timeframes(exchange, symbol)
    if frames is None:
        raise SystemExit(f"could not fetch clean 3-TF data for {symbol}")

    ind_1h = compute_indicators(frames["1h"])
    ind_15m = compute_indicators(frames["15m"])
    ind_5m = compute_indicators(frames["5m"])
    sweep = filter_1h.detect_sweep(frames["1h"], "BUY")
    if sweep is None:  # no live sweep right now -> craft a synthetic one for the demo
        swing = ind_1h["swing_low_20"]
        sweep = {"direction": "BUY", "age_candles": 2, "level": swing,
                 "wick": ind_1h["atr"] * 1.5, "body": ind_1h["atr"] * 0.5,
                 "wick_body_ratio": 3.0, "volume_ratio": 1.8,
                 "candle_low": swing - ind_1h["atr"], "candle_high": swing + ind_1h["atr"],
                 "timestamp": frames["1h"].index[-3], "synthetic": True}

    print(f"[demo] real {symbol} data: price={ind_5m['close']:.6g} rsi5m={ind_5m['rsi']:.1f} "
          f"rsi1h={ind_1h['rsi']:.1f} atr1h={ind_1h['atr']:.4g} "
          f"sweep_level={sweep['level']:.6g} (synthetic={sweep.get('synthetic', False)})")
    return {
        "symbol": symbol,
        "direction": "BUY",
        "current_price": ind_5m["close"],
        "entry_price": ind_5m["close"],
        "ind_1h": ind_1h, "sweep": sweep,
        "ind_15m": ind_15m, "confirm_score": 4,
        "ind_5m": ind_5m,
    }


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT"
    exchange = scanner.make_exchange()
    bundle = build_bundle(exchange, symbol)
    guard = duplicate_guard.DuplicateGuard()

    for run in (1, 2):  # second run must be swallowed by the duplicate guard
        now = datetime.now(config.TZ)
        if guard.is_duplicate(bundle["symbol"], now):
            print(f"[demo] run {run}: DUPLICATE within cooldown -> skipped silently "
                  f"(tracked={guard.tracked_count()})")
            continue
        decision = _decide(bundle)
        sig = {"coin": bundle["symbol"], "signal": decision["signal"],
               "entry": decision["entry"], "SL": decision["sl"], "TP": decision["tp"],
               "RR": decision["rr"], "reason": decision["reason"], "ai_used": decision["ai_used"]}
        print(f"\n[demo] run {run} decision: {sig['signal']} | ai_used={sig['ai_used']}")
        if sig["signal"] == "HOLD":
            logger.log_signal(sig)
            print("[demo] HOLD -> no alert, logged only")
        else:
            print("[demo] alert text:\n" + alerts.format_alert(sig).replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
            alerts.send_alert(sig)
            logger.log_signal(sig)
        guard.record(bundle["symbol"], now)

    print(f"\n[demo] demo log rows in {config.SIGNALS_LOG_FILE}:")
    print(config.SIGNALS_LOG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
