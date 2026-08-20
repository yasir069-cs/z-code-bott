"""Phase 7 — OpenRouter (NVIDIA Nemotron 3 Ultra) final decision.

Called ONLY after the Python filters passed (rules.md). Sends the full
context (coin, prices, 1H context + sweep, 15M/5M indicator values, last 10
RSI values, RSI trend direction, EMA21/VWAP/BB per timeframe, volume trend,
swing high/low, ATR) to OpenRouter and expects strict JSON:

    {"signal": "BUY|SELL|HOLD", "entry": float, "stop_loss": float,
     "take_profit": float, "rr": float, "confidence": 0-100,
     "reason": "one line"}

The model's reasoning output is NEVER exposed to Telegram/alerts — only the
final JSON answer is used. Any failure (missing key, HTTP error, timeout,
invalid/missing JSON fields) raises AIDecisionError so the caller falls
back to the Python indicator decision (Phase 8, unchanged).
"""
import json
import logging

import requests

import config

log = logging.getLogger("ai_decision")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPT = """You are the final decision engine for a crypto futures signal bot.

Your job is NOT to execute trades. You only decide: BUY / SELL / HOLD.
The Python engine has already filtered the market; analyze the complete
1H -> 15M -> 5M context provided to you.

IMPORTANT RULES:
1. Do not make a decision from RSI alone.
2. Respect the 1H -> 15M -> 5M top-down structure: 1H is the main context,
   15M is confirmation, 5M is entry timing.
3. A liquidation sweep is an important confirmation.
4. Consider RSI direction/trend (e.g. 50->55->51->56 is a higher low, still
   bullish), not only its current value.
5. Consider EMA21, VWAP, Bollinger Bands and volume together.
6. If signals conflict, prefer HOLD.
7. If the 1H context is weak, prefer HOLD.
8. If volume confirmation is weak, prefer HOLD.
9. If the liquidation sweep is old (5+ candles) or invalid, prefer HOLD.
10. Never invent missing market data. Never guarantee profit.

For BUY: bullish 1H context + valid bullish sweep + 15M confirmation + 5M
bullish entry structure. For SELL: the mirror. Calculate SL from the recent
swing structure and ATR; TP at the next meaningful support/resistance.

Return ONLY valid JSON with exactly these keys:
signal, entry, stop_loss, take_profit, rr, confidence, reason.
For HOLD, entry/stop_loss/take_profit/rr may be null and confidence 0.
No markdown, no code fences, JSON only."""


class AIDecisionError(Exception):
    """Raised when the AI provider is unavailable, fails, or returns unusable output."""


def _fmt_snap(snap) -> str:
    if snap is None:
        return "n/a"
    return (
        f"rsi={snap['rsi']:.2f} (prev {snap['rsi_prev']:.2f}), "
        f"ema21={snap['ema21']:.6g}, vwap={snap['vwap']:.6g}, "
        f"bb=[{snap['bb_lower']:.6g} / {snap['bb_mid']:.6g} / {snap['bb_upper']:.6g}], "
        f"close={snap['close']:.6g}, atr={snap['atr']:.6g}, "
        f"volume last5={[round(v, 1) for v in snap['volume_trend']]}"
    )


def build_prompt(bundle: dict) -> str:
    """Assemble the full-context prompt from a candidate bundle."""
    sweep = bundle["sweep"]
    snap5 = bundle["ind_5m"]
    trend = "UP (higher lows)" if bundle["direction"] == "BUY" else "DOWN (lower highs)"
    zone = "bottom 30% (BUY zone)" if bundle["direction"] == "BUY" else "top 30% (SELL zone)"

    if sweep is not None:
        sweep_line = (f"Liquidation sweep: {sweep['direction']} side, {sweep['age_candles']} candles ago, "
                      f"swept level={sweep['level']:.6g}, wick={sweep['wick']:.6g} "
                      f"({sweep['wick_body_ratio']:.1f}x body), volume={sweep['volume_ratio']:.1f}x avg20")
    else:
        sweep_line = "Liquidation sweep: NONE detected (no recent sweep — weigh other factors more heavily)"

    return f"""Analyze the following crypto market setup and return the JSON decision.

Coin: {bundle['symbol']}
Current price: {bundle['current_price']:.6g}
Python filter direction: {bundle['direction']}
Entry price (last closed 5M candle): {bundle['entry_price']:.6g}

1H CONTEXT: {zone}, range_pos={bundle['ind_1h']['range_pos']:.3f} (0=low, 1=high)
1H indicators: {_fmt_snap(bundle['ind_1h'])}
{sweep_line}
Recent swing low (1H, 20 candles): {bundle['ind_1h']['swing_low_20']:.6g}
Recent swing high (1H, 20 candles): {bundle['ind_1h']['swing_high_20']:.6g}
ATR (1H, 14): {bundle['ind_1h']['atr']:.6g}

15M CONFIRMATION (score {bundle['confirm_score']}/5): {_fmt_snap(bundle['ind_15m'])}

5M ENTRY: {_fmt_snap(snap5)}
Last 10 RSI values (5M): {[round(v, 2) for v in snap5['rsi_history']]}
RSI trend direction: {trend}

Volume trend (1H last 5): {[round(v, 1) for v in bundle['ind_1h']['volume_trend']]}

Return exactly this JSON structure:
{{"signal": "BUY|SELL|HOLD", "entry": 0, "stop_loss": 0, "take_profit": 0, "rr": 0, "confidence": 0, "reason": "short explanation"}}"""


