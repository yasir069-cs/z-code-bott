"""Phase 7 + 8 tests — OpenRouter/Nemotron decision (mocked HTTP, never a
real API call) and the Python fallback."""
import json

import pytest
import requests as _requests

import ai_decision
import config
import fallback
from main import _decide


def _bundle(**over):
    base = dict(
        symbol="TEST/USDT",
        direction="BUY",
        current_price=100.0,
        entry_price=99.8,
        ind_1h={
            "rsi": 55.1, "rsi_prev": 51.0, "ema21": 99.0, "vwap": 99.3,
            "bb_lower": 99.1, "bb_mid": 100.0, "bb_upper": 101.2,
            "close": 99.8, "atr": 0.8, "range_pos": 0.2,
            "swing_low_20": 98.5, "swing_high_20": 103.0,
            "volume_trend": [900, 950, 1000, 1100, 1300],
        },
        sweep={
            "direction": "BUY", "age_candles": 2, "level": 98.6,
            "wick": 1.2, "wick_body_ratio": 4.0, "volume_ratio": 2.1,
        },
        ind_15m={"rsi": 57.0, "rsi_prev": 54.0, "ema21": 99.2, "vwap": 99.4,
                 "bb_lower": 99.2, "bb_mid": 100.0, "bb_upper": 101.0,
                 "close": 99.9, "atr": 0.4,
                 "volume_trend": [900, 950, 1000, 1100, 1200]},
        confirm_score=5,
        ind_5m={"rsi": 56.0, "rsi_prev": 51.0, "rsi_history": [48, 50, 55, 51, 56],
                "ema21": 99.4, "vwap": 99.6, "bb_lower": 99.5, "bb_mid": 100.0,
                "bb_upper": 100.8, "close": 99.8, "atr": 0.2,
                "volume_trend": [900, 950, 1000, 1100, 1250]},
    )
    base.update(over)
    return base


# ------------------------------------------------------------------ prompt
def test_prompt_contains_required_context():
    prompt = ai_decision.build_prompt(_bundle())
    for needle in ("TEST/USDT", "Current price", "Entry price",
                   "1H CONTEXT", "Liquidation sweep", "15M CONFIRMATION",
                   "5M ENTRY", "Last 10 RSI values", "RSI trend direction",
                   "swing low", "swing high", "ATR", "volume", "confidence"):
        assert needle.lower() in prompt.lower(), f"prompt missing {needle!r}"


def test_prompt_states_rsi_trend_direction_explicitly():
    assert "RSI trend direction: UP" in ai_decision.build_prompt(_bundle())
    sell = ai_decision.build_prompt(_bundle(direction="SELL"))
    assert "RSI trend direction: DOWN" in sell


# ------------------------------------------------------ mocked HTTP helpers
class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _mock_post(monkeypatch, resp):
    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["payload"] = json
        calls["url"] = url
        return resp

    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    return calls


def _content_resp(content):
    return _Resp(payload={"choices": [{"message": {"content": content, "reasoning": "secret chain-of-thought"}}]})


