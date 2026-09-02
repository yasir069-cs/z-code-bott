"""Phase 7 + 8 tests — OpenRouter/Nemotron decision (mocked HTTP, never a
real API call) and the Python fallback.

Phase 3 of the reconstruction added batching, retry/backoff, a secondary
model and per-IST-day budget accounting, so the tests below also cover:
  * a transient 502 is retried instead of losing the signal
  * a truncated JSON body is retried (it is a model failure, not a verdict)
  * one scan's candidates cost ONE request, not one request each
  * a single bad array element only costs that coin its AI decision
  * the prompt reports the measured zone instead of asserting "bottom 30%"
"""
import json

import pytest
import requests as _requests

import ai_decision
import config
import fallback
from main import _decide


@pytest.fixture(autouse=True)
def _no_backoff_sleep_and_fresh_budget(monkeypatch):
    """Retries are real now, so without this every failure test would sleep
    1s + 2s per model. Also resets the daily counter so test order cannot
    starve later tests of budget."""
    monkeypatch.setattr(ai_decision, "_backoff_sleep", lambda *a, **k: None)
    ai_decision.reset_budget()
    yield
    ai_decision.reset_budget()


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
        # Phase 1 grading, now handed to the model as evidence
        score_1h=82.0, score_15m=76.0, score_5m=71.0, confluence=76.9,
        score_breakdown_1h={"zone": 25.0, "rsi": 20.0, "volume": 15.0,
                            "bb": 7.1, "sweep": 25.0},
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
    def __init__(self, payload=None, status=200, text="", headers=None):
        self._payload = payload
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _SseResp(_Resp):
    """A gateway that answers with text/event-stream despite stream:false."""

    def __init__(self, deltas, status=200):
        events = [f"data: {json.dumps({'choices': [{'delta': {'content': d}}]})}"
                  for d in deltas] + ["data: [DONE]"]
        super().__init__(payload=None, status=status,
                         text="\n".join(events),
                         headers={"Content-Type": "text/event-stream"})


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
    # request shape: provider endpoint, model, token budget; the OpenRouter
    # "reasoning" field is OMITTED when disabled (strict gateways reject
    # unknown body fields) and only sent when AI_REASONING_ENABLED.
    assert calls["url"] == ai_decision.OPENROUTER_URL
    assert calls["payload"]["model"] == config.AI_MODEL
    if config.AI_REASONING_ENABLED:
        assert calls["payload"]["reasoning"] == {"enabled": True}
    else:
        assert "reasoning" not in calls["payload"]
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


def test_sse_streamed_response_is_joined(monkeypatch):
    """Some gateways (AgentRouter seen live) answer with text/event-stream
    despite stream:false — the JSON parse then failed with 'Expecting value:
    line 1 column 1'. The deltas must be joined into one answer instead."""
    verdict = json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 98.5,
                          "take_profit": 103, "rr": 2.0, "confidence": 80,
                          "reason": "sse joined", "rsi_bounce_detected": True})
    _mock_post(monkeypatch, _SseResp(['{"signal": "BUY", ', '"entry": 100, ',
                                      '"stop_loss": 98.5, ', '"take_profit": 103, ',
                                      '"rr": 2.0, ', '"confidence": 80, ',
                                      '"reason": "sse joined", ',
                                      '"rsi_bounce_detected": true}']))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "BUY" and out["ai_used"] is True
    assert out["confidence"] == 80
    assert out["reason"] == "sse joined"


def test_sse_detected_by_data_prefix_even_without_content_type(monkeypatch):
    """A gateway that streams but forgets the event-stream content-type is
    still recognized from the body's leading 'data:' line."""
    resp = _Resp(text='data: {"choices": [{"delta": {"content": "OK"}}]}\n\ndata: [DONE]')
    _mock_post(monkeypatch, resp)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    assert ai_decision._join_sse_deltas(resp.text) == "OK"


def test_empty_200_body_raises_with_diagnostic(monkeypatch):
    """HTTP 200 with an empty/non-JSON body (the live AgentRouter failure)
    must raise a retryable error whose message carries the content-type and
    body snippet — not a bare 'Expecting value' JSONDecodeError."""
    resp = _Resp(text="", headers={"Content-Type": "application/json"})
    _mock_post(monkeypatch, resp)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError) as ei:
        ai_decision.nemotron_decision(_bundle())
    assert "non-JSON body" in str(ei.value)
    assert ei.value.retryable is True


