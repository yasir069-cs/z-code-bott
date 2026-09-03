"""OpenRouter connectivity diagnosis — runs Tests A-D in order and prints
the REAL error bodies (never the API key):

  A: minimal request, NO reasoning      -> basic connectivity + model validity
  B: minimal request, WITH reasoning    -> is reasoning the breaker?
  C: full crypto prompt, NO reasoning   -> prompt size / JSON compliance
  D: full crypto prompt, WITH reasoning -> the production configuration
  E: full crypto prompt, bare UA        -> does the provider WAF still challenge
                                            python-requests? Production sends a
                                            browser-like UA; E drops it again.
  F: production BATCH prompt + JSON mode  -> the live failure: 200 OK with prose
  G: production BATCH prompt, no mode     -> is response_format what fixes it?
  F/G are parsed with the production extractor, so "HTTP 200 but ai_used=False"
  is diagnosed here instead of in a scan log: parse status, verdict counts, and
  whether the reply was truncated at max_tokens.

Usage: python scripts/openrouter_diagnose.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

import ai_decision
import config
import scanner
from indicators import compute_indicators

MODEL = config.AI_MODEL


def check_key_hygiene() -> None:
    key = config.OPENROUTER_API_KEY
    no_quotes = bool(key) and not key.startswith(("\"", "'"))
    no_newline = bool(key) and ("\n" not in key and "\r" not in key)
    print("=== API key hygiene ===")
    print(f".env loaded            : {bool(key)}")
    print(f"key present            : {len(key) > 0}")
    print(f"key prefix ok          : {key.startswith('sk-or-') if key else '-'}")
    print(f"no surrounding spaces  : {key == key.strip() if key else '-'}")
    print(f"no quotes              : {no_quotes}")
    print(f"no newline inside      : {no_newline}")
    print(f"key length             : {len(key)} chars")
    gitignore = (config.BASE_DIR / ".gitignore").read_text(encoding="utf-8")
    print(f".env in .gitignore     : {'.env' in gitignore}")


def run_test(name: str, messages: list, reasoning_enabled: bool | None,
             *, bare_ua: bool = False, json_mode: bool = False) -> dict:
    payload = {"model": MODEL, "messages": messages, "max_tokens": config.AI_MAX_TOKENS}
    if reasoning_enabled is not None:
        payload["reasoning"] = {"enabled": reasoning_enabled}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # Same headers the bot sends, so a pass here means a pass in production.
    # bare_ua drops the User-Agent to reproduce the pre-fix WAF challenge.
    headers = dict(ai_decision.provider_headers())
    if bare_ua:
        headers.pop("User-Agent", None)
    print(f"\n=== Test {name} ===")
    print(f"Model: {MODEL}")
    print(f"Reasoning: {payload.get('reasoning', 'omitted')}")
    print(f"User-Agent: {headers.get('User-Agent', 'python-requests (bare)')}")
    print(f"response_format: {payload.get('response_format', 'omitted')}")
    try:
        response = requests.post(
            ai_decision.chat_completions_url(),   # the endpoint production uses
            headers=headers,
            json=payload,
            timeout=config.AI_TIMEOUT_SECONDS,
        )
        print(f"HTTP status: {response.status_code}")
        if response.status_code != 200:
            print(f"OpenRouter error: {response.text[:800]}")
            print("Success/Failed: FAILED")
            return {"ok": False, "status": response.status_code}
        data = response.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        print(f"finish_reason: {choice.get('finish_reason')}")
        print(f"content ({len(content)} chars): {content[:300]!r}")
        print(f"Success/Failed: {'OK' if content else 'EMPTY CONTENT'}")
        return {"ok": bool(content), "status": 200, "content": content,
                "finish": choice.get("finish_reason")}
    except requests.exceptions.RequestException as exc:
        print(f"OpenRouter error: {exc!r}")
        print("Success/Failed: FAILED")
        return {"ok": False, "status": None}


def build_real_bundles(symbols=("BTC/USDT", "ETH/USDT")) -> list:
    """The bundles the production batch prompt is built from, on live data."""
    exchange = scanner.make_exchange()
    return [_bundle_from(exchange, symbol) for symbol in symbols]


def _bundle_from(exchange, symbol: str) -> dict:
    frames = scanner.fetch_all_timeframes(exchange, symbol)
    ind_1h, ind_15m, ind_5m = (compute_indicators(frames[tf]) for tf in ("1h", "15m", "5m"))
    sweep = {"direction": "BUY", "age_candles": 2, "level": ind_1h["swing_low_20"],
             "wick": ind_1h["atr"] * 1.5, "body": ind_1h["atr"] * 0.5,
             "wick_body_ratio": 3.0, "volume_ratio": 1.8}
    return {"symbol": symbol, "direction": "BUY",
            "current_price": ind_5m["close"], "entry_price": ind_5m["close"],
            "ind_1h": ind_1h, "sweep": sweep, "ind_15m": ind_15m,
            "confirm_score": 4, "ind_5m": ind_5m,
            # the audit stage sends measured facts, never the Python verdict
            "deterministic": {"setup_quality": 60.0, "htf_bias": "LONG",
                              "structure": {}, "sr": {}, "liquidity": {},
                              "price_action": {}, "trendline": {}, "futures": {},
                              "risk": {}, "mtf": {}, "data_warnings": [],
                              "funding_rate": None}}


def build_real_bundle() -> dict:
    exchange = scanner.make_exchange()
    frames = scanner.fetch_all_timeframes(exchange, "BTC/USDT")
    ind_1h, ind_15m, ind_5m = (compute_indicators(frames[tf]) for tf in ("1h", "15m", "5m"))
    sweep = {"direction": "BUY", "age_candles": 2, "level": ind_1h["swing_low_20"],
             "wick": ind_1h["atr"] * 1.5, "body": ind_1h["atr"] * 0.5,
             "wick_body_ratio": 3.0, "volume_ratio": 1.8}
    return {"symbol": "BTC/USDT", "direction": "BUY",
            "current_price": ind_5m["close"], "entry_price": ind_5m["close"],
            "ind_1h": ind_1h, "sweep": sweep, "ind_15m": ind_15m,
            "confirm_score": 4, "ind_5m": ind_5m}


def main() -> None:
    config.setup_logging()
    check_key_hygiene()

    minimal = [{"role": "user", "content": "Reply with exactly: TEST_OK"}]
    result_a = run_test("A: minimal, no reasoning", minimal, None)
    result_b = run_test("B: minimal, with reasoning", minimal, True)

    print("\nBuilding full crypto prompt from live Binance data ...")
    bundle = build_real_bundle()
    full = [{"role": "system", "content": ai_decision._SYSTEM_PROMPT},
            {"role": "user", "content": ai_decision.build_prompt(bundle)}]
    result_c = run_test("C: full crypto prompt, no reasoning", full, False)
    result_d = run_test("D: full crypto prompt, with reasoning", full, True)
    result_e = run_test("E: full crypto prompt, bare python UA", full, False,
                        bare_ua=True)

    bundles = build_real_bundles()
    batch = [{"role": "system", "content": ai_decision._SYSTEM_PROMPT},
             {"role": "user", "content": ai_decision._build_batch_decision_prompt(bundles)}]
    result_f = run_test("F: production batch prompt + JSON mode", batch, False,
                        json_mode=True)
    result_g = run_test("G: production batch prompt, no JSON mode", batch, False)

    print("\n=== Parsing F/G with the production extractor ===")
    for name, res in (("F", result_f), ("G", result_g)):
        content = res.get("content") or ""
        if not content:
            print(f"Test {name}: no content to parse (HTTP {res['status']})")
            res["parsed"] = 0
            continue
        meta: dict = {}
        try:
            elements = ai_decision._extract_json_array(content, meta)
        except ai_decision.AIDecisionError as exc:
            print(f"Test {name}: PARSE FAILED — {exc}")
            res["parsed"] = 0
            continue
        verdicts = [e for e in elements if isinstance(e, dict) and e.get("signal")]
        res["parsed"] = len(verdicts)
        flags = " ".join(f"{k}={meta[k]}" for k in ("truncated", "salvaged") if meta.get(k))
        print(f"Test {name}: PARSED {len(verdicts)}/{len(bundles)} verdicts"
              + (f" ({flags})" if flags else "")
              + " | " + ", ".join(f"{v.get('symbol', '?')}={v.get('signal')}"
                                 f"({v.get('confidence')})" for v in verdicts))

    print("\n=== Summary ===")
    for name, res in (("A minimal/no-reasoning", result_a), ("B minimal/reasoning", result_b),
                      ("C full/no-reasoning", result_c), ("D full/reasoning", result_d),
                      ("E full/bare-ua", result_e),
                      ("F batch/json-mode", result_f), ("G batch/no-json-mode", result_g)):
        print(f"Test {name}: HTTP {res['status']} -> {'OK' if res['ok'] else 'FAILED'}"
              + (f" | parsed {res['parsed']}" if "parsed" in res else ""))
    if "F" in result_f and "G" in result_g:
        if result_f.get("parsed") and not result_g.get("parsed"):
            print("Verdict: this model needs response_format — JSON mode is what makes "
                  "the batch usable. Keep AI_JSON_MODE=1.")
        elif result_g.get("parsed") and not result_f.get("parsed"):
            print("Verdict: JSON mode HURT (the gateway rejected or mangled it) — "
                  "set AI_JSON_MODE=0 in .env.")
        elif not result_f.get("parsed") and not result_g.get("parsed"):
            print("Verdict: neither shape parsed. Check finish_reason above: 'length' "
                  f"means the reply is truncated at max_tokens={config.AI_MAX_TOKENS} "
                  "(raise it), anything else means the model is answering in prose and "
                  "the prompt/temperature needs work.")

    # If any full-prompt run produced parsable JSON, show the parsed decision
    for name, res in (("C", result_c), ("D", result_d)):
        if res.get("content"):
            try:
                parsed = ai_decision.parse_ai_response(res["content"], bundle["current_price"])
                print(f"\nParsed decision from Test {name}: "
                      f"{parsed['signal']} entry={parsed['entry']} sl={parsed['sl']} "
                      f"tp={parsed['tp']} rr={parsed['rr']} confidence={parsed.get('confidence')}")
            except ai_decision.AIDecisionError as exc:
                print(f"\nTest {name} content could not be parsed as a decision: {exc}")


if __name__ == "__main__":
    main()
