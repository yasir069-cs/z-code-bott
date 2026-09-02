"""Structured-output contract tests (ai_decision).

The live box had a working API key, an HTTP 200 and still produced `ai_used=False`
on every row: the model answered with analysis prose, the extractor only knew how
to strip a fence at the very start and how to slice first-`[`-to-last-`]`, and the
retry ladder then replayed the identical doomed request three times per model,
spending the daily budget on it. Everything below pins one half of that fix:
the reply is *tolerated* (prose and fences around the JSON are skipped, a
truncated block is cut back to its last complete element), the contract is
*enforced* (the provider is asked to constrain output to a JSON object), and a
parse failure is retried differently from a transport failure.
"""
import json

import pytest
import requests as _requests

import ai_decision
import config


@pytest.fixture(autouse=True)
def _quiet_retry_ladder(monkeypatch):
    """No sleeps, a fresh budget, and the provider capabilities restored —
    `_CAPS` is process state and a test that disables json mode must not leak."""
    monkeypatch.setattr(ai_decision, "_backoff_sleep", lambda *a, **k: None)
    ai_decision.reset_budget()
    ai_decision._reset_provider_caps()
    yield
    ai_decision.reset_budget()
    ai_decision._reset_provider_caps()


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


def _content_resp(content, finish=None, reasoning=None):
    choice = {"message": {"content": content}}
    if finish:
        choice["finish_reason"] = finish
    if reasoning:
        choice["message"]["reasoning"] = reasoning
    return _Resp(payload={"choices": [choice]})


def _serve(monkeypatch, responses):
    """Serve `responses` in order (repeating the last), recording every payload."""
    state = {"n": 0, "payloads": [], "messages": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        i = min(state["n"], len(responses) - 1)
        state["n"] += 1
        state["payloads"].append(dict(json or {}))
        state["messages"].append(list((json or {}).get("messages") or []))
        return responses[i]

    monkeypatch.setattr(ai_decision.requests, "post", fake_post)
    return state


def _bundle(symbol="A/USDT:USDT", **over):
    base = dict(symbol=symbol, direction="BUY", current_price=100.0, entry_price=99.8,
                ind_1h={"rsi": 55.0, "rsi_prev": 51.0, "ema21": 99.0, "vwap": 99.3,
                        "bb_lower": 99.1, "bb_mid": 100.0, "bb_upper": 101.2, "close": 99.8,
                        "atr": 0.8, "range_pos": 0.2, "swing_low_20": 98.5,
                        "swing_high_20": 103.0, "volume_trend": [900, 950, 1000, 1100, 1300]},
                ind_15m={"rsi": 57.0, "rsi_prev": 54.0, "ema21": 99.2, "vwap": 99.4,
                         "bb_lower": 99.2, "bb_mid": 100.0, "bb_upper": 101.0, "close": 99.9,
                         "atr": 0.4, "volume_trend": [900, 950, 1000, 1100, 1200]},
                ind_5m={"rsi": 56.0, "rsi_prev": 51.0, "rsi_history": [48, 50, 55, 51, 56],
                        "ema21": 99.4, "vwap": 99.6, "bb_lower": 99.5, "bb_mid": 100.0,
                        "bb_upper": 100.8, "close": 99.8, "atr": 0.2,
                        "volume_trend": [900, 950, 1000, 1100, 1250]},
                sweep=None, confirm_score=4, confluence=60.0,
                deterministic={"setup_quality": 60.0, "htf_bias": "LONG",
                               "structure": {}, "sr": {}, "liquidity": {},
                               "price_action": {}, "trendline": {}, "futures": {},
                               "risk": {}, "mtf": {}, "data_warnings": [],
                               "funding_rate": None})
    base.update(over)
    return base


def _verdict(symbol, signal="LONG", confidence=70):
    return {"symbol": symbol, "signal": signal, "confidence": confidence,
            "reason": f"{symbol} structure holds"}


# ------------------------------------------------------------- tolerant parsing

def test_prose_before_and_after_the_array_no_longer_eats_the_answer():
    """The exact live shape: an intro sentence with a bracketed reference, a
    fenced array, then a closing note with another bracket."""
    reply = ("Sure — I weighed each setup. My notes [1] on structure:\n"
             "```json\n" + json.dumps([_verdict("A/USDT:USDT"),
                                       _verdict("B/USDT:USDT", "NO_TRADE", 20)]) + "\n```\n"
             "See reference [2] for the liquidation caveat.")
    parsed = ai_decision._extract_json_array(reply)
    assert [item["symbol"] for item in parsed] == ["A/USDT:USDT", "B/USDT:USDT"]


def test_bare_array_and_envelope_object_are_both_accepted():
    payload = json.dumps([_verdict("A/USDT:USDT")])
    assert len(ai_decision._extract_json_array(payload)) == 1
    assert len(ai_decision._extract_json_array(json.dumps({"decisions": [_verdict("A")]}))) == 1
    assert len(ai_decision._extract_json_array(json.dumps({"results": [_verdict("A")]}))) == 1


def test_single_object_reply_to_a_one_setup_batch_is_not_lost():
    parsed = ai_decision._extract_json_array(json.dumps(_verdict("A/USDT:USDT")))
    assert parsed == [_verdict("A/USDT:USDT")]


def test_brackets_inside_a_reason_string_do_not_mis_slice_the_json():
    reply = 'Verdict: ' + json.dumps({"symbol": "A", "signal": "LONG", "confidence": 70,
                                      "reason": "sweep at [1] then retest [2]"}) + " done"
    obj = ai_decision._extract_json(reply)
    assert obj["reason"] == "sweep at [1] then retest [2]"


def test_fences_anywhere_are_stripped():
    fenced = "```json\n" + json.dumps(_verdict("A")) + "\n```"
    assert ai_decision._extract_json(fenced)["signal"] == "LONG"
    mid = "answer:\n```\n" + json.dumps(_verdict("A")) + "\n```\nthanks"
    assert ai_decision._extract_json(mid)["signal"] == "LONG"


def test_a_truncated_batch_is_cut_back_to_its_complete_elements():
    """`max_tokens` ran out mid-object. Keep the verdicts that fully arrived and
    report the truncation — the missing setup takes the deterministic path, it is
    never filled in by guesswork."""
    complete = json.dumps([_verdict("A/USDT:USDT"), _verdict("B/USDT:USDT")])
    reply = complete[:-1] + '{"symbol":"C/USDT:USDT","signal":"NO_TRA'   # cut mid-object
    meta: dict = {}
    parsed = ai_decision._extract_json_array(reply, meta=meta)
    assert [item["symbol"] for item in parsed] == ["A/USDT:USDT", "B/USDT:USDT"]
    assert meta.get("truncated") and meta.get("salvaged")
    assert isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed)