def test_request_payload_asks_for_non_streaming(monkeypatch):
    """stream:false is sent explicitly so streaming-by-default gateways
    return one plain JSON body."""
    calls = _mock_post(monkeypatch, _content_resp(
        json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 98.5,
                    "take_profit": 103, "rr": 2.0, "confidence": 70,
                    "reason": "ok"})))
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    ai_decision.nemotron_decision(_bundle())
    assert calls["payload"]["stream"] is False


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


# ------------------------------------------------------ AI provider transport
def test_llm_transport_defaults_to_agentrouter(monkeypatch):
    """The AI layer talks to AgentRouter by default: key from
    OPENROUTER_API_KEY, endpoint derived from AI_BASE_URL, model deepseek-v4-flash."""
    import importlib

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("AI_MODEL_FALLBACK", raising=False)
    cfg = importlib.reload(config)
    assert cfg.OPENROUTER_API_KEY == "sk-or-test"
    assert cfg.AI_BASE_URL == "https://agentrouter.org/v1"
    assert cfg.AI_MODEL == "deepseek-v4-flash"
    assert ai_decision.OPENROUTER_URL == "https://agentrouter.org/v1/chat/completions"


def test_empty_fallback_model_disables_the_second_model(monkeypatch):
    """With AI_MODEL_FALLBACK empty (the AgentRouter default — the key serves
    one model) the retry ladder stops after the primary model's attempts."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(config, "AI_MODEL_FALLBACK", "")
    state = _counting_post(monkeypatch, [_Resp(status=503, text="down")])
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())
    assert state["n"] == config.AI_RETRY_MAX, "primary only, no fallback model"


# ------------------------------------------------------- Phase 3: zone truth
def test_prompt_reports_the_measured_zone_not_the_direction():
    """The old prompt hardcoded `zone = "bottom 30%" if direction == "BUY"`, so
    the model was told the coin was in the bottom zone whatever range_pos said,
    and then parroted that back in every logged reason."""
    prompt = ai_decision.build_prompt(_bundle(direction="BUY"))
    assert "range_pos=0.200" in prompt
    assert "deep in the bottom zone" in prompt

    # Same direction, but genuinely mid-range: the prompt must say so.
    mid = _bundle()
    mid["ind_1h"] = dict(mid["ind_1h"], range_pos=0.48)
    mid_prompt = ai_decision.build_prompt(mid)
    assert "IN-BETWEEN" in mid_prompt
    assert "deep in the bottom zone" not in mid_prompt
    assert "range_pos=0.480" in mid_prompt


def test_prompt_zone_mirrors_for_sell():
    sell = _bundle(direction="SELL")
    sell["ind_1h"] = dict(sell["ind_1h"], range_pos=0.92)
    prompt = ai_decision.build_prompt(sell)
    assert "deep in the top zone" in prompt
    assert "bottom" not in prompt.split("1H CONTEXT:")[1].split("\n")[0]


def test_prompt_carries_the_python_scores():
    """The engine's grading is evidence for the model, not something it must
    re-derive from raw numbers."""
    prompt = ai_decision.build_prompt(_bundle())
    assert "PYTHON ENGINE SCORES" in prompt
    assert "1H 82" in prompt and "15M 76" in prompt and "5M 71" in prompt
    assert "confluence 77" in prompt
    assert "zone 25.0" in prompt


def test_prompt_survives_a_bundle_without_scores():
    """main.py must never crash the AI stage just because a score is absent."""
    bare = _bundle()
    for key in ("score_1h", "score_15m", "score_5m", "confluence", "score_breakdown_1h"):
        bare.pop(key)
    prompt = ai_decision.build_prompt(bare)
    assert "PYTHON ENGINE SCORES" not in prompt
    assert "TEST/USDT" in prompt


def test_prompt_labels_a_missing_sweep_as_weaker():
    prompt = ai_decision.build_prompt(_bundle(sweep=None))
    assert "NONE detected" in prompt
    assert "cap confidence" in prompt


def test_system_prompt_quotes_real_config_thresholds():
    """It used to claim "volume > 1.5x average" and "15M score 4/5", neither of
    which the code ever checked. Interpolating from config stops that drift."""
    sp = ai_decision._build_system_prompt()
    assert "1.5x average" not in sp
    assert "4/5" not in sp
    assert f"zone {config.W_1H_ZONE}" in sp
    assert f"{config.RSI_BUY_FULL_MIN:.0f}-{config.RSI_BUY_FULL_MAX:.0f}" in sp
    assert f"{config.RSI_SELL_FULL_MIN:.0f}-{config.RSI_SELL_FULL_MAX:.0f}" in sp


# --------------------------------------------------------- Phase 3: retrying
def _counting_post(monkeypatch, responses):
    """Serve `responses` in order, repeating the last one forever, and count calls."""
    state = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        i = min(state["n"], len(responses) - 1)
        state["n"] += 1
        state.setdefault("models", []).append(json["model"])
        return responses[i]

    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    return state


def test_transient_502_is_retried_and_then_succeeds(monkeypatch):
    """One Nvidia "502 Service temporarily overloaded" used to drop the signal
    straight to indicator-only. It is now just a retry."""
    good = _content_resp(json.dumps(
        {"signal": "BUY", "entry": 100, "stop_loss": 98, "take_profit": 104,
         "rr": 2.0, "confidence": 77, "reason": "retried fine"}))
    state = _counting_post(monkeypatch, [_Resp(status=502, text="overloaded"), good])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "BUY" and out["confidence"] == 77
    assert state["n"] == 2, "the 502 should have cost one retry, not the signal"


def test_truncated_json_is_retried(monkeypatch):
    """AI_MAX_TOKENS = 300 used to cut answers mid-"reason". A malformed body is
    a transient model failure, so it must be retried, not treated as a verdict."""
    truncated = _content_resp('{"signal": "SELL", "entry": 99, "reason": "1H top zone (range_pos 0.73')
    good = _content_resp(json.dumps(
        {"signal": "SELL", "entry": 99, "stop_loss": 100.5, "take_profit": 96,
         "rr": 2.0, "confidence": 64, "reason": "complete answer"}))
    state = _counting_post(monkeypatch, [truncated, good])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decision(_bundle())
    assert out["signal"] == "SELL" and out["reason"] == "complete answer"
    assert state["n"] == 2


def test_retries_are_bounded_then_the_fallback_model_is_tried(monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL_FALLBACK", "deepseek/deepseek-chat-v3.1:free")
    state = _counting_post(monkeypatch, [_Resp(status=503, text="down")])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())
    assert state["n"] == config.AI_RETRY_MAX * 2, "primary then fallback, AI_RETRY_MAX each"
    assert "deepseek/deepseek-chat-v3.1:free" in state["models"]


def test_a_bad_request_is_not_retried_on_the_same_model(monkeypatch):
    """400 means the request is wrong; retrying it only burns budget."""
    monkeypatch.setattr(config, "AI_MODEL_FALLBACK", "deepseek/deepseek-chat-v3.1:free")
    state = _counting_post(monkeypatch, [_Resp(status=400, text="bad model name")])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle())
    assert state["n"] == 2, "one attempt per model, no retries"


def test_deadline_stops_retrying(monkeypatch):
    """Past the scan deadline the AI stage must yield rather than push the scan
    into the next 5-minute slot."""
    import time as _time
    state = _counting_post(monkeypatch, [_Resp(status=502, text="overloaded")])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    with pytest.raises(ai_decision.AIDecisionError):
        ai_decision.nemotron_decision(_bundle(), deadline=_time.monotonic() - 1)
    assert state["n"] == 0, "no request should be sent after the deadline"


# ---------------------------------------------------------- Phase 3: batching
def _batch_bundles(*symbols):
    return [_bundle(symbol=s, confluence=60.0 + i) for i, s in enumerate(symbols)]


def _decision(symbol, signal="BUY", confidence=70):
    return {"symbol": symbol, "signal": signal, "entry": 100, "stop_loss": 98,
            "take_profit": 104, "rr": 2.0, "confidence": confidence, "reason": f"{symbol} ok"}


def test_a_whole_scan_costs_one_request(monkeypatch):
    """The free tier allows 50 requests/day and the bot was hitting it: 5
    candidates used to mean 5 requests and ~51s."""
    bundles = _batch_bundles("A/USDT", "B/USDT", "C/USDT", "D/USDT", "E/USDT")
    payload = json.dumps([_decision(b["symbol"]) for b in bundles])
    state = _counting_post(monkeypatch, [_content_resp(payload)])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(bundles)
    assert set(out) == {b["symbol"] for b in bundles}
    assert state["n"] == 1, "five candidates must cost one request"
    assert ai_decision.budget_status()["used"] == 1


def test_batch_is_chunked_at_the_configured_maximum(monkeypatch):
    bundles = _batch_bundles(*[f"C{i}/USDT" for i in range(config.AI_BATCH_MAX + 3)])
    # Every element is answered; matching is by symbol so one payload can serve both chunks.
    payload = json.dumps([_decision(b["symbol"]) for b in bundles])
    state = _counting_post(monkeypatch, [_content_resp(payload)])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    ai_decision.nemotron_decisions(bundles)
    assert state["n"] == 2, f"{len(bundles)} candidates at AI_BATCH_MAX={config.AI_BATCH_MAX}"


def test_batch_is_ordered_by_confluence(monkeypatch):
    """If the daily budget runs out mid-scan, the strongest setups must be the
    ones that already got the AI."""
    bundles = [_bundle(symbol="WEAK/USDT", confluence=57.0),
               _bundle(symbol="STRONG/USDT", confluence=91.0),
               _bundle(symbol="MID/USDT", confluence=70.0)]
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["prompt"] = json["messages"][1]["content"]
        return _content_resp("[]")

    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    ai_decision.nemotron_decisions(bundles)
    prompt = captured["prompt"]
    assert (prompt.index("STRONG/USDT") < prompt.index("MID/USDT")
            < prompt.index("WEAK/USDT"))


def test_batch_drops_only_the_bad_element(monkeypatch):
    """A malformed element must cost that coin its AI decision, not the batch."""
    bundles = _batch_bundles("GOOD/USDT", "BAD/USDT", "ALSOGOOD/USDT")
    payload = json.dumps([
        _decision("GOOD/USDT"),
        {"symbol": "BAD/USDT", "signal": "BUY", "entry": 100, "stop_loss": 101,
         "take_profit": 104, "rr": 2, "confidence": 50, "reason": "sl above entry"},
        _decision("ALSOGOOD/USDT"),
    ])
    _counting_post(monkeypatch, [_content_resp(payload)])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(bundles)
    assert set(out) == {"GOOD/USDT", "ALSOGOOD/USDT"}
    assert "BAD/USDT" not in out, "the caller must see this coin as un-decided"


def test_batch_matches_by_symbol_not_position(monkeypatch):
    """Models reorder arrays. A reorder must not attach BTC's stop-loss to ETH."""
    bundles = _batch_bundles("FIRST/USDT", "SECOND/USDT")
    payload = json.dumps([_decision("SECOND/USDT", confidence=41),
                          _decision("FIRST/USDT", confidence=88)])
    _counting_post(monkeypatch, [_content_resp(payload)])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(bundles)
    assert out["FIRST/USDT"]["confidence"] == 88
    assert out["SECOND/USDT"]["confidence"] == 41


