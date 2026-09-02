"""Tests for scripts/why_no_signals.py — the offline "why 0 signals?" report.

The script is the tool the owner uses to turn `no_trade_reason` rows into a floor
decision, so its arithmetic has to be exact: which layer blocked a row, whether a
row was already alerted, and how many rows a repaired layer would actually free.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import why_no_signals as wns  # noqa: E402


def row(coin="BTC/USDT:USDT", ts="2026-09-02 12:07:01", signal="HOLD",
        reason="", quality="40", rr=""):
    return {"timestamp": ts, "signal_id": f"id-{coin}", "coin": coin, "signal": signal,
            "entry": "", "SL": "", "TP": "", "RR": rr, "setup_quality": quality,
            "no_trade_reason": reason}


def test_codes_are_split_and_attributed_to_a_layer():
    assert wns.codes_of(row(reason="no_clear_target, stop_too_wide")) == \
        ["no_clear_target", "stop_too_wide"]
    assert wns.codes_of(row(reason="")) == []
    assert wns.layer_of("poor_rr").startswith("target")
    assert wns.layer_of("stop_too_wide").startswith("stop")
    assert wns.layer_of("something_new") == "other"


def test_rows_are_grouped_per_scan_minute():
    rows = [row(ts="2026-09-02 12:07:01"), row(ts="2026-09-02 12:07:59"),
            row(ts="2026-09-02 12:20:00"), row(ts="")]
    groups = wns.group_scans(rows)
    assert [key for key, _ in groups] == ["2026-09-02 12:07", "2026-09-02 12:20", "unknown"]
    assert len(groups[0][1]) == 2


def test_summarise_counts_alerts_and_blocked_rows_separately():
    out = wns.summarise([row(signal="BUY", reason=""),
                         row(coin="ETH/USDT:USDT", reason="no_clear_target"),
                         row(coin="SOL/USDT:USDT", reason="insufficient_primary_evidence, "
                                                          "insufficient_primary_evidence")],
                        alert_min=None)
    assert out["rows"] == 3 and out["signalled"] == 1 and out["blocked"] == 2
    assert out["histogram"]["no_clear_target"] == 1
    assert out["histogram"]["insufficient_primary_evidence"] == 2
    assert out["layers"]["target (reward side)"]["blocked_only_by_this"] == 1


def test_repairing_a_layer_only_clears_rows_it_alone_blocked():
    rows = [row(coin="A", reason="target_too_close, poor_rr"),
            row(coin="B", reason="no_clear_target, stop_too_wide"),
            row(coin="C", reason="stop_too_wide")]
    target = wns.simulate(rows, set(wns.LAYERS["target (reward side)"]), alert_min=None)
    assert [r["coin"] for r in target] == ["A"]
    both = wns.simulate(rows, set(wns.LAYERS["target (reward side)"]) | set(wns.LAYERS["stop (risk side)"]),
                        alert_min=None)
    assert [r["coin"] for r in both] == ["A", "B", "C"]


def test_alert_floor_filters_rows_that_would_stay_log_only():
    rows = [row(coin="A", reason="no_clear_target", quality="70"),
            row(coin="B", reason="no_clear_target", quality="20")]
    fixed = set(wns.LAYERS["target (reward side)"])
    assert [r["coin"] for r in wns.simulate(rows, fixed, alert_min=None)] == ["A", "B"]
    assert [r["coin"] for r in wns.simulate(rows, fixed, alert_min=50)] == ["A"]


def test_rows_without_levels_are_not_double_counted_as_signalled():
    # An older build could log a HOLD row with no reason (e.g. llm override); the
    # report must surface it rather than silently treating it as a signal.
    out = wns.summarise([row(reason="")])
    assert out["signalled"] == 0 and out["no_reason_hold"] == 1


def test_missing_or_malformed_numbers_do_not_crash_the_report(tmp_path):
    path = tmp_path / "signals_log.csv"
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "coin", "signal", "setup_quality",
                                                "RR", "no_trade_reason"])
        writer.writeheader()
        writer.writerow({"timestamp": "2026-09-02 12:07:01", "coin": "A", "signal": "HOLD",
                         "setup_quality": "", "RR": None, "no_trade_reason": "no_clear_target"})
        writer.writerow({"timestamp": "2026-09-02 12:07:02", "coin": "B", "signal": "HOLD",
                         "setup_quality": "n/a", "RR": "1.2", "no_trade_reason": ""})
    rows = wns.load_rows(path)
    out = wns.summarise(rows, alert_min=50.0)
    assert out["quality"]["n"] == 0            # blanks and 'n/a' are skipped, not guessed
    assert out["rr"]["n"] == 1
    assert out["layers"]["target (reward side)"]["would_clear"] == 1


def test_unknown_fix_layer_is_rejected_by_the_cli(tmp_path, capsys, monkeypatch):
    path = tmp_path / "signals_log.csv"
    path.write_text("timestamp,coin,signal,no_trade_reason\n2026-09-02 12:07,A,HOLD,\n")
    monkeypatch.setattr(sys, "argv", ["why_no_signals.py", "--file", str(path), "--fix", "vibes",
                                      "--alert-min", "50"])
    try:
        wns.main()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unknown --fix layer must exit non-zero")
    assert "unknown --fix layer" in capsys.readouterr().err