def test_reply_with_no_json_at_all_is_a_parse_failure_not_a_verdict():
    with pytest.raises(ai_decision.AIDecisionError) as exc:
        ai_decision._extract_json_array("Let me think about this market carefully. "
                                        "Price is near resistance so I would wait.")
    assert exc.value.retryable and exc.value.parse_failure
    assert "prose/markdown" in str(exc.value)


def test_batch_prompt_demands_one_object_and_names_the_envelope():
    prompt = ai_decision._build_batch_decision_prompt([_bundle("A/USDT:USDT"),
                                                        _bundle("B/USDT:USDT")])
    assert '"decisions"' in prompt
    assert "exactly 2 objects" in prompt
    assert "no text before the first" in prompt


# ------------------------------------------------- json mode + the retry ladder

def test_json_mode_is_requested_for_verdicts_and_never_for_chat(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    state = _serve(monkeypatch, [_content_resp(json.dumps(
        {"decisions": [_verdict("A/USDT:USDT")]}))])
    ai_decision.llm_verdicts([_bundle("A/USDT:USDT")])
    assert state["payloads"][0]["response_format"] == {"type": "json_object"}


def test_a_single_setup_answered_in_batch_shape_is_still_understood(monkeypatch):
    """One-setup scans skip batching; a model that still wraps its reply in
    `{"decisions": [ … ]}` must not be thrown away for punctuation."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    _serve(monkeypatch, [_content_resp(json.dumps(
        {"decisions": [_verdict("A/USDT:USDT", "SHORT", 64)]}))])
    out = ai_decision.llm_verdicts([_bundle("A/USDT:USDT")])
    assert out["A/USDT:USDT"]["signal"] == "SHORT"
    assert out["A/USDT:USDT"]["ai_used"] is True

    chat = _serve(monkeypatch, [_content_resp("a plain prose answer")])
    ai_decision.complete_chat([{"role": "user", "content": "hi"}])
    assert "response_format" not in chat["payloads"][0]


def test_a_prose_reply_is_retried_with_a_correction_and_more_tokens(monkeypatch):
    """The retry must change: the same prompt earns the same prose, and on the live
    box each repetition cost another unit of the daily budget."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    good = _content_resp(json.dumps({"decisions": [_verdict("A/USDT:USDT"),
                                                    _verdict("B/USDT:USDT", "NO_TRADE", 15)]}))
    state = _serve(monkeypatch, [_content_resp("I will analyse each setup in turn. "
                                               "First, the range...", finish="length"), good])
    out = ai_decision.llm_verdicts([_bundle("A/USDT:USDT"), _bundle("B/USDT:USDT")])

    assert state["n"] == 2
    assert {k: v["signal"] for k, v in out.items()} == {"A/USDT:USDT": "LONG",
                                                         "B/USDT:USDT": "NO_TRADE"}
    assert state["payloads"][1]["max_tokens"] > state["payloads"][0]["max_tokens"]
    assert state["payloads"][1]["max_tokens"] <= config.AI_MAX_TOKENS_RETRY_CAP
    correction = state["messages"][1][-1]
    assert correction["role"] == "user" and "JSON payload ONLY" in correction["content"]
    assert state["messages"][1][:-1] == state["messages"][0]     # history preserved


