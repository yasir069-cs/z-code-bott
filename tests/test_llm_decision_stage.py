"""LLM decision stage: shortlisted coins -> model verdict -> deterministic gates.

The pipeline: funnel -> decide() (full structured data) -> LLM LONG/SHORT/
NO_TRADE -> the SAME deterministic validation (direction gate, risk gate,
quality, 65 alert floor). An LLM verdict can rescue a setup the core would
reject, or veto one it would take — but it can never bypass a safety gate.
"""
import numpy as np

import config
import decision
import ai_decision
from ai_decision import parse_verdict, AIDecisionError
from main import (_apply_llm_verdict, _build_decision_bundle, _emission_kind,
                  _SIGNAL_MAP)


def _candles(controls, **kw):
    from conftest import make_candles
    closes = []
    for a, b in zip(controls[:-1], controls[1:]):
        closes.extend(np.linspace(a, b, 5, endpoint=False))
    closes.append(controls[-1])
    return make_candles(closes, wick=kw.pop("wick", 0.3), **kw)


def _decide(controls):
    df = _candles(controls)
    frames = {config.TF_HTF: df, config.TF_SETUP: df, config.TF_ENTRY: df}
    return decision.decide(frames, funding_rate=-0.0001,
                           symbol="TEST/USDT:USDT")


# --------------------------------------------------------- verdict parsing

def test_verdict_parser_maps_both_vocabularies():
    for raw, want in [("LONG", "LONG"), ("BUY", "LONG"), ("SHORT", "SHORT"),
                      ("SELL", "SHORT"), ("NO_TRADE", "NO_TRADE"),
                      ("HOLD", "NO_TRADE")]:
        out = parse_verdict(f'{{"signal": "{raw}", "confidence": 70, "reason": "r"}}')
        assert out["signal"] == want
    assert parse_verdict('{"signal":"LONG","confidence":150,"reason":"r"}')["confidence"] == 100.0


def test_verdict_parser_rejects_garbage():
    for bad in ('{"signal": "MOON"}', '{"signal": null}', 'not json',
                '[]'):
        try:
            parse_verdict(bad)
            raise AssertionError(f"accepted garbage: {bad}")
        except AIDecisionError:
            pass


def test_verdict_parser_handles_fenced_json():
    out = parse_verdict('```json\n{"signal": "LONG", "confidence": 66, "reason": "ok"}\n```')
    assert out["signal"] == "LONG" and out["reason"] == "ok"


# ------------------------------------------------- applying verdicts (the gates)

DOWNTREND = [140, 135, 138, 130, 133, 128, 131, 126, 122, 116, 110, 104]
UPTREND = [130, 126, 131, 101, 107, 104, 110, 107, 112, 111, 112.5]


def _verdict(signal, reason="model reason"):
    return {"signal": signal, "confidence": 70.0, "reason": reason, "ai_used": True}


def test_llm_no_trade_verdict_vetoes_any_deterministic_signal():
    d = _decide(UPTREND)                      # deterministic LONG
    assert d["decision"] == "LONG"
    out, signal, ai_used, reason = _apply_llm_verdict(d, _verdict("NO_TRADE"), "T")
    assert signal == "HOLD" and ai_used is True
    assert out["decision"] == "NO_TRADE"
    assert "llm_no_trade" in out["no_trade_reasons"]
    assert reason == "model reason"


def test_llm_confirming_verdict_keeps_signal():
    d = _decide(UPTREND)
    out, signal, ai_used, _ = _apply_llm_verdict(d, _verdict("LONG"), "T")
    assert signal == "BUY" and out is d and ai_used is True


def test_llm_confirming_a_core_rejected_direction_still_validates():
    """The LLM confirms the SAME direction the core proposed but REJECTED
    (exhaustion/quality): the hard gates must still re-validate it — a
    confirming verdict cannot rescue a setup the math rejects."""
    d = _decide(DOWNTREND)                    # core direction=SHORT, NO_TRADE
    assert d["direction"] == "SHORT" and d["decision"] == "NO_TRADE"
    out, signal, ai_used, _ = _apply_llm_verdict(d, _verdict("SHORT"), "T")
    assert ai_used is True
    assert out is not d                       # post_llm_validate ran
    assert signal == "HOLD"                   # gates still reject the short
    assert out["decision"] == "NO_TRADE"
    assert "llm_no_trade" not in (out.get("no_trade_reasons") or [])


def test_absent_verdict_leaves_deterministic_result():
    d = _decide(DOWNTREND)                    # exhausted short -> NO_TRADE
    out, signal, ai_used, reason = _apply_llm_verdict(d, None, "T")
    assert signal == "HOLD" and ai_used is False and reason is None
    assert out is d


