"""Live funnel check for Phases 4-6: run the 1H -> 15M -> 5M cascade on real
Binance data and print how many coins survive each stage.

Usage: python scripts/funnel_check.py [--max N] [--relaxed]
  --max N     only scan the top N coins by volume (default: all)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config

config.setup_logging()

import filter_15m
import filter_5m
import filter_1h
import scanner
from indicators import compute_indicators


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=0, help="scan only top N coins by volume")
    args = parser.parse_args()

    exchange = scanner.make_exchange()
    symbols = scanner.get_active_usdt_symbols(exchange)
    if args.max:
        symbols = dict(list(symbols.items())[: args.max])

    after_1h: list[tuple[str, str]] = []
    after_15m: list[tuple[str, str]] = []
    final: list[tuple[str, str]] = []
    sweeps_found = 0

    for symbol in symbols:
        df_1h = scanner.fetch_ohlcv(exchange, symbol, "1h")
        if df_1h is None:
            continue
        for direction in ("BUY", "SELL"):
            if filter_1h.detect_sweep(df_1h, direction) is not None:
                sweeps_found += 1
        ctx = filter_1h.analyze_1h(df_1h)
        if ctx is None:
            continue
        after_1h.append((symbol, ctx["direction"]))

        df_15m = scanner.fetch_ohlcv(exchange, symbol, "15m")
        if df_15m is None or filter_15m.confirm_15m(df_15m, ctx["direction"]) is None:
            continue
        after_15m.append((symbol, ctx["direction"]))

        df_5m = scanner.fetch_ohlcv(exchange, symbol, "5m")
        if df_5m is None or filter_5m.entry_5m(df_5m, ctx["direction"]) is None:
            continue
        final.append((symbol, ctx["direction"]))

    print(f"\nscanned          : {len(symbols)} coins (24h vol >= ${config.VOLUME_MIN_USDT:,})")
    print(f"recent sweeps    : {sweeps_found} (bullish+bearish, last 10 candles)")
    print(f"after 1H context : {len(after_1h)}  {after_1h}")
    print(f"after 15M conf.  : {len(after_15m)}  {after_15m}")
    print(f"final candidates : {len(final)}  {final}")


if __name__ == "__main__":
    main()