def test_transport_failure_is_retried_without_changing_the_request(monkeypatch):
    """A 502 is not a disobedient model: no correction turn, no token escalation."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    good = _content_resp(json.dumps({"decisions": [_verdict("A/USDT:USDT")] }))
    state = _serve(monkeypatch, [_Resp(status=503, text="upstream down"), good])
    out = ai_decision.llm_verdicts([_bundle("A/USDT:USDT"), _bundle("B/USDT:USDT")])
    assert "A/USDT:USDT" in out
    assert state["payloads"][1]["max_tokens"] == state["payloads"][0]["max_tokens"]
    assert all(len(msgs) == 2 for msgs in state["messages"])


def test_gateway_that_rejects_response_format_falls_back_once_and_remembers(monkeypatch):
    """`json_object` is not universal. A 400 naming the field costs one retry, then
    the capability stays off for the process instead of failing every scan."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    rejected = _Resp(status=400, text='{"error":"Unsupported parameter: response_format"}')
    good = _content_resp(json.dumps({"decisions": [_verdict("A/USDT:USDT"),
                                                    _verdict("B/USDT:USDT")]}))
    state = _serve(monkeypatch, [rejected, good])

    out = ai_decision.llm_verdicts([_bundle("A/USDT:USDT"), _bundle("B/USDT:USDT")])
    assert "A/USDT:USDT" in out
    assert "response_format" not in state["payloads"][1]
    assert ai_decision.provider_caps()["json_object"] is False

    again = _serve(monkeypatch, [good])
    ai_decision.llm_verdicts([_bundle("A/USDT:USDT"), _bundle("B/USDT:USDT")])
    assert "response_format" not in again["payloads"][0]
    assert again["n"] == 1, "a disabled capability must not be probed again"


def test_json_mode_can_be_turned_off_from_config(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(config, "AI_JSON_MODE", False)
    state = _serve(monkeypatch, [_content_resp(json.dumps(
        {"decisions": [_verdict("A/USDT:USDT")]}))])
    ai_decision.llm_verdicts([_bundle("A/USDT:USDT")])
    assert "response_format" not in state["payloads"][0]


def test_empty_and_truncated_replies_are_distinguishable_in_the_logs(monkeypatch, caplog):
    """`finish_reason=length` on an empty body used to read like a provider fault;
    the message now says the reply was cut off, which is the actionable half."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "sk-or-test")
    _serve(monkeypatch, [_Resp(payload={"choices": [{"message": {"content": ""},
                                                     "finish_reason": "length"}]})])
    with caplog.at_level("WARNING"):
        out = ai_decision.llm_verdicts([_bundle("A/USDT:USDT")])
    assert out == {}                      # no verdict, no fabrication
    assert "empty content" in caplog.text


# ------------------------------------------------------- audit outcome reporting

def test_audit_line_reports_answered_counts_and_that_nothing_was_applied(tmp_path, monkeypatch,
                                                                        caplog):
    """The one INFO line that answers "did the model answer, and was it used?"."""
    import csv  # noqa: F401  (the worker writes the CSV; this test only reads the log)
    import scan_coordinator

    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE", tmp_path / "ai_opinions.csv")
    records = [{"symbol": "A/USDT:USDT", "signal_id": "s-A",
                 "deterministic_decision": "LONG"},
               {"symbol": "B/USDT:USDT", "signal_id": "s-B",
                "deterministic_decision": "NO_TRADE"},
               {"symbol": "C/USDT:USDT", "signal_id": "s-C",
                "deterministic_decision": "NO_TRADE"}]
    verdicts = {"A/USDT:USDT": {"signal": "LONG", "confidence": 70, "reason": "ok"},
                "B/USDT:USDT": {"signal": "LONG", "confidence": 61, "reason": "would alert"}}
    worker = scan_coordinator.AIOpinionWorker(lambda bundles: verdicts)

    with caplog.at_level("INFO"):
        worker._run("scan-9", records, [{"symbol": r["symbol"]} for r in records])

    line = next(rec.message for rec in caplog.records if "AI AUDIT" in rec.message)
    assert "2/3 answered" in line
    assert "LONG 2 SHORT 0 NO_TRADE 0" in line
    assert "applied=never (audit-only)" in line
    assert "json_mode=" in line
    assert worker.status()["answered"] == 2 and worker.status()["expected"] == 3
    assert worker.status()["tally"]["SIGNAL_PROPOSED"] == 1


def test_budget_and_caps_defaults_leave_room_for_a_full_batch():
    assert config.AI_JSON_MODE is True
    assert config.AI_MAX_TOKENS_RETRY_CAP > config.AI_MAX_TOKENS
    assert config.AI_BATCH_MAX == 20


def test_a_pathological_reply_fails_cleanly_instead_of_blowing_up():
    """A model that emits tens of thousands of nested brackets makes the JSON
    *decoder* recurse past its limit. That must read as an unparseable answer —
    the audit worker then keeps the deterministic verdict — not as a
    RecursionError escaping into the thread."""
    nested = "[" * 5000 + "]" * 5000
    with pytest.raises(ai_decision.AIDecisionError) as exc:
        ai_decision._extract_json_array(nested)
    assert exc.value.parse_failure

    real = json.dumps({"decisions": [_verdict("A/USDT:USDT")]})
    assert ai_decision._extract_json_array("[" * 30000 + " " + real)
