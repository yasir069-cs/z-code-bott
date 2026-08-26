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
import decision as decision_core
import scanner

log = logging.getLogger("backtest")

CandleFetcher = Callable[[str, datetime, datetime], Optional[pd.DataFrame]]

# decision.decide speaks LONG/SHORT/NO_TRADE; the fill simulator + CSV speak BUY/SELL.
_STRAT_SIGNAL_MAP = {"LONG": "BUY", "SHORT": "SELL"}

# Candle open-time -> its close offset, per timeframe. A candle at open time O on
# timeframe TF is only CLOSED (and therefore usable in a decision) once O+offset
# has elapsed — this is what keeps the multi-TF replay free of look-ahead.
_TF_DURATION = {"1h": pd.Timedelta(hours=1), "15m": pd.Timedelta(minutes=15),
                "5m": pd.Timedelta(minutes=5)}


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


def evaluate_signal(sig: dict, fetcher: CandleFetcher, horizon_hours: int = 24) -> dict:
    """Walk 5M candles after the signal; classify win/loss/open.

    *horizon_hours* bounds how far past the signal a TP/SL is allowed to fill;
    beyond it the trade is scored OPEN at the last close.
    """
    ts = datetime.fromisoformat(sig["timestamp"])  # ISO with tz offset (IST)
    ts_utc = ts.astimezone(timezone.utc)
    horizon = ts_utc + timedelta(hours=horizon_hours)
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


def _summarize(results: list[dict], holds: int, horizon_hours: int) -> dict:
    """Aggregate a list of evaluate_signal results into the report dict.

    Shared by the logged-signal replay and the strategy replay so both modes
    report win-rate / avg-RR / total-R identically.
    """
    wins = [r for r in results if r["outcome"] == "WIN"]
    losses = [r for r in results if r["outcome"] == "LOSS"]
    opens = [r for r in results if r["outcome"] == "OPEN"]
    nodata = [r for r in results if r["outcome"] == "NO_DATA"]
    closed = wins + losses
    return {
        "total_signals": len(results),
        "holds_logged": holds,
        "horizon_hours": horizon_hours,
        "wins": len(wins),
        "losses": len(losses),
        "open": len(opens),
        "no_data": len(nodata),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "avg_rr": (sum(r["RR"] for r in closed) / len(closed)) if closed else 0.0,
        "total_r": sum(r["r"] for r in results),
        "results": results,
    }


def run_backtest(log_path: Path, fetcher: CandleFetcher, horizon_hours: int = 24) -> dict:
    signals = [s for s in load_signals(log_path) if s.get("signal") in ("BUY", "SELL")]
    holds = sum(1 for s in load_signals(log_path) if s.get("signal") == "HOLD")
    results = [evaluate_signal(s, fetcher, horizon_hours) for s in signals]
    return _summarize(results, holds, horizon_hours)


# ── Strategy replay ────────────────────────────────────────────────────────
# Instead of scoring signals that were logged live, step the *decision core*
# (decision.decide) over historical frames bar-by-bar and score whatever it
# produces. Same code path as live, so win-rate/R here == what the live bot
# would have alerted. The slicing below is what keeps it look-ahead-free.

def _slice_frames(frames_full: dict, c5: pd.Timestamp) -> dict:
    """Slice every TF frame to candles CLOSED at/before *c5* (no look-ahead).

    The index is a candle's OPEN time; a candle only closes at open + its TF
    duration. Keeping rows where ``open + duration <= c5`` reproduces exactly
    the frame the live bot would have held the instant the 5M decision bar at
    close-time *c5* closed — an HTF candle whose hour has not finished is
    correctly excluded.
    """
    out = {}
    for tf, df in frames_full.items():
        dur = _TF_DURATION.get(tf, pd.Timedelta(0))
        out[tf] = df[df.index + dur <= c5]
    return out


