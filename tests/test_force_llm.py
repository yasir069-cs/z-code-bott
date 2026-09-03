"""--force-llm harness tests: the gate-bypass bundle that feeds the LLM.

run_force_llm() itself needs a live exchange, so these cover the piece that
can break silently: _build_llm_bundle must hand the prompt every field it
reads (indicator snapshots, direction mapping, liquidation context) and skip
coins whose snapshots are missing instead of crashing the batch.
"""
from main import _build_llm_bundle


def _snap():
    return {
        "rsi": 55.0, "rsi_prev": 51.0, "ema21": 99.0, "vwap": 99.3,
        "bb_lower": 99.1, "bb_mid": 100.0, "bb_upper": 101.2,
        "close": 99.8, "atr": 0.8, "range_pos": 0.2,
        "swing_low_20": 98.5, "swing_high_20": 103.0,
        "volume_trend": [900, 950, 1000, 1100, 1300],
        "rsi_history": [48, 50, 55, 51, 56],
    }


def _cand():
    return {
        "symbol": "BTC/USDT:USDT",
        "direction": "BUY",
        "feat_1h": {"indicators": _snap(), "sweep": None},
        "feat_15m": {"indicators": _snap()},
        "feat_5m": {"indicators": _snap()},
    }


def _decision(direction, entry=99.8, quality=61.0):
    return {"direction": direction, "entry": entry, "sr": {},
            "setup_quality": quality}


def test_bundle_carries_every_prompt_field():
    bundle = _build_llm_bundle(_cand(), _decision("LONG"), last_price=100.5)
    assert bundle["symbol"] == "BTC/USDT:USDT"
    assert bundle["direction"] == "BUY"            # LONG mapped to prompt wording
    assert bundle["current_price"] == 100.5
    assert bundle["ind_1h"]["swing_low_20"]         # prompt reads these directly
    assert bundle["ind_5m"]["rsi_history"]
    assert "liquidation" in bundle                  # websocket context attached


def test_bundle_maps_short_direction():
    bundle = _build_llm_bundle(_cand(), _decision("SHORT"), last_price=100.0)
    assert bundle["direction"] == "SELL"


def test_bundle_falls_back_to_funnel_direction_and_price():
    """A no_directional_bias decision (direction=None) still gets a bundle —
    that is the whole point of --force-llm."""
    d = _decision(None)
    bundle = _build_llm_bundle(_cand(), d, last_price=None)
    assert bundle["direction"] == "BUY"             # funnel hint used
    assert bundle["current_price"] == 99.8          # 5M close used as price


def test_bundle_skips_coin_with_missing_snapshot():
    cand = _cand()
    cand["feat_5m"] = {"indicators": None}
    assert _build_llm_bundle(cand, _decision("LONG"), None) is None


def test_cli_exposes_force_llm_flags():
    """--help must list both flags so the test harness is discoverable."""
    import inspect
    import main
    src = inspect.getsource(main.main)
    assert "--force-llm" in src and "--force-llm-top" in src
    assert "run_force_llm" in src.replace("def run_force_llm", "")  # dispatched