def test_llm_flip_is_validated_by_post_llm_gates():
    """LLM flips a deterministic LONG to SHORT: post_llm_validate runs the
    hard gates for SHORT — the flip only stands if they confirm it (here the
    1H structure is bullish, so SHORT fails the quality floor)."""
    d = _decide(UPTREND)                      # deterministic LONG
    assert d["decision"] == "LONG"
    out, signal, ai_used, _ = _apply_llm_verdict(d, _verdict("SHORT"), "T")
    assert ai_used is True
    assert out["direction"] == "SHORT"        # re-validated for the LLM's direction
    assert signal == "HOLD"                   # ...but the gates rejected it
    assert out["decision"] == "NO_TRADE"
    assert out["no_trade_reasons"]            # with explicit gate reasons


def test_post_llm_gates_are_hard_safety_only():
    """The post-LLM reasons must be hard-safety gates (data/levels/stop/RR/
    quality) — never opinion vetoes like counter_htf or directional_conflict
    (those live inside the quality computation as score discounts)."""
    d = _decide(UPTREND)
    out = decision.post_llm_validate(d, "SHORT")
    assert out["decision"] == "NO_TRADE"
    assert out["no_trade_reasons"]
    for r in out["no_trade_reasons"]:
        assert r not in ("counter_htf", "directional_conflict"), r


def test_post_llm_validate_confirming_direction_passes():
    d = _decide(UPTREND)
    out = decision.post_llm_validate(d, "LONG")
    assert out["decision"] == "LONG"
    assert out["no_trade_reasons"] == []


def test_post_llm_validate_rejects_invalid_direction():
    d = _decide(UPTREND)
    out = decision.post_llm_validate(d, "GARBAGE")
    assert out["decision"] == "NO_TRADE"
    assert "invalid_direction" in out["no_trade_reasons"]


def test_flip_rejection_reasons_are_deterministic_not_llm_opinion():
    d = _decide(UPTREND)
    out, _, _, _ = _apply_llm_verdict(d, _verdict("SHORT"), "T")
    # every reason is a real gate verdict, never a bare "llm said no"
    assert all(r != "llm_no_trade" for r in out["no_trade_reasons"])


# ------------------------------------------------------- emission rule (tiers)

def test_emission_rule():
    """Owner's tier system: below 50 ignored (log-only), 50+ alerts."""
    assert _emission_kind("HOLD", 90.0) == "hold"
    assert _emission_kind("BUY", 29.9) == "log_only"
    assert _emission_kind("SELL", 29.99) == "log_only"
    assert _emission_kind("BUY", 30.0) == "alert"
    assert _emission_kind("SELL", 35.0) == "alert"
    assert _emission_kind("BUY", 45.0) == "alert"
    assert _emission_kind("SELL", 80.0) == "alert"


def test_alert_quality_tiers_from_config():
    """The tier boundaries live in config: 30 floor, NORMAL 40-50, HIGH 50-60,
    STRONG 60+. The no-sweep cap sits just below STRONG."""
    assert config.ALERT_QUALITY_MIN == 30
    assert config.ALERT_TIER_NORMAL_MIN == 40
    assert config.ALERT_TIER_HIGH_MIN == 50
    assert config.ALERT_TIER_STRONG_MIN == 60
    assert config.QUALITY_MIN == 42
    assert config.NO_SWEEP_CONFIDENCE_CAP < config.ALERT_TIER_STRONG_MIN


def test_alert_tier_labels():
    """alerts._conf_label maps the confidence to the tier word shown in the
    Telegram message."""
    import alerts
    assert alerts._conf_label(75.0)[1] == "STRONG"
    assert alerts._conf_label(60.0)[1] == "STRONG"
    assert alerts._conf_label(59.9)[1] == "HIGH"
    assert alerts._conf_label(50.0)[1] == "HIGH"
    assert alerts._conf_label(49.9)[1] == "NORMAL"
    assert alerts._conf_label(40.0)[1] == "NORMAL"
    assert alerts._conf_label(35.0)[1] == "LOW"     # below floor: defensive only
    assert alerts._conf_label("strong")[1] == "STRONG"


# --------------------------------------------------- the full-data bundle

def test_decision_bundle_carries_full_structured_data():
    d = _decide(UPTREND)
    snap = d["snaps"]["5m"]
    cand = {"symbol": "TEST/USDT:USDT", "direction": "BUY", "fr": -0.0001,
            "feat_1h": {"sweep": None}}
    bundle = _build_decision_bundle(cand, d, snap, last_price=None)
    assert bundle is not None
    assert bundle["deterministic"]["direction"] == "LONG"
    assert bundle["deterministic"]["setup_quality"] == d["setup_quality"]
    assert bundle["ind_1h"] is d["snaps"]["1h"]
    assert "liquidation" in bundle
    prompt = ai_decision.build_decision_prompt(bundle)
    # the model sees the factual evidence and the verdict instruction
    assert "LONG|SHORT|NO_TRADE" in prompt
    assert "MEASURED MARKET FACTS" in prompt
    assert "Calculated structural levels" in prompt
    assert "Structure (setup TF)" in prompt
    assert "1H range position" in prompt
    assert "WEBSOCKET LIQUIDATION DATA" in prompt
    assert "Liquidation data is context only" in prompt


