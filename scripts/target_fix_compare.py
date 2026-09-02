"""Same candles, two gate settings — what does the reachable-target scan actually buy?

`RISK_TARGET_SCAN_ZONES` is the single knob that separates this branch from the
behaviour that filled the live log with `no_clear_target` and `RR below MIN_RR`:
the target used to be read from the *nearest* opposing zone only, and that zone is
usually a wall the price is sitting inside, not a target. The question the owner has
to answer — deploy, or keep tuning floors — needs a count rather than an argument,
so this script runs the real decision core TWICE over identical candles (the market
cannot move between the two runs, and no verdict is invented for the sake of a
number) and prints exactly what changes:

  * which setups blocked *only* by the reward-side measurement now clear;
  * which stay blocked, and by what;
  * the risk side (`stop_too_wide`, `no_structure_stop`) counts before and after —
    they must be identical, because nothing here touches the stop.

Read-only by construction: public candles in, `decision.decide` out. It never
alerts, never writes signals_log.csv, never trades, and never changes a config file
(the flag is set in this process only, and restored).

Usage:
  python scripts/target_fix_compare.py                      # coins from the newest scan in the log
  python scripts/target_fix_compare.py --symbols BTC/USDT:USDT ETH/USDT:USDT
  python scripts/target_fix_compare.py --max 25 --json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The reason codes that describe the REWARD side of the gate. `stop_too_wide` and
# `no_structure_stop` are deliberately NOT here: they belong to the risk side, and
# the whole point of the comparison is to show they do not move.
TARGET_CODES = frozenset({"no_clear_target", "target_too_close", "poor_rr"})
RISK_CODES = frozenset({"stop_too_wide", "no_structure_stop", "no_atr"})


def codes(decision: dict) -> list[str]:
    return [code for code in (decision.get("no_trade_reasons") or []) if code]


def pair_from(symbol: str, before: dict, after: dict) -> dict:
    """One coin's before/after row, from two `decision.decide` results."""
    b_codes, a_codes = set(codes(before)), set(codes(after))
    return {
        "symbol": symbol,
        "decision_before": before.get("decision"),
        "decision_after": after.get("decision"),
        "codes_before": sorted(b_codes),
        "codes_after": sorted(a_codes),
        "quality_before": before.get("setup_quality"),
        "quality_after": after.get("setup_quality"),
        "rr_before": before.get("rr"),
        "rr_after": after.get("rr"),
        "tp_before": before.get("tp"),
        "tp_after": after.get("tp"),
        "cleared_only_by_target": bool(b_codes) and not a_codes and b_codes <= TARGET_CODES,
        "newly_blocked": bool(a_codes - b_codes - TARGET_CODES) and not b_codes,
    }


def summarise(pairs: list[dict]) -> dict:
    """Counts the owner decides on: gained, unchanged, and what still blocks."""
    still = {}
    for pair in pairs:
        for code in pair["codes_after"]:
            still[code] = still.get(code, 0) + 1
    before = {}
    for pair in pairs:
        for code in pair["codes_before"]:
            before[code] = before.get(code, 0) + 1
    alerted_before = sum(1 for p in pairs if p["decision_before"] in ("LONG", "SHORT"))
    alerted_after = sum(1 for p in pairs if p["decision_after"] in ("LONG", "SHORT"))
    return {
        "coins": len(pairs),
        "verdicts_before": {"LONG": sum(1 for p in pairs if p["decision_before"] == "LONG"),
                            "SHORT": sum(1 for p in pairs if p["decision_before"] == "SHORT"),
                            "NO_TRADE": sum(1 for p in pairs
                                           if p["decision_before"] == "NO_TRADE")},
        "verdicts_after": {"LONG": sum(1 for p in pairs if p["decision_after"] == "LONG"),
                           "SHORT": sum(1 for p in pairs if p["decision_after"] == "SHORT"),
                           "NO_TRADE": sum(1 for p in pairs if p["decision_after"] == "NO_TRADE")},
        "cleared_only_by_target": sum(1 for p in pairs if p["cleared_only_by_target"]),
        "newly_blocked": sum(1 for p in pairs if p["newly_blocked"]),
        "codes_before": before,
        "codes_after": still,
        "alerts_emitted_before": alerted_before,
        "alerts_emitted_after": alerted_after,
        "risk_side_identical": all(before.get(code, 0) == still.get(code, 0)
                                   for code in RISK_CODES),
    }


def decide_both(decision_core, frames: dict, symbol: str, funding_rate=None,
                oi_df=None) -> tuple[dict, dict]:
    """The same `decide()` call with the gate's target scan off, then on.

    The flag lives on `config` because `risk_gate` reads it from the config module
    at call time; it is restored in a `finally` so a crash cannot leave this
    process evaluating a policy it did not ask for.
    """
    import config
    saved = getattr(config, "RISK_TARGET_SCAN_ZONES", True)
    try:
        config.RISK_TARGET_SCAN_ZONES = False
        before = decision_core.decide(frames, funding_rate=funding_rate, oi_df=oi_df,
                                      symbol=symbol)
        config.RISK_TARGET_SCAN_ZONES = True
        after = decision_core.decide(frames, funding_rate=funding_rate, oi_df=oi_df,
                                     symbol=symbol)
    finally:
        config.RISK_TARGET_SCAN_ZONES = saved
    return before, after


