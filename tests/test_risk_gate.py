"""Deterministic tests for the risk/reward & trade-quality gate."""
import config
import risk_gate


STRUCT = {"last_swing_low": {"price": 101.0}, "last_swing_high": {"price": 130.0}}
SR_FAR = {"nearest_resistance": {"lo": 120.0, "hi": 122.0},
          "nearest_support": {"lo": 95.0, "hi": 97.0}, "at_zone": None}
ATR = 2.0


def test_good_long_clears_gate():
    out = risk_gate.evaluate("LONG", 105.0, STRUCT, SR_FAR, ATR)
    assert out["ok"] is True
    assert out["reasons"] == []
    assert out["rr"] >= config.MIN_RR
    assert out["sl"] < 105.0 < out["tp"]


def test_poor_rr_is_rejected():
    sr_close = {"nearest_resistance": {"lo": 108.0, "hi": 109.0},
                "nearest_support": {"lo": 95.0, "hi": 97.0}, "at_zone": None}
    out = risk_gate.evaluate("LONG", 105.0, STRUCT, sr_close, ATR)
    assert out["ok"] is False
    assert "poor_rr" in out["reasons"]


def test_stop_too_wide_is_rejected():
    wide = {"last_swing_low": {"price": 90.0}, "last_swing_high": {"price": 130.0}}
    out = risk_gate.evaluate("LONG", 105.0, wide, SR_FAR, ATR)
    assert out["ok"] is False
    assert "stop_too_wide" in out["reasons"]


def test_into_opposing_zone_is_rejected():
    sr_at_res = {**SR_FAR, "at_zone": {"side": "resistance"}}
    out = risk_gate.evaluate("LONG", 105.0, STRUCT, sr_at_res, ATR)
    assert out["ok"] is False
    assert "into_opposing_zone" in out["reasons"]


def test_no_target_is_rejected():
    sr_none = {"nearest_resistance": None, "nearest_support": {"lo": 95.0, "hi": 97.0},
               "at_zone": None}
    out = risk_gate.evaluate("LONG", 105.0, STRUCT, sr_none, ATR)
    assert out["ok"] is False
    assert "no_clear_target" in out["reasons"]


def test_excessive_spread_is_rejected():
    out = risk_gate.evaluate("LONG", 105.0, STRUCT, SR_FAR, ATR, spread_pct=0.01)
    assert out["ok"] is False
    assert "excessive_spread" in out["reasons"]


def test_short_mirror_clears_gate():
    struct = {"last_swing_low": {"price": 80.0}, "last_swing_high": {"price": 109.0}}
    sr = {"nearest_resistance": {"lo": 118.0, "hi": 120.0},
          "nearest_support": {"lo": 90.0, "hi": 92.0}, "at_zone": None}
    out = risk_gate.evaluate("SHORT", 105.0, struct, sr, ATR)
    assert out["ok"] is True
    assert out["tp"] < 105.0 < out["sl"]
