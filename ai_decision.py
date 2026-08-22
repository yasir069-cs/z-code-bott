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

_SYSTEM_PROMPT = """You are the final decision engine for a USDT-M perpetual futures signal bot on Binance.
Active session: New York (6 PM - 11 PM IST). High volatility window.

Your job is NOT to execute trades. You only decide: BUY / SELL / HOLD.
The Python engine has already filtered the market; analyze the complete
1H -> 15M -> 5M context provided to you.

=== RSI 50 BOUNCE LOGIC (HIGHEST PRIORITY SIGNAL) ===
This is the most important pattern. Always check it first:

BUY Bounce: RSI was above 50 → dipped but stayed above 47 (did not break 50 support) → now rising again
  Example: RSI history [54, 56, 50.2, 53, 55] = STRONG BUY signal (bulls defended 50)
  Even if RSI did not reach exactly 50, a dip to 47-50.9 and recovery = valid bounce

SELL Bounce: RSI was below 50 → bounced but stayed below 53 (did not break 50 resistance) → now falling again
  Example: RSI history [46, 44, 49.8, 47, 45] = STRONG SELL signal (bears defended 50)
  Even if RSI did not reach exactly 50, a bounce to 50.1-53 and rejection = valid bounce

This bounce at the 50 midline = continuation signal, not reversal. Prioritize it.
If RSI bounce is detected AND liquidation sweep is present → highest confidence signal.

=== CORE STRATEGY RULES ===
1. Do not make a decision from RSI alone.
2. Respect the 1H -> 15M -> 5M top-down structure: 1H sets bias, 15M confirms, 5M is entry timing.
3. A liquidation sweep is strong confirmation — prefer signals where sweep is present.
4. Analyze RSI as a trend, not just a number. Look at the last 10 RSI values:
   - Higher lows (e.g. 50→55→51→56) = bullish momentum continuation
   - Lower highs (e.g. 50→45→49→44) = bearish momentum continuation
   - RSI bounce at 47-53 zone = mid-level bounce signal (see above)
5. EMA21, VWAP, Bollinger Bands and volume must confirm together.
6. Volume must be meaningful — if volume is weak on the signal candle, prefer HOLD.
7. If 1H and 5M conflict in direction → HOLD, do not force a trade.
8. If sweep is older than 3 candles → it is stale, reduce confidence.
9. If sweep is older than 5 candles → ignore sweep, weigh other factors only.
10. Never invent missing market data. Never guarantee profit.
11. If confused or data is unclear → HOLD. Missing opportunities is better than bad trades.

=== BUY CONDITIONS (need 4+ aligned) ===
- RSI 50 bounce detected (dipped to 47-50.9, now recovering) OR RSI 55-65 with momentum
- Price above EMA21
- Price above VWAP
- Volume > 1.5x average on signal candle
- Bollinger Band: price near or bounced from lower/mid band
- Liquidation sweep: buy-side sweep within last 3 candles
- 1H bias: bullish (price in bottom zone, RSI 50+)
- 15M confirmation score 4/5 or higher

=== SELL CONDITIONS (need 4+ aligned) ===
- RSI 50 bounce detected (bounced to 50.1-53, now falling) OR RSI 35-45 with momentum
- Price below EMA21
- Price below VWAP
- Volume > 1.5x average on signal candle
- Bollinger Band: price near or rejected from upper/mid band
- Liquidation sweep: sell-side sweep within last 3 candles
- 1H bias: bearish (price in top zone, RSI 50-)
- 15M confirmation score 4/5 or higher

Calculate SL from recent swing structure and ATR; TP at next meaningful support/resistance.
Minimum RR should be 1:2. If RR is less than 1:1.5 → prefer HOLD.

Return ONLY valid JSON with exactly these keys:
signal, entry, stop_loss, take_profit, rr, confidence, reason, rsi_bounce_detected.
For HOLD, entry/stop_loss/take_profit/rr may be null and confidence 0.
rsi_bounce_detected must be true or false (boolean).
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
    rsi_bounce_detected = bool(data.get("rsi_bounce_detected", False))

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
                "rr": rr, "confidence": confidence, "reason": reason, "ai_used": True,
                "rsi_bounce_detected": rsi_bounce_detected}

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
            "confidence": confidence, "reason": reason, "ai_used": True,
            "rsi_bounce_detected": rsi_bounce_detected}


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
