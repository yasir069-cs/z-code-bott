"""Why did the bot alert nothing? Read the answer out of the log instead of guessing.

Every decision the pipeline makes — signalled or not — is appended to
signals_log.csv with its `no_trade_reason` codes, so the record of "0 signals from
41 decisions" already says which layer did it. This script reads that file only:
no exchange calls, no bot interaction, no writes. It prints

  * one block per scan (rows grouped by the minute they were logged, so rows
    written by an older build are never averaged together with the newest run);
  * a histogram of reason codes, labelled with the layer that produced them;
  * for each layer, how many decisions would clear if that layer alone were
    repaired — the honest "fixing the target buys me N alerts" number;
  * the setup-quality / RR spread of the rows still blocked, so a floor is chosen
    from the data rather than from intuition.

Funding and duplicate rejections are absent by design: they stop a coin before
`decision.decide` runs, so nothing is logged for them (see the scan summary line).

Usage: python scripts/why_no_signals.py [--file signals_log.csv] [--scans N]
       [--all] [--fix target,stop] [--alert-min N]
"""
import argparse
import csv
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Which layer owns which NO_TRADE reason code. `decision.py` also records prose
# penalties; those land in `reason`, not here, and are ignored.
LAYERS = {
    "target (reward side)": ("no_clear_target", "target_too_close", "poor_rr"),
    "stop (risk side)": ("no_structure_stop", "stop_too_wide", "no_atr"),
    "position vs zone": ("into_opposing_zone", "excessive_spread"),
    "quality floors": ("insufficient_primary_evidence", "low_setup_quality"),
    "direction / htf": ("counter_htf", "directional_conflict", "llm_no_trade"),
    "data": ("invalid_data",),
}
_CODE_LAYER = {code: layer for layer, codes in LAYERS.items() for code in codes}


def layer_of(code: str) -> str:
    return _CODE_LAYER.get(code, "other")


def codes_of(row: dict) -> list[str]:
    """Reason codes recorded for one decision (empty list when it was accepted)."""
    raw = (row.get("no_trade_reason") or "").strip()
    if not raw:
        return []
    return [code.strip() for code in raw.split(",") if code.strip()]


def signalled(row: dict) -> bool:
    return (row.get("signal") or "").strip().upper() in {"BUY", "SELL", "LONG", "SHORT"}


def num(row: dict, key: str):
    try:
        return float(row.get(key) or "")
    except (TypeError, ValueError):
        return None


def log_candidates() -> list[Path]:
    """Where the log is looked for, in order.

    This script is usually run from a copy in /tmp on the live box, so the repo
    cannot be assumed to be `parents[1]` of the script — that resolved to
    `/signals_log.csv` and reported "nothing to analyse" next to a log with a
    thousand rows in it. The bot's own config is the authority when it can be
    imported; otherwise the obvious places are tried, and a missing file says
    where it looked.
    """
    found: list[Path] = []
    try:
        import config
        found.append(Path(config.SIGNALS_LOG_FILE))
    except Exception:  # config imports .env; a copy outside the repo has neither
        pass
    home = Path.home()
    for candidate in (Path.cwd() / "signals_log.csv", home / "z-code-bott" / "signals_log.csv",
                      Path("/home/ubuntu/z-code-bott/signals_log.csv")):
        if candidate not in found:
            found.append(candidate)
    return found


def resolve_log_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in log_candidates():
        if candidate.exists():
            return candidate
    return log_candidates()[0]


def load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: (v or "") for k, v in row.items()} for row in csv.DictReader(fh)]


