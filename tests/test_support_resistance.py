"""Deterministic tests for the support/resistance zone detector."""
import numpy as np

import config
import support_resistance as sr
from conftest import make_candles


def zigzag(controls, seg=5):
    """Piecewise-linear close series; control points become swing pivots.

    A repeated high/low control (e.g. 110 tapped several times) yields a
    horizontal level the detector should cluster into one strength-scored zone.
    """
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, seg, endpoint=False))
    closes.append(controls[-1])
    return closes


# ------------------------------------------------------------- zone detection

def test_resistance_zone_detected_above_price():
    closes = zigzag([100, 110, 100, 110, 100, 110, 101, 104])
    out = sr.analyze(make_candles(closes, wick=0.15))
    assert out["nearest_resistance"] is not None
    z = out["nearest_resistance"]
    assert z["mid"] > out["price"]          # opposing zone sits above current price
    assert z["side"] == "resistance"
    assert z["touches"] >= config.SR_MIN_TOUCHES


def test_support_zone_detected_below_price():
    closes = zigzag([100, 110, 100, 110, 100, 110, 101, 104])
    out = sr.analyze(make_candles(closes, wick=0.15))
    assert out["nearest_support"] is not None
    assert out["nearest_support"]["mid"] < out["price"]
    assert out["nearest_support"]["side"] == "support"


def test_zones_are_ranges_not_single_prices():
    closes = zigzag([100, 110, 100, 110, 100, 110, 101, 104])
    out = sr.analyze(make_candles(closes, wick=0.15))
    assert out["zones"]
    for z in out["zones"]:
        assert z["hi"] > z["lo"]            # every zone is a padded range
        assert z["lo"] <= z["mid"] <= z["hi"]


def test_repeatedly_touched_level_is_major():
    # four clean taps of 110 -> touches >= SR_MAJOR_TOUCHES -> major
    closes = zigzag([100, 110, 100, 110, 100, 110, 100, 110, 102, 105])
    out = sr.analyze(make_candles(closes, wick=0.15))
    resistances = [z for z in out["zones"] if z["side"] == "resistance"]
    assert resistances
    assert any(z["major"] for z in resistances)


# ----------------------------------------------------------------- at-zone

def test_at_zone_set_when_price_sits_in_a_level():
    # price finishes right back at the 110 level it kept tapping
    closes = zigzag([100, 110, 100, 110, 100, 110, 109])
    out = sr.analyze(make_candles(closes, wick=0.15))
    assert out["at_zone"] is not None


# --------------------------------------------------------------- session levels

def test_prev_day_levels_present_on_multiday_frame():
    closes = zigzag([100, 108, 101, 109, 102, 110, 103, 111, 104], seg=8)
    out = sr.analyze(make_candles(closes, freq="1h"))   # ~65 candles -> spans days
    sess = out["session_levels"]
    assert sess["prev_day_high"] is not None
    assert sess["prev_day_high"] >= sess["prev_day_low"]


def test_prev_day_levels_none_when_intraday_short():
    out = sr.analyze(make_candles(zigzag([100, 102, 100, 102]), freq="5min"))
    assert out["session_levels"]["prev_day_high"] is None
    assert out["session_levels"]["prev_week_high"] is None


# --------------------------------------------------------------- safe degrade

def test_thin_data_degrades_safely():
    out = sr.analyze(make_candles([100, 101, 102]))
    assert out["zones"] == []
    assert out["nearest_support"] is None and out["nearest_resistance"] is None
    assert out["at_zone"] is None
