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


# ── target search across the full zone list (RISK_TARGET_SCAN_ZONES) ────────
# A zone is a RANGE (~1 ATR wide: 0.5 ATR cluster tolerance + 0.25 ATR pad per
# side), so the "nearest opposing zone" is very often a wall at the entry
# rather than a target. Live scan 2026-09-02: 21/37 coins died on
# `no_clear_target` and 11 on `poor_rr` while a usable zone sat unread.
ATR2 = 2.0


def _z(lo, hi, side):
    return {"lo": lo, "hi": hi, "mid": (lo + hi) / 2.0, "side": side,
            "major": True, "touches": 5, "strength": 6.0}


SR_WALL_THEN_ROOM = {
    "nearest_support": _z(99.4, 100.3, "support"),        # straddles entry=100
    "nearest_resistance": _z(104.0, 105.0, "resistance"),
    "zones": [_z(94.0, 95.2, "support"), _z(99.4, 100.3, "support")],
    "at_zone": None,
}


def test_short_targets_deeper_zone_when_nearest_straddles_entry():
    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, SR_WALL_THEN_ROOM, ATR2)
    assert out["ok"] is True, out["reasons"]
    assert out["tp"] < 100.0
    assert out["rr"] >= config.MIN_RR
    # the note names the zone actually used and says a nearer one was skipped
    assert "1 nearer opposing zone(s)" in out["target_note"]


def test_long_mirror_targets_further_zone():
    sr = {"nearest_resistance": _z(100.1, 100.8, "resistance"),   # straddles entry
          "nearest_support": None,
          "zones": [_z(100.1, 100.8, "resistance"), _z(106.0, 107.2, "resistance")],
          "at_zone": None}
    struct = {"last_swing_low": {"price": 98.4}, "atr": ATR2}
    out = risk_gate.evaluate("LONG", 100.0, struct, sr, ATR2)
    assert out["ok"] is True, out["reasons"]
    assert out["tp"] > 100.0 and out["rr"] >= config.MIN_RR


def test_target_is_never_on_the_wrong_side_of_the_entry():
    """A zone 0.1 ATR from the entry: padded TP would land above the entry for
    a SHORT. It must be refused as a target, not emitted as a fake one."""
    sr = {"nearest_support": _z(99.55, 99.85, "support"), "nearest_resistance": None,
          "zones": [_z(99.55, 99.85, "support")], "at_zone": None}
    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, sr, ATR2)
    assert out["ok"] is False
    assert "no_clear_target" in out["reasons"]
    assert out["tp"] is None


def test_no_usable_target_still_rejects_without_a_zone_list():
    sr = {"nearest_support": _z(99.4, 100.3, "support"), "nearest_resistance": None,
          "at_zone": None}                       # legacy shape: no "zones" key
    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, sr, ATR2)
    assert out["ok"] is False and "no_clear_target" in out["reasons"]


def test_too_close_nearest_zone_keeps_target_too_close_diagnostic():
    """No deeper zone -> the owner's `target_too_close` reason still fires."""
    sr = {"nearest_support": _z(97.4, 97.9, "support"), "nearest_resistance": None,
          "zones": [_z(97.4, 97.9, "support")], "at_zone": None}
    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, sr, ATR2)
    assert out["ok"] is False
    assert "target_too_close" in out["reasons"]
    assert "no deeper opposing zone reachable" in out["target_note"]


def test_zone_search_can_be_disabled_to_nearest_only():
    """RISK_TARGET_SCAN_ZONES=False reproduces the old nearest-only veto."""
    class Cfg:
        RISK_SL_BUFFER_ATR = config.RISK_SL_BUFFER_ATR
        RISK_MAX_STOP_ATR = config.RISK_MAX_STOP_ATR
        RISK_MIN_TARGET_ATR = config.RISK_MIN_TARGET_ATR
        RISK_TARGET_ZONE_PAD_ATR = config.RISK_TARGET_ZONE_PAD_ATR
        RISK_MAX_SPREAD_PCT = config.RISK_MAX_SPREAD_PCT
        MIN_RR = config.MIN_RR
        RISK_TARGET_SCAN_ZONES = False

    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, SR_WALL_THEN_ROOM, ATR2, cfg=Cfg)
    assert out["ok"] is False
    assert "no_clear_target" in out["reasons"]


def test_atr_zero_shape_carries_every_key():
    out = risk_gate.evaluate("SHORT", 100.0, {}, {}, 0.0)
    assert out["reasons"] == ["no_atr"]
    assert "target_note" in out


# ── what the alert is told about the target ──────────────────────────────────

def test_target_zone_is_reported_as_a_separate_field():
    """The alert and the prompt need the zone label itself (compact) plus how many
    nearer zones were skipped — not a sentence to re-parse."""
    struct = {"last_swing_high": {"price": 101.6}, "atr": ATR2}
    out = risk_gate.evaluate("SHORT", 100.0, struct, SR_WALL_THEN_ROOM, ATR2)
    assert out["target_zone"].startswith("support@")
    assert out["target_skipped"] == 1
    assert "unusable or too close" in out["target_note"]


def test_alert_names_the_zone_the_target_came_from():
    from main import _target_zone_label
    risk = {"target_zone": "support@94.6", "target_skipped": 1}
    near = {"mid": 99.85, "major": False}
    assert _target_zone_label(risk, near) == "support@94.6 (+1 nearer zone(s) skipped)"
    # no zone chosen -> fall back to the nearest one, as before
    assert _target_zone_label({"target_zone": ""}, near) == "minor@99.85"
    assert _target_zone_label(None, None) == ""
    # an unobstructed choice is stated plainly, without a skip count
    assert _target_zone_label({"target_zone": "resistance@104.6",
                               "target_skipped": 0}, None) == "resistance@104.6"