def test_batch_falls_back_to_position_when_symbol_is_missing(monkeypatch):
    bundles = _batch_bundles("ONE/USDT", "TWO/USDT")
    payload = json.dumps([
        {k: v for k, v in _decision("ONE/USDT").items() if k != "symbol"},
        _decision("TWO/USDT"),
    ])
    _counting_post(monkeypatch, [_content_resp(payload)])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(bundles)
    assert set(out) == {"ONE/USDT", "TWO/USDT"}


def test_an_unlabelled_element_never_steals_a_named_slot():
    """Two-pass matching. The labelled element owns SECOND, so the unlabelled one
    can only take FIRST. Matching in one pass gave FIRST the wrong stop-loss."""
    bundles = _batch_bundles("FIRST/USDT", "SECOND/USDT")
    payload = json.dumps([
        _decision("SECOND/USDT", confidence=33),
        {k: v for k, v in _decision("FIRST/USDT", confidence=88).items() if k != "symbol"},
    ])
    out = ai_decision.parse_batch_response(payload, bundles)
    assert out["SECOND/USDT"]["confidence"] == 33
    assert out["FIRST/USDT"]["confidence"] == 88


def test_batch_accepts_an_envelope_object(monkeypatch):
    """Models drift between a bare array and {"results": [...]}. Reshaping is
    cheaper than burning a retry."""
    bundles = _batch_bundles("X/USDT")
    out = ai_decision.parse_batch_response(
        json.dumps({"results": [_decision("X/USDT")]}), bundles)
    assert set(out) == {"X/USDT"}