def replay_strategy(symbol: str, frames_full: dict, horizon_hours: int = 24,
                    decide_fn=None) -> list[dict]:
    """Replay decision.decide over the 5M history and score each LONG/SHORT.

    For every 5M bar we hand the decider the multi-TF frames sliced to that
    bar's close (via :func:`_slice_frames`), then simulate the fill with the
    *same* :func:`evaluate_signal` used for logged signals — but reading only
    future 5M bars, so the outcome is look-ahead-free too. NO_TRADE bars are
    skipped, mirroring the live HOLD (silent). A per-coin cooldown mirrors the
    live duplicate guard so one setup is not re-emitted every 5 minutes.

    *decide_fn* defaults to the live :func:`decision.decide` (resolved at call
    time so it can be monkeypatched); pass a stub to test the harness alone.
    """
    decide = decide_fn or decision_core.decide
    entry_df = frames_full.get(config.TF_ENTRY)
    if entry_df is None or entry_df.empty:
        return []
    dur5 = _TF_DURATION[config.TF_ENTRY]

    def fill_fetcher(_sym, start, end):  # future 5M bars only (>= signal_ts + 5m)
        w = entry_df[(entry_df.index >= pd.Timestamp(start)) & (entry_df.index < pd.Timestamp(end))]
        return None if w.empty else w

    cooldown = timedelta(minutes=config.DUPLICATE_COOLDOWN_MIN)
    results: list[dict] = []
    last_emit: Optional[datetime] = None

    for open_ts in entry_df.index:
        emit_dt = open_ts.to_pydatetime()
        if last_emit is not None and emit_dt < last_emit + cooldown:
            continue
        frames = _slice_frames(frames_full, open_ts + dur5)
        d = decide(frames)
        word = _STRAT_SIGNAL_MAP.get(d.get("decision"))
        if word is None:  # NO_TRADE -> silent, exactly like a live HOLD
            continue
        sig = {
            "timestamp": open_ts.astimezone(config.TZ).isoformat(),
            "coin": symbol,
            "signal": word,
            "entry": d["entry"], "SL": d["sl"], "TP": d["tp"],
            "RR": d["rr"] if d.get("rr") else 2.0,
        }
        results.append(evaluate_signal(sig, fill_fetcher, horizon_hours))
        last_emit = emit_dt
    return results


def _fetch_tf_history(exchange, symbol: str, timeframe: str, limit: int,
                      now: Optional[datetime] = None) -> Optional[pd.DataFrame]:
    """Fetch the most recent *limit* candles for one timeframe, closed bars only."""
    batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not batch:
        return None
    df = pd.DataFrame(batch, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp").sort_index()
    # Drop a still-forming final candle so the frame matches the live invariant
    # (detectors only ever see closed candles).
    cutoff = pd.Timestamp(now or datetime.now(timezone.utc))
    dur = _TF_DURATION.get(timeframe, pd.Timedelta(0))
    return df[df.index + dur <= cutoff]


def run_strategy_backtest(symbol: str, exchange, horizon_hours: int = 24) -> dict:
    """Fetch 1H/15M/5M history for *symbol* and replay the live decision core."""
    frames_full = {
        config.TF_HTF:   _fetch_tf_history(exchange, symbol, config.TF_HTF, 300),
        config.TF_SETUP: _fetch_tf_history(exchange, symbol, config.TF_SETUP, 500),
        config.TF_ENTRY: _fetch_tf_history(exchange, symbol, config.TF_ENTRY, 1000),
    }
    missing = [tf for tf, df in frames_full.items() if df is None or df.empty]
    if missing:
        log.warning("strategy backtest: no candle data for %s on %s", missing, symbol)
        return _summarize([], 0, horizon_hours)
    results = replay_strategy(symbol, frames_full, horizon_hours)
    return _summarize(results, 0, horizon_hours)


def write_report(report: dict, out_path: Path) -> None:
    lines = [
        "=== Crypto Signal Bot — Backtest Report ===",
        f"Generated: {datetime.now(config.TZ).isoformat(timespec='seconds')}",
        f"Total BUY/SELL signals: {report['total_signals']}  (HOLD logged: {report['holds_logged']})",
        f"Fill horizon: {report.get('horizon_hours', 24)}h after each signal",
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
    parser.add_argument("--horizon-hours", type=int, default=24,
                        help="hours after each signal to allow a TP/SL fill before scoring it OPEN")
    parser.add_argument("--strategy", metavar="SYMBOL", default=None,
                        help="replay the live decision core (decision.decide) over recent 1H/15M/5M "
                             "history for SYMBOL (e.g. BTC/USDT:USDT) instead of scoring logged signals")
    parser.add_argument("--offline-csv", type=Path, default=None,
                        help="use a local CSV of 5m candles (timestamp_ms,open,high,low,close,volume) "
                             "for all coins instead of live Binance (safe DEMO mode)")
    args = parser.parse_args()

    config.setup_logging()

    if args.strategy:
        exchange = scanner.make_exchange()
        report = run_strategy_backtest(args.strategy, exchange, args.horizon_hours)
        write_report(report, config.BASE_DIR / "backtest_strategy_report.txt")
        print(f"[strategy {args.strategy}] signals={report['total_signals']} wins={report['wins']} "
              f"losses={report['losses']} open={report['open']} "
              f"win_rate={report['win_rate']:.1f}% total_r={report['total_r']:+.2f}R")
        return

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

    report = run_backtest(args.log, fetcher, args.horizon_hours)
    write_report(report, config.BASE_DIR / "backtest_report.txt")
    print(f"signals={report['total_signals']} wins={report['wins']} losses={report['losses']} "
          f"open={report['open']} win_rate={report['win_rate']:.1f}% total_r={report['total_r']:+.2f}R")


if __name__ == "__main__":
    main()
