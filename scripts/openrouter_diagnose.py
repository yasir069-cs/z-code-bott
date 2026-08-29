"""OpenRouter connectivity diagnosis — runs Tests A-D in order and prints
the REAL error bodies (never the API key):

  A: minimal request, NO reasoning      -> basic connectivity + model validity
  B: minimal request, WITH reasoning    -> is reasoning the breaker?
  C: full crypto prompt, NO reasoning   -> prompt size / JSON compliance
  D: full crypto prompt, WITH reasoning -> the production configuration

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


def run_test(name: str, messages: list, reasoning_enabled: bool | None) -> dict:
    payload = {"model": MODEL, "messages": messages, "max_tokens": config.AI_MAX_TOKENS}
    if reasoning_enabled is not None:
        payload["reasoning"] = {"enabled": reasoning_enabled}
    print(f"\n=== Test {name} ===")
    print(f"Model: {MODEL}")
    print(f"Reasoning: {payload.get('reasoning', 'omitted')}")
    try:
        response = requests.post(
            ai_decision.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
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

    print("\n=== Summary ===")
    for name, res in (("A minimal/no-reasoning", result_a), ("B minimal/reasoning", result_b),
                      ("C full/no-reasoning", result_c), ("D full/reasoning", result_d)):
        print(f"Test {name}: HTTP {res['status']} -> {'OK' if res['ok'] else 'FAILED'}")

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