def symbols_from_log(path: Path, max_symbols: int) -> list[str]:
    """The coins of the newest scan in signals_log.csv — the setups that actually
    got rejected, not a convenient sample."""
    try:
        import why_no_signals
        rows = why_no_signals.load_rows(path)
    except (OSError, ImportError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return []
    groups = why_no_signals.group_scans(rows)
    if not groups:
        return []
    _, scan_rows = groups[-1]
    out = []
    for row in scan_rows:
        coin = (row.get("coin") or "").strip()
        if coin and coin not in out:
            out.append(coin)
    return out[:max_symbols] if max_symbols else out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", nargs="*", default=[], help="explicit symbols to compare")
    parser.add_argument("--log", default=None, help="signal log to take the last scan's coins from")
    parser.add_argument("--max", type=int, default=0, help="cap the number of coins (0 = all)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    config_path = None
    if args.log:
        config_path = Path(args.log).expanduser()
    symbols = list(args.symbols)
    if not symbols:
        import config
        path = config_path or Path(config.SIGNALS_LOG_FILE)
        symbols = symbols_from_log(path, args.max)
        if not symbols:
            print(f"no coins found in {path} — pass --symbols SYMBOL [SYMBOL …]",
                  file=sys.stderr)
            raise SystemExit(1)
    if args.max and len(symbols) > args.max:
        symbols = symbols[: args.max]

    import config                                   # noqa: F401  (risk_gate reads it live)
    import decision as decision_core
    import scanner

    exchange = scanner.make_exchange()
    pairs: list[dict] = []
    skipped: list[str] = []
    for symbol in symbols:
        frames = {}
        for tf in (config.TF_HTF, config.TF_SETUP, config.TF_ENTRY):
            frame = scanner.fetch_ohlcv(exchange, symbol, tf)
            if frame is None:
                skipped.append(symbol)
                break
            frames[tf] = frame
        else:
            before, after = decide_both(decision_core, frames, symbol)
            pairs.append(pair_from(symbol, before, after))

    summary = summarise(pairs)
    summary["skipped_no_data"] = skipped
    if args.json:
        print(json.dumps({"summary": summary, "pairs": pairs}, indent=2, default=str))
        return
    if skipped:
        # A coin that could not be fetched is not evidence about the gate. Saying so
        # is the difference between "the fix changed nothing" and "nothing was measured".
        print(f"note: {len(skipped)} coin(s) skipped, no candles returned: "
              f"{', '.join(skipped[:8])}{' …' if len(skipped) > 8 else ''}")

    print(f"target-scan comparison on {summary['coins']} coin(s) — same candles, "
          f"gate flag off vs on")
    if not pairs:
        print("  nothing measured — the comparison says nothing about the fix")
        return
    print(f"  verdicts  before: LONG {summary['verdicts_before']['LONG']} "
          f"SHORT {summary['verdicts_before']['SHORT']} "
          f"NO_TRADE {summary['verdicts_before']['NO_TRADE']}")
    print(f"  verdicts   after: LONG {summary['verdicts_after']['LONG']} "
          f"SHORT {summary['verdicts_after']['SHORT']} "
          f"NO_TRADE {summary['verdicts_after']['NO_TRADE']}")
    print(f"  setups cleared by the target fix alone: {summary['cleared_only_by_target']}")
    print(f"  setups this branch would newly block:   {summary['newly_blocked']}")
    print(f"  risk side unchanged (stop_too_wide etc.): {summary['risk_side_identical']}")
    print("  reason counts before -> after:")
    for code in sorted(set(summary["codes_before"]) | set(summary["codes_after"])):
        b, a = summary["codes_before"].get(code, 0), summary["codes_after"].get(code, 0)
        tag = " (risk side — must not move)" if code in RISK_CODES and b == a else ""
        print(f"    {code:<32} {b:>3} -> {a:>3}{tag}")
    print()
    for pair in pairs:
        if pair["codes_before"] == pair["codes_after"]:
            continue
        rr = (f"rr {pair['rr_before']} -> {pair['rr_after']}" if pair["rr_before"] != pair["rr_after"]
              else f"rr {pair['rr_after']}")
        print(f"  {pair['symbol']:<22} {'/'.join(pair['codes_before']) or '—':<48} -> "
              f"{'/'.join(pair['codes_after']) or 'clear'}  [{rr}] "
              f"{pair['decision_before']}->{pair['decision_after']}")
    print("\nNote: alert *delivery* also needs ALERT_QUALITY_MIN; this reports the "
          "gate, which is the part the fix changes.")


if __name__ == "__main__":
    main()
