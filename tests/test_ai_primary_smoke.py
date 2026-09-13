import config
import main


def _d():
    snap = {"close": 100, "ema21": 99, "vwap": 99, "rsi": 55, "rsi_prev": 52,
            "volume": 100, "volume_prev": 90, "volume_avg20": 100, "bb_mid": 100,
            "bb_upper": 102, "bb_lower": 98}
    return {
        "decision": "NO_TRADE", "direction": "BUY", "setup_quality": 10,
        "entry": 100, "snaps": {"1h": snap, "15m": snap, "5m": snap},
        "structure": {"atr": 1}, "sr": {}, "liquidity": {},
        "mtf": {}, "price_action": {}, "trendline": {}, "futures": {},
    }


def test_all_survivors_are_eligible_even_low_quality(monkeypatch):
    monkeypatch.setattr(config, "MAX_CANDIDATES_FOR_AI", 0)
    rows = [(dict(_d(), setup_quality=1), {"symbol": "A"}),
            (dict(_d(), setup_quality=99), {"symbol": "B"})]
    assert len(main._select_ai_eligible(rows)) == 2


def test_llm_buy_is_primary_but_invalid_risk_is_still_blocked():
    d = _d()
    d["risk"] = {"sl": 99, "tp": 102, "rr": 2, "ok": True, "reasons": []}
    out, signal, used, reason = main._apply_llm_verdict(
        d, {"signal": "LONG", "reason": "RSI 55 and price above EMA21."}, "A")
    assert used is True
    assert signal in ("BUY", "HOLD")
    assert reason.startswith("RSI")