def test_prompt_contains_no_python_verdict_or_reasons():
    """THE anti-rubber-stamp test: the LLM must never see Python's
    preliminary NO_TRADE verdict, its reasons, its quality scores, its
    penalty narratives or the funnel's directional hint — anything Python
    'decided' would bias the model before it evaluates the data itself."""
    d = _decide(DOWNTREND)               # core rejected this exhausted short
    assert d["decision"] == "NO_TRADE" and d["no_trade_reasons"]
    cand = {"symbol": "TEST/USDT:USDT", "direction": "SELL", "fr": None,
            "feat_1h": {"sweep": None}}
    bundle = _build_decision_bundle(cand, d, d["snaps"]["5m"], None)
    assert bundle is not None
    prompt = ai_decision.build_decision_prompt(bundle)
    for banned in ("Python core direction", "Python NO_TRADE reasons",
                   "Setup quality:", "Quality penalties", "MTF notes",
                   "insufficient_primary_evidence", "poor_rr",
                   "low_setup_quality", "directional_conflict",
                   "counter_htf", "Funnel direction", "REJECTED",
                   "penalized"):
        assert banned not in prompt, f"prompt leaks Python verdict: {banned}"
    # the batched prompt is equally clean
    batch_prompt = ai_decision._build_batch_decision_prompt([bundle])
    for banned in ("Python core", "no_trade_reasons", "Setup quality",
                   "penalized", "Funnel direction"):
        assert banned not in batch_prompt, f"batch prompt leaks: {banned}"


def test_prompt_instructions_declare_independence():
    d = _decide(UPTREND)
    cand = {"symbol": "TEST/USDT:USDT", "direction": "BUY", "fr": None,
            "feat_1h": {"sweep": None}}
    bundle = _build_decision_bundle(cand, d, d["snaps"]["5m"], None)
    prompt = ai_decision.build_decision_prompt(bundle)
    assert "NO preliminary verdict" in prompt
    assert "first decision-maker" in prompt


def test_bundle_skips_incomplete_snapshots():
    d = _decide(UPTREND)
    d2 = dict(d)
    d2["snaps"] = {}                          # snapshots missing
    cand = {"symbol": "T", "direction": "BUY", "fr": None}
    assert _build_decision_bundle(cand, d2, None, None) is None


# --------------------------------------------------- verdict batching

def test_llm_verdicts_batch_parse(monkeypatch):
    """The batched verdict parser matches symbols first, positionally second."""
    bundles = [
        {"symbol": "A/USDT:USDT", "deterministic": {"setup_quality": 70}},
        {"symbol": "B/USDT:USDT", "deterministic": {"setup_quality": 60}},
    ]
    content = ('[{"symbol": "B/USDT:USDT", "signal": "NO_TRADE", "confidence": 60,'
               ' "reason": "b"}, {"signal": "LONG", "confidence": 80, "reason": "a"}]')
    out = ai_decision._parse_verdict_batch(content, bundles)
    assert out["A/USDT:USDT"]["signal"] == "LONG"
    assert out["B/USDT:USDT"]["signal"] == "NO_TRADE"


def test_llm_verdicts_empty_bundles(monkeypatch):
    assert ai_decision.llm_verdicts([]) == {}


def test_llm_verdicts_unusable_batch_falls_back(monkeypatch):
    """A transport failure means no verdicts — no fabricated answers; the
    caller (run_scan) proceeds deterministically."""
    d = _decide(UPTREND)
    cand = {"symbol": "TEST/USDT:USDT", "direction": "BUY", "fr": None,
            "feat_1h": {"sweep": None}}
    bundles = [_build_decision_bundle(cand, d, d["snaps"]["5m"], None)]
    assert bundles[0] is not None

    def boom(*a, **k):
        raise AIDecisionError("transport down")

    monkeypatch.setattr(ai_decision, "_complete", boom)
    out = ai_decision.llm_verdicts(bundles)
    assert out == {}                          # no fabricated verdicts


# --------------------------------------- deterministic path stays authoritative

def test_deterministic_decision_unchanged_by_stage():
    """The core's own decision must survive the stage untouched when the LLM
    is disabled or unavailable — decide() output is the fallback everywhere."""
    d = _decide(UPTREND)
    signal = _SIGNAL_MAP.get(d["decision"], "HOLD")
    assert signal == "BUY"                    # the funnel's LONG still alerts
    out, s, ai_used, _ = _apply_llm_verdict(d, None, "T")
    assert out is d and s == signal and ai_used is False