def test_batch_ignores_extra_elements(monkeypatch):
    bundles = _batch_bundles("REAL/USDT")
    out = ai_decision.parse_batch_response(
        json.dumps([_decision("REAL/USDT"), _decision("HALLUCINATED/USDT")]), bundles)
    assert set(out) == {"REAL/USDT"}


def test_an_all_bad_batch_is_retried(monkeypatch):
    """A partial answer is fine, but zero usable elements means the body was
    unusable — worth another attempt before dropping every coin to fallback."""
    bundles = _batch_bundles("A/USDT", "B/USDT")
    good = _content_resp(json.dumps([_decision("A/USDT"), _decision("B/USDT")]))
    state = _counting_post(monkeypatch, [_content_resp("not json at all"), good])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(bundles)
    assert set(out) == {"A/USDT", "B/USDT"}
    assert state["n"] == 2


def test_batch_failure_returns_empty_not_a_raise(monkeypatch):
    """nemotron_decisions never substitutes a fallback itself — the caller must
    see which symbols went un-decided and decide."""
    bundles = _batch_bundles("A/USDT", "B/USDT")
    _counting_post(monkeypatch, [_Resp(status=502, text="down")])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    assert ai_decision.nemotron_decisions(bundles) == {}


def test_no_candidates_costs_no_request(monkeypatch):
    state = _counting_post(monkeypatch, [_content_resp("[]")])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    assert ai_decision.nemotron_decisions([]) == {}
    assert state["n"] == 0