def _extract_json(text: str) -> dict:
    """Parse the JSON object out of a (possibly fenced) response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "")
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AIDecisionError(f"no JSON object in AI response: {text[:200]!r}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIDecisionError(f"AI returned invalid JSON: {exc}") from exc


def _to_float(value, field: str):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AIDecisionError(f"non-numeric {field} from AI: {value!r}") from exc


def parse_ai_response(text: str, current_price: float) -> dict:
    """Validate the model's JSON into the standard signal dict
    (sl/tp are normalised from stop_loss/take_profit for the pipeline)."""
    data = _extract_json(text)

    required = ("signal", "entry", "stop_loss", "take_profit", "rr", "confidence", "reason")
    missing = [f for f in required if f not in data]
    if missing:
        raise AIDecisionError(f"AI response missing fields: {missing}")

    signal = str(data["signal"]).upper()
    if signal not in ("BUY", "SELL", "HOLD"):
        raise AIDecisionError(f"invalid signal from AI: {signal!r}")
    if not isinstance(data["reason"], str):
        raise AIDecisionError("reason must be a string")

    entry = _to_float(data["entry"], "entry")
    sl = _to_float(data["stop_loss"], "stop_loss")
    tp = _to_float(data["take_profit"], "take_profit")
    rr = _to_float(data["rr"], "rr")
    confidence = _to_float(data["confidence"], "confidence")
    if confidence is None or not 0 <= confidence <= 100:
        raise AIDecisionError(f"confidence out of range: {confidence!r}")
    reason = data["reason"].strip() or "no reason given"

    if signal == "HOLD":
        return {"signal": "HOLD", "entry": entry or current_price, "sl": sl, "tp": tp,
                "rr": rr, "confidence": confidence, "reason": reason, "ai_used": True}

    entry = entry or current_price
    if sl is None or tp is None or sl <= 0 or tp <= 0:
        raise AIDecisionError(f"{signal} without valid stop_loss/take_profit: {data!r}")
    if signal == "BUY" and (sl >= entry or tp <= entry):
        raise AIDecisionError(f"BUY geometry invalid (stop_loss>=entry or take_profit<=entry): {data!r}")
    if signal == "SELL" and (sl <= entry or tp >= entry):
        raise AIDecisionError(f"SELL geometry invalid (stop_loss<=entry or take_profit>=entry): {data!r}")
    if rr is None or rr <= 0:
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0

    return {"signal": signal, "entry": entry, "sl": sl, "tp": tp, "rr": rr,
            "confidence": confidence, "reason": reason, "ai_used": True}


def nemotron_decision(bundle: dict) -> dict:
    """Call OpenRouter/Nemotron with the full context.
    Raises AIDecisionError on any failure (caller uses the Python fallback)."""
    if not config.OPENROUTER_API_KEY:
        raise AIDecisionError("OPENROUTER_API_KEY not configured")

    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(bundle)},
        ],
        "reasoning": {"enabled": config.AI_REASONING_ENABLED},
        "max_tokens": config.AI_MAX_TOKENS,
        "temperature": config.AI_TEMPERATURE,
    }
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.AI_TIMEOUT_SECONDS,
        )
        log.info("OpenRouter HTTP status: %s (model=%s, reasoning=%s)",
                 response.status_code, config.AI_MODEL, payload["reasoning"])
        if response.status_code != 200:
            # never hide the API error: log status + full body (contains no key)
            log.error("OpenRouter error body: %s", response.text[:2000])
        response.raise_for_status()
        data = response.json()
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except requests.exceptions.HTTPError as exc:
        body = getattr(locals().get("response"), "text", "") or ""
        raise AIDecisionError(
            f"OpenRouter HTTP error: {exc} | status={getattr(exc.response, 'status_code', '?')} "
            f"model={config.AI_MODEL} reasoning={payload['reasoning']} body={body[:500]}") from exc
    except requests.exceptions.Timeout as exc:
        raise AIDecisionError(
            f"OpenRouter timeout after {config.AI_TIMEOUT_SECONDS}s "
            f"(model={config.AI_MODEL}, reasoning={payload['reasoning']})") from exc
    except requests.exceptions.RequestException as exc:  # connection etc.
        raise AIDecisionError(
            f"OpenRouter request failed: {exc} (model={config.AI_MODEL}, "
            f"reasoning={payload['reasoning']})") from exc
    except (KeyError, IndexError, ValueError) as exc:  # malformed response body
        raise AIDecisionError(f"OpenRouter response malformed: {exc} | body={response.text[:500]}") from exc

    if not content:
        raise AIDecisionError(
            f"OpenRouter returned empty content (model={config.AI_MODEL}, "
            f"reasoning={payload['reasoning']}, finish={data['choices'][0].get('finish_reason')}, "
            f"body={response.text[:500]})")
    # NOTE: any 'reasoning' field in the response is deliberately ignored —
    # only the final JSON answer is ever used downstream.
    log.info("AI raw response for %s: %.300s", bundle["symbol"], content)
    return parse_ai_response(content, bundle["current_price"])
