"""Phase 13 — Backtesting engine.

Reads signals_log.csv, matches each BUY/SELL with historical 5M candles
after the signal timestamp, and reports win rate, average RR, total signals
and cumulative P&L (in R multiples). HOLD rows are counted but not traded.

Outcome rules (conservative):
  * candle hits SL -> loss  (-1R)          [if a candle hits both, SL wins]
  * candle hits TP first -> win            (+RR of the signal)
  * neither within the horizon -> OPEN     (unrealized R at last close)

Usage:
    python backtest.py                        # uses signals_log.csv
    python backtest.py --log path/log.csv --horizon-hours 24
"""
import argparse
import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

import config
import scanner

log = logging.getLogger("backtest")

CandleFetcher = Callable[[str, datetime, datetime], Optional[pd.DataFrame]]


def _default_fetcher(exchange) -> CandleFetcher:
    def fetch(symbol: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        since_ms = int(start.timestamp() * 1000)
        rows: list[list] = []
        while True:
            batch = exchange.fetch_ohlcv(symbol, timeframe="5m", since=since_ms, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            since_ms = batch[-1][0] + 1
            if len(batch) < 1000 or rows[-1][0] >= end.timestamp() * 1000:
                break
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.drop_duplicates(subset="timestamp").set_index("timestamp")
        return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))]
    return fetch


def load_signals(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    with open(log_path, newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def evaluate_signal(sig: dict, fetcher: CandleFetcher) -> dict:
    """Walk 5M candles after the signal; classify win/loss/open."""
    ts = datetime.fromisoformat(sig["timestamp"])  # ISO with tz offset (IST)
    ts_utc = ts.astimezone(timezone.utc)
    horizon = ts_utc + timedelta(hours=24)
    df = fetcher(sig["coin"], ts_utc + timedelta(minutes=5), horizon)
    result = {"coin": sig["coin"], "signal": sig["signal"], "timestamp": sig["timestamp"],
              "entry": float(sig["entry"]), "SL": float(sig["SL"]), "TP": float(sig["TP"]),
              "RR": float(sig["RR"]) if sig["RR"] else 2.0, "outcome": "NO_DATA", "r": 0.0}
    if df is None or df.empty:
        return result

    entry, sl, tp = result["entry"], result["SL"], result["TP"]
    is_buy = sig["signal"] == "BUY"
    for _, candle in df.iterrows():
        hit_sl = candle["low"] <= sl if is_buy else candle["high"] >= sl
        hit_tp = candle["high"] >= tp if is_buy else candle["low"] <= tp
        if hit_sl:  # conservative: SL first when both hit in one candle
            result.update(outcome="LOSS", r=-1.0)
            return result
        if hit_tp:
            result.update(outcome="WIN", r=result["RR"])
            return result
    last_close = float(df["close"].iloc[-1])
    move = (last_close - entry) if is_buy else (entry - last_close)
    risk = abs(entry - sl) or 1e-9
    result.update(outcome="OPEN", r=round(move / risk, 3))
    return result


def run_backtest(log_path: Path, fetcher: CandleFetcher) -> dict:
    signals = [s for s in load_signals(log_path) if s.get("signal") in ("BUY", "SELL")]
    holds = sum(1 for s in load_signals(log_path) if s.get("signal") == "HOLD")
    results = [evaluate_signal(s, fetcher) for s in signals]

    wins = [r for r in results if r["outcome"] == "WIN"]
    losses = [r for r in results if r["outcome"] == "LOSS"]
    opens = [r for r in results if r["outcome"] == "OPEN"]
    nodata = [r for r in results if r["outcome"] == "NO_DATA"]
    closed = wins + losses
    return {
        "total_signals": len(results),
        "holds_logged": holds,
        "wins": len(wins),
        "losses": len(losses),
        "open": len(opens),
        "no_data": len(nodata),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "avg_rr": (sum(r["RR"] for r in closed) / len(closed)) if closed else 0.0,
        "total_r": sum(r["r"] for r in results),
        "results": results,
    }


def write_report(report: dict, out_path: Path) -> None:
    lines = [
        "=== Crypto Signal Bot — Backtest Report ===",
        f"Generated: {datetime.now(config.TZ).isoformat(timespec='seconds')}",
        f"Total BUY/SELL signals: {report['total_signals']}  (HOLD logged: {report['holds_logged']})",
        f"Wins: {report['wins']}   Losses: {report['losses']}   Open: {report['open']}   No data: {report['no_data']}",
        f"Win rate (closed): {report['win_rate']:.1f}%",
        f"Avg RR (closed):   {report['avg_rr']:.2f}",
        f"Total P&L:         {report['total_r']:+.2f} R",
        "",
        f"{'timestamp':25s} {'coin':14s} {'sig':4s} {'entry':>12s} {'SL':>12s} {'TP':>12s} {'RR':>5s} {'outcome':8s} {'R':>7s}",
    ]
    for r in report["results"]:
        lines.append(f"{r['timestamp']:25s} {r['coin']:14s} {r['signal']:4s} "
                     f"{r['entry']:12.6g} {r['SL']:12.6g} {r['TP']:12.6g} {r['RR']:5.2f} "
                     f"{r['outcome']:8s} {r['r']:+7.2f}")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Backtest report written to %s", out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest signals_log.csv against Binance history")
    parser.add_argument("--log", type=Path, default=config.SIGNALS_LOG_FILE)
    parser.add_argument("--horizon-hours", type=int, default=24)  # informational
    parser.add_argument("--offline-csv", type=Path, default=None,
                        help="use a local CSV of 5m candles (timestamp_ms,open,high,low,close,volume) "
                             "for all coins instead of live Binance (safe DEMO mode)")
    args = parser.parse_args()

    config.setup_logging()
    if args.offline_csv:
        # Support CSVs with or without a 'symbol' column
        raw = pd.read_csv(args.offline_csv)
        cols = [c.lower().strip() for c in raw.columns]
        has_symbol = "symbol" in cols
        if has_symbol:
            frame = pd.read_csv(args.offline_csv)
            frame.columns = [c.lower().strip() for c in frame.columns]
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
            frame = frame.set_index("timestamp")
        else:
            frame = pd.read_csv(args.offline_csv,
                                names=["timestamp", "open", "high", "low", "close", "volume"])
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
            frame = frame.set_index("timestamp")
            log.warning("Offline CSV has no 'symbol' column — all coins will use the same candle data")

        def fetcher(symbol, start, end):
            if has_symbol:
                coin_data = frame[frame["symbol"] == symbol]
            else:
                coin_data = frame
            window = coin_data[(coin_data.index >= pd.Timestamp(start)) & (coin_data.index < pd.Timestamp(end))]
            return None if window.empty else window
    else:
        exchange = scanner.make_exchange()
        fetcher = _default_fetcher(exchange)

    report = run_backtest(args.log, fetcher)
    write_report(report, config.BASE_DIR / "backtest_report.txt")
    print(f"signals={report['total_signals']} wins={report['wins']} losses={report['losses']} "
          f"open={report['open']} win_rate={report['win_rate']:.1f}% total_r={report['total_r']:+.2f}R")


if __name__ == "__main__":
    main()