def test_single_candidate_uses_the_single_prompt(monkeypatch):
    """One candidate does not need array framing."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["prompt"] = json["messages"][1]["content"]
        return _content_resp(json_dumps_single())

    def json_dumps_single():
        return json.dumps({"signal": "BUY", "entry": 100, "stop_loss": 98,
                           "take_profit": 104, "rr": 2.0, "confidence": 70, "reason": "ok"})

    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    out = ai_decision.nemotron_decisions(_batch_bundles("SOLO/USDT"))
    assert set(out) == {"SOLO/USDT"}
    assert "JSON ARRAY" not in captured["prompt"]


# ----------------------------------------------------- Phase 3: daily budget
def test_budget_counts_requests_not_candidates(monkeypatch):
    bundles = _batch_bundles(*[f"S{i}/USDT" for i in range(5)])
    _counting_post(monkeypatch, [_content_resp(
        json.dumps([_decision(b["symbol"]) for b in bundles]))])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    ai_decision.nemotron_decisions(bundles)
    st = ai_decision.budget_status()
    assert st["used"] == 1 and st["remaining"] == config.AI_DAILY_BUDGET - 1


def test_exhausted_budget_stops_calling(monkeypatch):
    """Past the cap OpenRouter answers 429 anyway; not sending the request keeps
    the scan fast and the log honest."""
    monkeypatch.setattr(config, "AI_DAILY_BUDGET", 1)
    good = _content_resp(json.dumps(
        {"signal": "BUY", "entry": 100, "stop_loss": 98, "take_profit": 104,
         "rr": 2.0, "confidence": 70, "reason": "ok"}))
    state = _counting_post(monkeypatch, [good])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    ai_decision.nemotron_decision(_bundle())            # spends the only request
    with pytest.raises(ai_decision.AIDecisionError, match="budget exhausted"):
        ai_decision.nemotron_decision(_bundle())
    assert state["n"] == 1


def test_exhausted_notice_is_sent_once(monkeypatch):
    """Yasir needs to know alerts have gone indicator-only — once, not every scan."""
    monkeypatch.setattr(config, "AI_DAILY_BUDGET", 1)
    good = _content_resp(json.dumps(
        {"signal": "HOLD", "entry": None, "stop_loss": None, "take_profit": None,
         "rr": None, "confidence": 0, "reason": "ok"}))
    _counting_post(monkeypatch, [good])
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    assert ai_decision.budget_exhausted_notice() is None   # nothing spent yet
    ai_decision.nemotron_decision(_bundle())
    notice = ai_decision.budget_exhausted_notice()
    assert notice is not None and "indicator-only" in notice
    assert ai_decision.budget_exhausted_notice() is None   # never repeated


def test_budget_rolls_over_at_ist_midnight(monkeypatch):
    from datetime import date
    monkeypatch.setattr(config, "AI_DAILY_BUDGET", 1)
    assert ai_decision._budget.consume() is True
    assert ai_decision._budget.consume() is False
    # Simulate the IST date advancing
    ai_decision._budget._day = date(2020, 1, 1)
    assert ai_decision._budget.consume() is True


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