# ------------------------------------------------------------ real parsing
def test_valid_buy_json(monkeypatch):
    calls = _mock_post(monkeypatch, _content_resp(
        json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 98.5, "take_profit": 103,
                    "rr": 2.0, "confidence": 82, "reason": "sweep + RSI higher low"})))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "BUY" and out["ai_used"] is True
    assert out["sl"] == 98.5 and out["tp"] == 103.0 and out["rr"] == 2.0
    assert out["confidence"] == 82
    # request shape: OpenRouter endpoint, model, reasoning per config, token budget
    assert calls["url"] == ai_decision.OPENROUTER_URL
    assert calls["payload"]["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert calls["payload"]["reasoning"] == {"enabled": config.AI_REASONING_ENABLED}
    assert calls["payload"]["max_tokens"] == config.AI_MAX_TOKENS
    assert "Authorization" not in calls["payload"]  # key only in headers


def test_valid_sell_json(monkeypatch):
    _mock_post(monkeypatch, _content_resp(
        json.dumps({"signal": "SELL", "entry": 99, "stop_loss": 100.5, "take_profit": 96,
                    "rr": 2.0, "confidence": 71, "reason": "bearish rejection"})))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "SELL" and out["sl"] == 100.5 and out["tp"] == 96.0


def test_valid_hold_json(monkeypatch):
    _mock_post(monkeypatch, _content_resp(
        json.dumps({"signal": "HOLD", "entry": None, "stop_loss": None,
                    "take_profit": None, "rr": None, "confidence": 0,
                    "reason": "Conflicting market conditions."})))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "HOLD" and out["sl"] is None and out["entry"] == 100.0


def test_markdown_wrapped_json(monkeypatch):
    fenced = "```json\n" + json.dumps(
        {"signal": "BUY", "entry": 100, "stop_loss": 98, "take_profit": 104,
         "rr": 2.0, "confidence": 60, "reason": "ok"}) + "\n```"
    _mock_post(monkeypatch, _content_resp(fenced))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "BUY" and out["sl"] == 98.0


def test_invalid_json_raises(monkeypatch):
    _mock_post(monkeypatch, _content_resp("I cannot decide today, sorry."))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_missing_fields_raise():
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.parse_ai_response(
            json.dumps({"signal": "BUY", "entry": 100, "rr": 2, "reason": "no sl/tp"}), 100.0)


def test_invalid_signal_raises():
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.parse_ai_response(
            json.dumps({"signal": "MAYBE", "entry": 1, "stop_loss": 1, "take_profit": 1,
                        "rr": 1, "confidence": 50, "reason": "?"}), 100.0)


def test_confidence_out_of_range_raises():
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.parse_ai_response(
            json.dumps({"signal": "HOLD", "entry": None, "stop_loss": None,
                        "take_profit": None, "rr": None, "confidence": 900,
                        "reason": "?"}), 100.0)


def test_buy_bad_geometry_raises():
    bad = json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 101,
                      "take_profit": 103, "rr": 2, "confidence": 50, "reason": "x"})
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.parse_ai_response(bad, 100.0)


def test_http_failure_raises(monkeypatch):
    _mock_post(monkeypatch, _Resp(status=502, text="bad gateway"))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_timeout_raises(monkeypatch):
    def fake_post(*a, **k):
        raise _requests.exceptions.Timeout("timed out")
    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_empty_content_raises(monkeypatch):
    _mock_post(monkeypatch, _content_resp("   "))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_malformed_response_body_raises(monkeypatch):
    _mock_post(monkeypatch, _Resp(payload={"error": "no choices"}))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_missing_key_raises_immediately(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())


def test_reasoning_never_leaks_into_signal(monkeypatch):
    resp = _Resp(payload={"choices": [{"message": {
        "content": json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 98,
                               "take_profit": 104, "rr": 2.0, "confidence": 55,
                               "reason": "ok"}),
        "reasoning": "internal chain of thought"}}]})
    _mock_post(monkeypatch, resp)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert "internal chain of thought" not in json.dumps(out)


# ------------------------------------------------------- fallback activation
def test_fallback_activates_on_ai_failure(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "")  # AI cannot run
    out = _decide(_bundle())                              # main pipeline decider
    assert out["ai_used"] is False                        # fallback used
    assert out["signal"] == "BUY"
    assert out["reason"].startswith("AI Unavailable - Indicator based signal")


# ------------------------------------------------------------------ fallback math
def test_fallback_buy_1_to_2_rr_from_sweep_level():
    out = fallback.fallback_decision(_bundle())
    assert out["signal"] == "BUY" and out["ai_used"] is False
    assert out["sl"] == 98.6                       # sweep level
    assert out["tp"] == pytest.approx(99.8 + 2 * (99.8 - 98.6))
    assert out["rr"] == 2.0


def test_fallback_sell_uses_swing_high():
    b = _bundle(direction="SELL")
    b["sweep"] = None
    out = fallback.fallback_decision(b)
    assert out["signal"] == "SELL"
    assert out["sl"] == b["ind_1h"]["swing_high_20"]  # 103.0
    assert out["tp"] == pytest.approx(99.8 - 2 * (103.0 - 99.8))


def test_fallback_degenerate_swing_uses_atr_buffer():
    b = _bundle()
    b["sweep"] = None
    b["ind_1h"]["swing_low_20"] = 100.5             # above entry -> invalid for BUY SL
    out = fallback.fallback_decision(b)
    assert out["sl"] == pytest.approx(99.8 - 1.5 * 0.8)   # entry - 1.5*ATR


def test_fallback_reason_is_tagged():
    assert fallback.fallback_decision(_bundle())["reason"].startswith(
        "AI Unavailable - Indicator based signal")


def test_fallback_never_claims_ai():
    assert fallback.fallback_decision(_bundle())["ai_used"] is False