def group_scans(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Rows sharing a logged minute belong to one scan; oldest group first."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = (row.get("timestamp") or "unknown").strip()[:16] or "unknown"
        groups.setdefault(key, []).append(row)
    return sorted(groups.items(), key=lambda kv: kv[0] == "unknown")


def simulate(rows: list[dict], fixed: set[str], alert_min: float | None) -> list[dict]:
    """Rows that would become signals if `fixed` layers' reasons disappeared.

    A row clears when the repaired layers explain every reason it recorded — and
    cover at least one of them, so a row that was never blocked by these layers
    (e.g. a HOLD logged without a reason) is not counted as a gain. When
    `alert_min` is given, the row must also already show enough recorded setup
    quality to reach the Telegram floor — and the quality shown is a LOWER bound:
    an unreachable target is itself a quality penalty, so repairing the reward
    side would raise it, not lower it.
    """
    cleared = []
    for row in rows:
        if signalled(row):
            continue
        recorded = set(codes_of(row))
        if not recorded or not (recorded & fixed) or recorded - fixed:
            continue
        quality = num(row, "setup_quality")
        if alert_min is not None and quality is not None and quality < alert_min:
            continue
        cleared.append(row)
    return cleared


def summarise(rows: list[dict], alert_min: float | None = None) -> dict:
    """Histogram + per-layer 'what would this buy' + floor diagnostics."""
    blocked = [row for row in rows if codes_of(row)]
    hist = Counter(code for row in blocked for code in codes_of(row))
    by_layer: Counter[str] = Counter()
    for code, count in hist.items():
        by_layer[layer_of(code)] += count

    layers = {}
    for layer, codes in LAYERS.items():
        owned = {code for code in hist if code in codes}
        if not owned:
            continue
        fixed = set(codes)
        solo = [row for row in blocked if set(codes_of(row)) <= fixed]
        cleared = simulate(rows, fixed, alert_min)
        layers[layer] = {
            "reasons": {code: hist[code] for code in sorted(owned, key=hist.get, reverse=True)},
            "blocked_only_by_this": len(solo),
            "would_clear": len(cleared),
            "coins": [row.get("coin") or row.get("signal_id") or "?" for row in cleared][:12],
        }

    qualities = [q for q in (num(row, "setup_quality") for row in blocked) if q is not None]
    rrs = [r for r in (num(row, "RR") for row in rows) if r is not None]
    return {
        "rows": len(rows),
        "signalled": sum(1 for row in rows if signalled(row)),
        "blocked": len(blocked),
        "no_reason_hold": sum(1 for row in rows
                              if not codes_of(row) and not signalled(row)),
        "histogram": dict(hist.most_common()),
        "by_layer": dict(by_layer.most_common()),
        "layers": layers,
        "quality": _spread(qualities),
        "rr": _spread(rrs),
    }


def _spread(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "min": ordered[0], "median": statistics.median(ordered),
            "max": ordered[-1], "p25": ordered[min(len(ordered) - 1, len(ordered) // 4)],
            "p75": ordered[min(len(ordered) - 1, 3 * len(ordered) // 4)]}


def _print_scan(key: str, rows: list[dict], alert_min: float | None,
                fix: set[str], fix_names: list[str] | None = None) -> None:
    out = summarise(rows, alert_min)
    print(f"\n=== scan {key}: {out['rows']} decisions | alerted {out['signalled']} "
          f"| blocked {out['blocked']}"
          + (f" | logged HOLD without a reason {out['no_reason_hold']}" if out['no_reason_hold'] else "")
          + " ===")
    if not out["blocked"]:
        print("  no blocked decisions here")
        return
    print("  reasons:")
    for layer, data in out["layers"].items():
        only = data["blocked_only_by_this"]
        clear = data["would_clear"]
        print(f"    {layer:<22} {out['by_layer'][layer]:>3} hits | "
              f"sole blocker on {only:>2} | clears {clear:>2} if repaired"
              + (f"  [{', '.join(data['coins'])}]" if data["coins"] else ""))
        for code, count in data["reasons"].items():
            print(f"        {code:<32} {count}")
    q, r = out["quality"], out["rr"]
    if q["n"]:
        line = (f"  setup_quality of blocked rows: n={q['n']} min={q['min']:.1f} "
                f"p25={q['p25']:.1f} median={q['median']:.1f} p75={q['p75']:.1f} max={q['max']:.1f}")
        if alert_min is not None:
            above = sum(1 for row in rows
                        if (num(row, "setup_quality") or 0) >= alert_min)
            line += f" | {above} already >= floor {alert_min:g}"
        print(line)
    if r["n"]:
        print(f"  RR where one was computed:        n={r['n']} min={r['min']:.2f} "
              f"p75={r['p75']:.2f} max={r['max']:.2f}")
    if fix:
        cleared = simulate(rows, fix, alert_min)
        label = "+".join(fix_names or sorted(fix))
        print(f"  with --fix {label}: {len(cleared)} would alert"
              + (f"  [{', '.join((row.get('coin') or '?') for row in cleared)}]" if cleared else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", "--file", dest="file", default=None,
                        help="signal log to read (default: the repo's signals_log.csv)")
    parser.add_argument("--scans", type=int, default=1, help="how many recent scans to report")
    parser.add_argument("--all", action="store_true", help="report every scan in the file")
    parser.add_argument("--fix", default="", help="comma list of layers to pretend were repaired: "
                        + ", ".join(name.split(" ")[0] for name in LAYERS))
    parser.add_argument("--alert-min", type=float, default=None,
                        help="Telegram alert floor (default: config.ALERT_QUALITY_MIN)")
    args = parser.parse_args()

    path = resolve_log_path(args.file)
    if not path.exists():
        print(f"no log at {path} — nothing to analyse\n"
              f"pass --log /path/to/signals_log.csv (looked in: "
              f"{', '.join(str(c) for c in log_candidates())})", file=sys.stderr)
        raise SystemExit(1)
    rows = load_rows(path)
    groups = group_scans(rows)
    if not args.all:
        groups = groups[-max(1, args.scans):]

    alert_min = args.alert_min
    if alert_min is None:
        import config
        alert_min = float(getattr(config, "ALERT_QUALITY_MIN", 0) or 0)

    fix: set[str] = set()
    fix_names: list[str] = []
    for token in (t.strip() for t in args.fix.split(",")):
        if not token:
            continue
        matches = [layer for layer in LAYERS if layer.split(" ")[0] == token or token in layer]
        if not matches:
            print(f"unknown --fix layer {token!r}; choose from "
                  f"{', '.join(l.split(' ')[0] for l in LAYERS)}", file=sys.stderr)
            raise SystemExit(2)
        for layer in matches:
            fix.update(LAYERS[layer])
            fix_names.append(layer.split(" ")[0])

    print(f"{path.name}: {len(rows)} rows total, reporting "
          f"{'all' if args.all else f'the last {len(groups)}'} scan(s) "
          f"(alert floor {alert_min:g})")
    for key, scan_rows in groups:
        _print_scan(key, scan_rows, alert_min, fix, fix_names)


if __name__ == "__main__":
    main()
