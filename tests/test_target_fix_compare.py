"""Tests for scripts/target_fix_compare.py — the before/after the owner decides on.

The script is the evidence behind "deploy the target fix or keep tuning floors", so
its counting rules matter more than its formatting: what counts as *cleared by the
target fix* (and therefore what does not), that the risk side is asserted unchanged,
and that toggling the flag never leaks out of the comparison.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import target_fix_compare as t  # noqa: E402


def _decision(decision="NO_TRADE", reasons=(), quality=40.0, rr=None, tp=None):
    return {"decision": decision, "no_trade_reasons": list(reasons),
            "setup_quality": quality, "rr": rr, "tp": tp}


def test_a_setup_blocked_only_by_reward_side_codes_counts_as_cleared():
    pair = t.pair_from("A", _decision(reasons=["no_clear_target"]),
                       _decision("LONG", [], 62.0, 2.2, 105.0))
    assert pair["cleared_only_by_target"] is True
    assert pair["codes_before"] == ["no_clear_target"] and pair["codes_after"] == []
    assert pair["rr_after"] == 2.2


def test_a_stop_rejection_is_never_credited_to_the_target_fix():
    pair = t.pair_from("A", _decision(reasons=["no_clear_target", "stop_too_wide"]),
                       _decision(reasons=["stop_too_wide"]))
    assert pair["cleared_only_by_target"] is False


def test_a_clear_setup_turning_blocked_is_counted_as_newly_blocked():
    pair = t.pair_from("A", _decision("LONG", [], 60.0, 2.0, 105.0),
                       _decision(reasons=["into_opposing_zone"]))
    assert pair["newly_blocked"] is True


def test_summarise_reports_verdict_shifts_and_the_risk_side_invariant():
    pairs = [t.pair_from("A", _decision(reasons=["no_clear_target"]), _decision("LONG", [])),
             t.pair_from("B", _decision(reasons=["no_clear_target", "stop_too_wide"]),
                         _decision(reasons=["stop_too_wide"])),
             t.pair_from("C", _decision("LONG", [], 70.0, 2.5, 110.0),
                         _decision("LONG", [], 70.0, 2.5, 110.0))]
    out = t.summarise(pairs)
    assert out["coins"] == 3
    assert out["verdicts_before"] == {"LONG": 1, "SHORT": 0, "NO_TRADE": 2}
    assert out["verdicts_after"] == {"LONG": 2, "SHORT": 0, "NO_TRADE": 1}
    assert out["cleared_only_by_target"] == 1
    assert out["codes_before"]["no_clear_target"] == 2 and out["codes_after"].get("no_clear_target") is None
    assert out["risk_side_identical"] is True

    broken = [t.pair_from("A", _decision(reasons=["stop_too_wide"]),
                          _decision(reasons=["stop_too_wide", "no_structure_stop"]))]
    assert t.summarise(broken)["risk_side_identical"] is False


def test_the_gate_flag_is_set_twice_and_restored():
    """The whole comparison depends on evaluating the same frames under both
    settings — and on leaving `config` as it was, or a later call in this process
    would silently run a different policy."""
    seen = []

    class FakeCore:
        def decide(self, frames, funding_rate=None, oi_df=None, symbol=None):
            seen.append(config.RISK_TARGET_SCAN_ZONES)
            if config.RISK_TARGET_SCAN_ZONES:
                return _decision("LONG", [], 61.0, 2.1, 104.0)
            return _decision(reasons=["no_clear_target"])

    saved = config.RISK_TARGET_SCAN_ZONES
    try:
        config.RISK_TARGET_SCAN_ZONES = False       # an unusual setting must come back
        before, after = t.decide_both(FakeCore(), {"1h": None}, "A/USDT:USDT")
        assert seen == [False, True]
        assert before["decision"] == "NO_TRADE" and after["decision"] == "LONG"
        assert config.RISK_TARGET_SCAN_ZONES is False
    finally:
        config.RISK_TARGET_SCAN_ZONES = saved


def test_a_crash_mid_comparison_still_restores_the_flag():
    class Exploding:
        def decide(self, frames, funding_rate=None, oi_df=None, symbol=None):
            if config.RISK_TARGET_SCAN_ZONES is False:
                raise RuntimeError("exchange blew up")
            return _decision()

    config.RISK_TARGET_SCAN_ZONES = True
    with pytest.raises(RuntimeError):
        t.decide_both(Exploding(), {}, "A")
    assert config.RISK_TARGET_SCAN_ZONES is True


def test_coins_come_from_the_newest_scan_of_the_log(tmp_path):
    header = "timestamp,coin,signal,no_trade_reason\n"
    body = ("2026-09-02 12:07:01,OLD/USDT:USDT,HOLD,no_clear_target\n"
            "2026-09-02 12:20:01,NEW-A/USDT:USDT,HOLD,stop_too_wide\n"
            "2026-09-02 12:20:02,NEW-B/USDT:USDT,HOLD,poor_rr\n"
            "2026-09-02 12:20:03,NEW-A/USDT:USDT,HOLD,poor_rr\n")
    path = tmp_path / "signals_log.csv"
    path.write_text(header + body)
    assert t.symbols_from_log(path, 0) == ["NEW-A/USDT:USDT", "NEW-B/USDT:USDT"]
    assert t.symbols_from_log(path, 1) == ["NEW-A/USDT:USDT"]
    assert t.symbols_from_log(tmp_path / "absent.csv", 0) == []


def test_codes_helper_ignores_nothing_and_tolerates_missing_key():
    assert t.codes(_decision(reasons=["poor_rr", "", "stop_too_wide"])) == ["poor_rr", "stop_too_wide"]
    assert t.codes({}) == []
