"""Phase 7 — LLM decision engine, multi-provider fallback pool.

Called ONLY after the Python filters passed (rules.md). The Python engine
grades every candidate 0-100 per timeframe; this module hands the model that
grading plus the raw numbers behind it, and the model makes the final
BUY / SELL / HOLD call. Expected output per setup is strict JSON.

TRANSPORT: instead of one fixed base_url/api_key/model, every call goes
through config.AI_PROVIDERS — an ORDERED LIST of independent providers (each
its own base_url, api_key, model, and daily budget counter). `_complete()`
walks the list: on a provider it tries up to AI_RETRY_MAX times (with the
existing truncation/parse-correction escalation), and moves to the NEXT
provider when the current one is exhausted (budget), fails unretryably, or
runs out of retries. This is what turns "AgentRouter has a bad day" or "this
OpenRouter account hit its free 50/day" into an automatic switch instead of a
silent fallback to the indicator-only Python decision.

The model's reasoning output is NEVER exposed to Telegram/alerts — only the
final JSON answer is used. Any unrecoverable failure (every provider
exhausted/failed) raises AIDecisionError so the caller falls back to the
Python indicator decision.
"""
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

import requests

import config

log = logging.getLogger("ai_decision")

# A browser-like UA is a hard requirement, not a nicety: some providers sit
# behind a WAF that challenges the bare `python-requests/x.y` agent.
PROVIDER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def chat_completions_url(base_url: Optional[str] = None) -> str:
    """The provider endpoint for `base_url` (or the pool's first provider /
    the legacy AI_BASE_URL when none is given — diagnostics scripts only)."""
    if base_url is None:
        base_url = config.AI_PROVIDERS[0]["base_url"] if config.AI_PROVIDERS else config.AI_BASE_URL
    return f"{base_url.rstrip('/')}/chat/completions"


# Import-time snapshot, kept for scripts/diagnostics; production paths always
# pass an explicit base_url through chat_completions_url().
OPENROUTER_URL = f"{config.AI_BASE_URL.rstrip('/')}/chat/completions"


def provider_headers(api_key: Optional[str] = None) -> dict:
    """The headers for a call using `api_key` (or the legacy
    OPENROUTER_API_KEY when none is given — diagnostics scripts only)."""
    if api_key is None:
        api_key = config.OPENROUTER_API_KEY
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": PROVIDER_USER_AGENT,
    }


def describe_providers() -> str:
    """One-line summary of the configured fallback ladder, for startup logs."""
    if not config.AI_PROVIDERS:
        return "NONE CONFIGURED"
    return " -> ".join(f"{p['name']}[{p['model']}]" for p in config.AI_PROVIDERS)


# Statuses worth retrying: rate limits, timeouts and provider-side faults.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# JSON-object capability, tracked PER BASE_URL (a gateway that rejects
# response_format is a property of the gateway, not of one API key).
_CAPS_LOCK = threading.Lock()
_CAPS: dict[str, dict] = {}


def _caps_for(base_url: str) -> dict:
    with _CAPS_LOCK:
        if base_url not in _CAPS:
            _CAPS[base_url] = {"json_object": bool(getattr(config, "AI_JSON_MODE", True))}
        return dict(_CAPS[base_url])


def provider_caps(base_url: Optional[str] = None) -> dict:
    """Copy of the runtime capabilities for `base_url` (json_object may be off)."""
    if base_url is None:
        base_url = config.AI_PROVIDERS[0]["base_url"] if config.AI_PROVIDERS else config.AI_BASE_URL
    return _caps_for(base_url)


def _reset_provider_caps() -> None:
    """Forget every learned capability — for tests and a reconfigured pool."""
    with _CAPS_LOCK:
        _CAPS.clear()


def _disable_cap(base_url: str, name: str, reason: str) -> None:
    with _CAPS_LOCK:
        current = _CAPS.setdefault(base_url, {"json_object": True})
        already_off = not current.get(name, True)
        current[name] = False
    if not already_off:
        log.warning("AI provider %s does not support %s (%s) — disabled for this process",
                    base_url, name, reason)


def _build_system_prompt() -> str:
    """Advanced system prompt for the LLM decision engine.

    Interpolates the live config values so the prompt can never claim
    thresholds the code does not use.
    """
    return f"""You are the senior decision analyst of a USDT-M perpetual
futures signal bot on Binance. Active session: New York overlap
({config.SESSION_START} - {config.SESSION_END} IST). High volatility window.
Your job is NOT to execute trades. You only decide: BUY / SELL / HOLD.

=== OUTPUT CONTRACT (READ FIRST — NON-NEGOTIABLE) ===
- Respond with ONE valid JSON object. No markdown, no code fences, no
  commentary before or after, no trailing prose.
- Exactly these keys: signal, entry, stop_loss, take_profit, rr, confidence,
  reason, rsi_bounce_detected.
- signal: "BUY" | "SELL" | "HOLD" (uppercase string).
- entry/stop_loss/take_profit/rr: numbers or null. For HOLD all four may be
  null and confidence 0.
- confidence: integer 0-100. rsi_bounce_detected: boolean true/false.
- reason: one concise sentence of concrete evidence (name the factors that
  decided it), never a generic template.
- Invalid JSON = a failed answer. There is no partial credit.

=== EVIDENCE HIERARCHY (WEIGH IN THIS ORDER) ===
1. Market structure and location (1H trend, range position, BOS/CHoCH)
2. Liquidity sweep (candle-based) — strong confirmation when fresh and confirmed
3. Support/resistance context — room to the opposing zone, achievable target
4. RSI trend and the RSI-50 bounce pattern
5. Volume confirmation — a move without volume is suspect
6. EMA21 / VWAP / Bollinger positioning — alignment, not a standalone trigger
7. Websocket liquidation data — context only; a spike alone never decides
8. Futures context (OI + funding) — crowding/squeeze context

A decision must be supported by CONVERGENCE of several layers. Any single
factor alone is never sufficient. When layers conflict, downgrade to HOLD.

=== HOW THE PYTHON ENGINE GRADED THIS SETUP ===
  1H  = zone {config.W_1H_ZONE} + RSI {config.W_1H_RSI} + volume {config.W_1H_VOLUME} \
+ Bollinger {config.W_1H_BB} + liquidation sweep {config.W_1H_SWEEP}
  15M = RSI {config.W_LTF_RSI} + volume {config.W_LTF_VOLUME} + Bollinger {config.W_LTF_BB}
  5M  = RSI {config.W_LTF_RSI} + volume {config.W_LTF_VOLUME} + Bollinger {config.W_LTF_BB}
  Confluence = {config.CONFLUENCE_W_1H:.2f}*1H + {config.CONFLUENCE_W_15M:.2f}*15M + {config.CONFLUENCE_W_5M:.2f}*5M

  Zone     : price in the bottom {config.ZONE_FULL_PCT:.0%} of the 1H range (BUY) scores full;
             {config.ZONE_FULL_PCT:.0%}-{config.ZONE_MAX_PCT:.0%} tapers down; beyond \
{config.ZONE_MAX_PCT:.0%} the setup was already rejected. SELL mirrors from the top.
  RSI      : BUY {config.RSI_BUY_FULL_MIN:.0f}-{config.RSI_BUY_FULL_MAX:.0f} scores full, \
{config.RSI_BUY_TOL_MIN:.0f}-{config.RSI_BUY_TOL_MAX:.0f} scores half.
             SELL {config.RSI_SELL_FULL_MIN:.0f}-{config.RSI_SELL_FULL_MAX:.0f} full, \
{config.RSI_SELL_TOL_MIN:.0f}-{config.RSI_SELL_TOL_MAX:.0f} half. RSI must also be MOVING in the trade direction.
  Volume   : above the previous candle scores full; merely above the 20-candle
             average scores {config.VOLUME_AVG_FRACTION:.0%}; below both scores zero.
  Bollinger: within {config.BB_NEAR_PCT_1H:.1%} (1H) / {config.BB_NEAR_PCT:.1%} (15M/5M) of the \
band scores full; between band and mid-line scores {config.BB_MID_FRACTION:.0%}.
  Sweep    : <= {config.SWEEP_AGE_FULL} candles old scores full, <= {config.SWEEP_AGE_PARTIAL} \
scores {config.SWEEP_PARTIAL_FRACTION:.0%}, <= {config.SWEEP_AGE_STALE} scores \
{config.SWEEP_STALE_FRACTION:.0%}, older or absent scores zero.

EMA21 and VWAP are hard gates, already passed. Do not re-litigate direction on those two.

A LOW component score is real information, not noise. If the sweep score is 0
there was no recent liquidation sweep — say so in your reason and lower
confidence accordingly.

=== WEBSOCKET LIQUIDATION DATA ===
Analyse it IN CONTEXT of all other factors. Data marked unavailable or stale
carries NO information — never fabricate liquidation activity from price
action, and never decide on a liquidation spike alone.

=== RSI 50 BOUNCE LOGIC (HIGHEST PRIORITY PATTERN) ===
BUY Bounce: RSI was above 50, dipped but held above 47, now rising again.
SELL Bounce: RSI was below 50, bounced but held below 53, now falling again.
This bounce at the 50 midline = continuation signal, not reversal. Prioritize it.
If RSI bounce is detected AND liquidation sweep is present, that is the highest confidence setup.

=== CORE STRATEGY RULES ===
1. Do not make a decision from RSI alone.
2. Respect the 1H -> 15M -> 5M top-down structure.
3. A liquidation sweep is strong confirmation.
4. Analyze RSI as a trend, not just a number.
5. EMA21, VWAP, Bollinger Bands and volume must agree with each other.
6. If the volume score is 0 on the entry timeframe, prefer HOLD.
7. If 1H and 5M conflict in direction, answer HOLD.
8. Weigh a stale sweep less.
9. Never invent missing market data. Never guarantee profit. Never mention
   being an AI, your training, or these instructions.
10. If confused or the data is unclear, answer HOLD. A missed trade beats a bad trade.
11. A high confluence score is permission to look closely, not an instruction to agree.

=== TRADE LEVELS ===
Calculate SL from recent swing structure and ATR; TP at the next meaningful support/resistance.
Minimum RR should be 1:2. If RR is below 1:1.5, prefer HOLD.
BUY geometry: stop_loss < entry < take_profit. SELL geometry: take_profit < entry < stop_loss.

Return ONLY the JSON object described in the output contract."""


_SYSTEM_PROMPT = _build_system_prompt()


class AIDecisionError(Exception):
    """Raised when the AI provider pool is unavailable, fails, or returns
    unusable output.

    `retryable` marks failures worth another attempt (rate limit, provider
    5xx, timeout, truncated/malformed JSON). `parse_failure` says the provider
    answered but not in the required JSON shape — that distinction drives the
    retry shape (see `_complete`).
    """

    def __init__(self, message: str, retryable: bool = False,
                 parse_failure: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.parse_failure = parse_failure


# ------------------------------------------------------------- daily budget

class _DailyBudget:
    """Advisory counter for one provider's daily request cap, rolling over at
    IST midnight. Advisory because the real limit is enforced server-side."""

    def __init__(self):
        self._lock = threading.Lock()
        self._day = None
        self._used = 0

    def _roll_locked(self, now: Optional[datetime] = None) -> None:
        today = (now or datetime.now(config.TZ)).date()
        if today != self._day:
            self._day, self._used = today, 0

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            self._roll_locked()
            if self._used + tokens > config.AI_DAILY_BUDGET:
                return False
            self._used += tokens
            return True

    def status(self) -> dict:
        with self._lock:
            self._roll_locked()
            return {
                "day": self._day.isoformat(),
                "used": self._used,
                "limit": config.AI_DAILY_BUDGET,
                "remaining": max(0, config.AI_DAILY_BUDGET - self._used),
            }

    def reset(self) -> None:
        with self._lock:
            self._day, self._used = None, 0


_budgets_lock = threading.Lock()
_budgets: dict[str, _DailyBudget] = {}


def _get_budget(provider_name: str) -> _DailyBudget:
    with _budgets_lock:
        if provider_name not in _budgets:
            _budgets[provider_name] = _DailyBudget()
        return _budgets[provider_name]


def budget_status() -> dict:
    """Aggregate + per-provider status. Flat keys (used/limit/remaining/day)
    stay backward-compatible with any caller expecting the single-provider
    shape; `providers` holds the breakdown."""
    today = datetime.now(config.TZ).date().isoformat()
    per_provider = {}
    total_used = total_limit = 0
    for p in config.AI_PROVIDERS:
        s = _get_budget(p["name"]).status()
        per_provider[p["name"]] = s
        total_used += s["used"]
        total_limit += s["limit"]
    return {
        "day": today,
        "used": total_used,
        "limit": total_limit,
        "remaining": max(0, total_limit - total_used),
        "providers": per_provider,
    }


_notice_lock = threading.Lock()
_notice_day = None
_notice_sent = False


def budget_exhausted_notice() -> Optional[str]:
    """The one-time Telegram message for the day EVERY provider in the pool
    is exhausted — not just one of them (that case is a silent internal
    fallback to the next provider, exactly as designed)."""
    global _notice_day, _notice_sent
    with _notice_lock:
        today = datetime.now(config.TZ).date()
        if today != _notice_day:
            _notice_day, _notice_sent = today, False
        if _notice_sent:
            return None
        status = budget_status()
        if status["remaining"] > 0 or not status["providers"]:
            return None
        _notice_sent = True
    lines = "; ".join(f"{name}: {s['used']}/{s['limit']}" for name, s in status["providers"].items())
    return (f"AI budget exhausted on ALL {len(status['providers'])} configured provider(s) "
            f"today ({lines}). Signals continue on indicator-only logic until reset.")


def reset_budget() -> None:
    with _budgets_lock:
        for b in _budgets.values():
            b.reset()
    global _notice_sent
    with _notice_lock:
        _notice_sent = False


# ------------------------------------------------------------ prompt building

def _fmt_snap(snap) -> str:
    if snap is None:
        return "n/a"
    trend = snap.get("volume_trend") or []
    return (
        f"rsi={snap['rsi']:.2f} (prev {snap['rsi_prev']:.2f}), "
        f"ema21={snap['ema21']:.6g}, vwap={snap['vwap']:.6g}, "
        f"bb=[{snap['bb_lower']:.6g} / {snap['bb_mid']:.6g} / {snap['bb_upper']:.6g}], "
        f"close={snap['close']:.6g}, atr={snap['atr']:.6g}, "
        f"volume last5={[round(v, 1) for v in trend]}"
    )


def _zone_line(bundle: dict) -> str:
    snap = bundle.get("ind_1h") or {}
    direction = bundle.get("direction", "BUY")
    range_pos = snap.get("range_pos")
    if range_pos is None:
        return "1H CONTEXT: zone unavailable (no range position)"

    edge = "bottom" if direction == "BUY" else "top"
    depth = range_pos if direction == "BUY" else 1.0 - range_pos
    if depth <= config.ZONE_FULL_PCT:
        band = f"deep in the {edge} zone (within the {edge} {config.ZONE_FULL_PCT:.0%})"
    elif depth <= config.ZONE_MAX_PCT:
        band = (f"IN-BETWEEN — {depth:.0%} in from the {edge}, past the "
                f"{config.ZONE_FULL_PCT:.0%} sweet spot but inside the {config.ZONE_MAX_PCT:.0%} limit")
    else:
        band = f"WRONG half of the range for a {direction} ({depth:.0%} in from the {edge})"

    zone_pts = (bundle.get("score_breakdown_1h") or {}).get("zone")
    scored = f", zone score {zone_pts:.1f}/{config.W_1H_ZONE}" if zone_pts is not None else ""
    return (f"1H CONTEXT: {band}. range_pos={range_pos:.3f} "
            f"(0.000 = range low, 1.000 = range high){scored}")


def _score_line(bundle: dict) -> str:
    s1h, s15, s5 = bundle.get("score_1h"), bundle.get("score_15m"), bundle.get("score_5m")
    if s1h is None and s15 is None and s5 is None:
        return ""
    fmt = lambda v: f"{v:.0f}" if v is not None else "n/a"
    conf = bundle.get("confluence")
    line = (f"PYTHON ENGINE SCORES (0-100): 1H {fmt(s1h)} | 15M {fmt(s15)} | 5M {fmt(s5)}"
            f" -> confluence {fmt(conf)}")
    breakdown = bundle.get("score_breakdown_1h")
    if breakdown:
        parts = ", ".join(f"{k} {v:.1f}" for k, v in breakdown.items())
        line += f"\n1H breakdown: {parts}"
    return line + "\n"


def _sweep_line(bundle: dict) -> str:
    sweep = bundle.get("sweep")
    if sweep is None:
        return ("Liquidation sweep: NONE detected — treat this as a weaker entry and "
                "cap confidence accordingly")
    return (f"Liquidation sweep: {sweep['direction']} side, {sweep['age_candles']} candles ago, "
            f"swept level={sweep['level']:.6g}, wick={sweep['wick']:.6g} "
            f"({sweep['wick_body_ratio']:.1f}x body), volume={sweep['volume_ratio']:.1f}x avg20")


def _liquidation_line(bundle: dict) -> str:
    summary = bundle.get("liquidation") or {}
    if not summary.get("available"):
        warning = summary.get("warning") or "no liquidation events in cache"
        return f"Liquidation stream: unavailable ({warning}) — do not infer or fabricate liquidation data"
    windows = summary.get("windows") or {}
    parts = []
    for window, data in windows.items():
        parts.append(f"{window}: long {data.get('long_count', 0)} "
                     f"(${data.get('long_notional', 0) or 0:.0f}) / "
                     f"short {data.get('short_count', 0)} "
                     f"(${data.get('short_notional', 0) or 0:.0f})"
                     + (" — BURST" if data.get("burst") else ""))
    return ("Websocket liquidation context: " + "; ".join(parts) +
            f"; latest={summary.get('latest_event_timestamp')}; "
            f"freshness={summary.get('freshness_seconds')}s; "
            "use only as extra confluence")


def _candidate_block(bundle: dict) -> str:
    snap5 = bundle["ind_5m"]
    trend = "UP (higher lows)" if bundle["direction"] == "BUY" else "DOWN (lower highs)"
    confirm = bundle.get("score_15m", bundle.get("confirm_score"))
    confirm_txt = f"{confirm:.0f}/100" if isinstance(confirm, (int, float)) else "n/a"

    return f"""Coin: {bundle['symbol']}
Current price: {bundle['current_price']:.6g}
Python filter direction: {bundle['direction']}
Entry price (last closed 5M candle): {bundle['entry_price']:.6g}

{_score_line(bundle)}{_zone_line(bundle)}
1H indicators: {_fmt_snap(bundle['ind_1h'])}
{_sweep_line(bundle)}
{_liquidation_line(bundle)}
Recent swing low (1H, 20 candles): {bundle['ind_1h']['swing_low_20']:.6g}
Recent swing high (1H, 20 candles): {bundle['ind_1h']['swing_high_20']:.6g}
ATR (1H, 14): {bundle['ind_1h']['atr']:.6g}

15M CONFIRMATION (score {confirm_txt}): {_fmt_snap(bundle['ind_15m'])}

5M ENTRY: {_fmt_snap(snap5)}
Last 10 RSI values (5M): {[round(v, 2) for v in snap5['rsi_history']]}
RSI trend direction: {trend}

Volume trend (1H last 5): {[round(v, 1) for v in bundle['ind_1h']['volume_trend']]}"""


_JSON_SHAPE = ('{"signal": "BUY|SELL|HOLD", "entry": 0, "stop_loss": 0, "take_profit": 0, '
               '"rr": 0, "confidence": 0, "reason": "short explanation", '
               '"rsi_bounce_detected": false}')


def build_prompt(bundle: dict) -> str:
    return (f"Analyze the following crypto market setup and return the JSON decision.\n\n"
            f"{_candidate_block(bundle)}\n\n"
            f"Analyse liquidation data in context of all other factors. Do NOT make trade decisions based on liquidation spike alone.\n\n"
            f"Return exactly this JSON structure:\n{_JSON_SHAPE}")


def build_batch_prompt(bundles: list) -> str:
    n = len(bundles)
    blocks = []
    for i, bundle in enumerate(bundles, start=1):
        blocks.append(f"===== SETUP {i} of {n}: {bundle['symbol']} =====\n{_candidate_block(bundle)}")
    body = "\n\n".join(blocks)
    example = f'{{"symbol": "{bundles[0]["symbol"]}", {_JSON_SHAPE[1:-1]}}}'
    return (f"Analyze the following {n} INDEPENDENT crypto market setups.\n"
            f"Judge each one only on its own evidence — they are unrelated coins, and a strong "
            f"setup is not a reason to approve a weak one. HOLD as many as deserve it.\n\n"
            f"{body}\n\n"
            f"Analyse liquidation data in context of all other factors. Do NOT make trade decisions based on liquidation spike alone.\n\n"
            f"Return a JSON ARRAY of exactly {n} objects, one per setup, in the same order, each "
            f"with an additional \"symbol\" field naming its coin. Wrap the array in one "
            f"JSON object — no analysis, no markdown, no code fences, no text before the "
            f"first brace or after the last one (a bare array is also accepted):\n"
            f"{{\"decisions\": [{example}, ...]}}\n"
            f"An answer that is not parseable JSON is a failed answer for every setup in it.")


# --------------------------------------------------------------- JSON parsing

_VERDICT_KEYS = ("signal", "decision", "action")
_BATCH_ENVELOPE_KEYS = ("decisions", "results", "signals", "setups", "data")


def _strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if "```" not in cleaned:
        return cleaned
    cleaned = re.sub(r"```[a-zA-Z0-9_+-]*[ \t]*\n?", "", cleaned)
    return cleaned.replace("```", "").strip()


def _is_verdict_object(value) -> bool:
    return isinstance(value, dict) and any(key in value for key in _VERDICT_KEYS)


def _is_verdict_array(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and any(_is_verdict_object(item) for item in value[:3]))


def _top_level_spans(text: str, opener: str, closer: str, *,
                     trust_quotes: bool = True) -> list[tuple[int, int, bool]]:
    spans: list[tuple[int, int, bool]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for index, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if trust_quotes and ch == '"':
            in_str = True
        elif ch == opener:
            if depth == 0:
                start = index
            depth += 1
        elif ch == closer and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, index + 1, False))
    if depth:
        spans.append((max(start, 0), len(text), True))
    return spans


def _repair_truncated(text: str, opener: str, closer: str) -> Optional[str]:
    depth = 0
    in_str = False
    esc = False
    last_end = -1
    for index, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 1:
                last_end = index
    if last_end < 0:
        return None
    return text[:last_end + 1] + closer


def _scan_blocks(text: str, opener: str, closer: str, accept=None,
                 meta: Optional[dict] = None):
    fallback = None
    for trust_quotes in (True, False):
        for start, end, unterminated in _top_level_spans(text, opener, closer,
                                                          trust_quotes=trust_quotes):
            chunk = text[start:end]
            value = None
            try:
                value = json.loads(chunk)
            except (json.JSONDecodeError, RecursionError):
                if not unterminated:
                    continue
                if meta is not None:
                    meta["truncated"] = True
                repaired = _repair_truncated(chunk, opener, closer)
                if repaired is None:
                    continue
                try:
                    value = json.loads(repaired)
                except (json.JSONDecodeError, RecursionError):
                    continue
                if meta is not None:
                    meta["salvaged"] = True
            if not value:
                continue
            if accept is None or accept(value):
                return value
            if fallback is None:
                fallback = value
        if fallback is not None:
            return fallback
    return None


def _parse_fail(what: str, reply: str) -> "AIDecisionError":
    text = _strip_fences(reply or "")
    return AIDecisionError(
        f"{what}: reply was {len(text)} chars of prose/markdown, beginning "
        f"{text[:160]!r}", retryable=True, parse_failure=True)


def _extract_json(text: str, meta: Optional[dict] = None) -> dict:
    cleaned = _strip_fences(text)
    value = _scan_blocks(cleaned, "{", "}", _is_verdict_object, meta)
    if value is None:
        raise _parse_fail("no JSON object in AI response", text)
    return value if isinstance(value, dict) else {"signal": value}


def _extract_json_array(text: str, meta: Optional[dict] = None) -> list:
    cleaned = _strip_fences(text)
    direct = _scan_blocks(cleaned, "[", "]", _is_verdict_array, meta)
    if isinstance(direct, list):
        return direct
    envelope = _scan_blocks(cleaned, "{", "}", None, meta)
    if isinstance(envelope, dict):
        for key in _BATCH_ENVELOPE_KEYS:
            value = envelope.get(key)
            if isinstance(value, list) and value:
                return value
        if _is_verdict_object(envelope):
            return [envelope]
    raise _parse_fail("no JSON array in AI batch response", text)


def _shape_fail(message: str) -> "AIDecisionError":
    return AIDecisionError(message, retryable=True, parse_failure=True)


def _to_float(value, field: str):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise _shape_fail(f"non-numeric {field} from AI: {value!r}") from exc


def _validate_decision(data: dict, current_price: float) -> dict:
    required = ("signal", "entry", "stop_loss", "take_profit", "rr", "confidence", "reason")
    missing = [f for f in required if f not in data]
    if missing:
        raise _shape_fail(f"AI response missing fields: {missing}")
    rsi_bounce_detected = bool(data.get("rsi_bounce_detected", False))

    signal = str(data["signal"]).upper()
    if signal not in ("BUY", "SELL", "HOLD"):
        raise _shape_fail(f"invalid signal from AI: {signal!r}")
    if not isinstance(data["reason"], str):
        raise _shape_fail("reason must be a string")

    entry = _to_float(data["entry"], "entry")
    sl = _to_float(data["stop_loss"], "stop_loss")
    tp = _to_float(data["take_profit"], "take_profit")
    rr = _to_float(data["rr"], "rr")
    confidence = _to_float(data["confidence"], "confidence")
    if confidence is None or not 0 <= confidence <= 100:
        raise _shape_fail(f"confidence out of range: {confidence!r}")
    reason = data["reason"].strip() or "no reason given"

    if signal == "HOLD":
        return {"signal": "HOLD", "entry": entry or current_price, "sl": sl, "tp": tp,
                "rr": rr, "confidence": confidence, "reason": reason, "ai_used": True,
                "rsi_bounce_detected": rsi_bounce_detected}

    entry = entry or current_price
    if sl is None or tp is None or sl <= 0 or tp <= 0:
        raise _shape_fail(f"{signal} without valid stop_loss/take_profit: {data!r}")
    if signal == "BUY" and (sl >= entry or tp <= entry):
        raise _shape_fail("BUY geometry invalid (stop_loss>=entry or take_profit<=entry): "
                          f"{data!r}")
    if signal == "SELL" and (sl <= entry or tp >= entry):
        raise _shape_fail("SELL geometry invalid (stop_loss<=entry or take_profit>=entry): "
                          f"{data!r}")
    if rr is None or rr <= 0:
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0

    return {"signal": signal, "entry": entry, "sl": sl, "tp": tp, "rr": rr,
            "confidence": confidence, "reason": reason, "ai_used": True,
            "rsi_bounce_detected": rsi_bounce_detected}


def parse_ai_response(text: str, current_price: float) -> dict:
    return _validate_decision(_extract_json(text), current_price)


def parse_batch_response(text: str, bundles: list) -> dict:
    elements = _extract_json_array(text)
    by_symbol = {b["symbol"]: b for b in bundles}
    claimed: dict[str, dict] = {}
    unlabelled: list[dict] = []

    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            log.warning("AI batch element %d is not an object: %.120r", index, element)
            continue
        symbol = element.get("symbol")
        bundle = by_symbol.get(symbol) if isinstance(symbol, str) else None
        if bundle is None:
            unlabelled.append(element)
        elif bundle["symbol"] in claimed:
            log.warning("AI batch returned %s twice, keeping the first", bundle["symbol"])
        else:
            claimed[bundle["symbol"]] = element

    open_slots = [b["symbol"] for b in bundles if b["symbol"] not in claimed]
    for symbol, element in zip(open_slots, unlabelled):
        log.warning("AI batch element with unusable symbol %r matched by position -> %s",
                    element.get("symbol"), symbol)
        claimed[symbol] = element
    if len(unlabelled) > len(open_slots):
        log.warning("AI batch returned %d element(s) matching no requested symbol, dropping",
                    len(unlabelled) - len(open_slots))

    out: dict[str, dict] = {}
    for symbol, element in claimed.items():
        try:
            out[symbol] = _validate_decision(element, by_symbol[symbol]["current_price"])
        except AIDecisionError as exc:
            log.warning("AI batch element for %s rejected (%s) — that coin falls back to Python",
                        symbol, exc)

    missing = [b["symbol"] for b in bundles if b["symbol"] not in out]
    if missing:
        log.warning("AI batch answered %d/%d setups; falling back to Python for: %s",
                    len(out), len(bundles), ", ".join(missing))
    return out


# ------------------------------------------------------------- HTTP transport

def _backoff_sleep(attempt: int, deadline: Optional[float] = None) -> None:
    delay = config.AI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.monotonic()))
    if delay > 0:
        time.sleep(delay)


def _join_sse_deltas(body: str) -> str:
    parts: list[str] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue
    return "".join(parts)


def _extract_content(response, model: str) -> tuple[str, Optional[dict]]:
    headers = getattr(response, "headers", None) or {}
    ctype = (headers.get("Content-Type") or "").lower()
    body = getattr(response, "text", "") or ""
    if "event-stream" in ctype or body.lstrip().startswith("data:"):
        content = _join_sse_deltas(body).strip()
        if not content:
            raise AIDecisionError(
                f"AI provider streamed an empty response (model={model}, "
                f"content-type={ctype or 'unknown'}, body={body[:500]!r})",
                retryable=True)
        return content, None
    try:
        data = response.json()
    except ValueError as exc:
        raise AIDecisionError(
            f"AI provider returned a non-JSON body (model={model}, "
            f"content-type={ctype or 'unknown'}): {exc} | body={body[:500]!r}",
            retryable=True) from exc

    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIDecisionError(
            f"AI response missing choices/message/content (model={model}): {exc!r} "
            f"| body={str(data)[:500]!r}", retryable=True) from exc
    return content, data


def _finish_reason(data) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(choice, dict):
        return None
    return choice.get("finish_reason")


def _post_once(messages: list, provider: dict, *, max_tokens: Optional[int] = None,
               temperature: Optional[float] = None, json_mode: bool = False,
               meta: Optional[dict] = None, deadline: Optional[float] = None) -> str:
    """One HTTP round trip to a specific provider. Returns the message
    content, or raises AIDecisionError tagged with whether another attempt
    (on this or the next provider) is worth making.

    `provider` = {"name", "base_url", "api_key", "model"} — one entry from
    config.AI_PROVIDERS.
    """
    model = provider["model"]
    base_url = provider["base_url"]
    api_key = provider["api_key"]
    name = provider["name"]

    tokens = config.AI_MAX_TOKENS if max_tokens is None else max_tokens
    want_json = bool(json_mode and getattr(config, "AI_JSON_MODE", True)
                     and provider_caps(base_url).get("json_object"))
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": tokens,
        "temperature": config.AI_TEMPERATURE if temperature is None else temperature,
        "stream": False,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    if config.AI_REASONING_ENABLED:
        payload["reasoning"] = {"enabled": True}
    # Opt-in OpenAI reasoning_effort (config.AI_REASONING_EFFORT). Ollama maps
    # "none" to think:false, which stops qwen3 from burning minutes in <think>
    # before the JSON verdict. Empty string sends nothing (cloud providers see
    # exactly the payload they saw before).
    effort = getattr(config, "AI_REASONING_EFFORT", "")
    if effort and not config.AI_REASONING_ENABLED:
        payload["reasoning_effort"] = effort

    response = None
    req_timeout = config.AI_TIMEOUT_SECONDS
    if deadline is not None:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise requests.exceptions.Timeout(f"AI deadline reached before HTTP call on {name}")
        req_timeout = max(1.0, min(req_timeout, rem))

    try:
        response = requests.post(
            chat_completions_url(base_url),
            headers=provider_headers(api_key),
            json=payload,
            timeout=req_timeout,
        )
        log.info("AI provider HTTP status: %s (provider=%s, model=%s, reasoning=%s, "
                 "json_mode=%s, max_tokens=%s)", response.status_code, name, model,
                 payload.get("reasoning", "off"), "on" if want_json else "off", tokens)
        if response.status_code != 200:
            log.warning("AI provider error: provider=%s status=%s body=%.200s",
                        name, response.status_code, response.text)
        if meta is not None:
            meta["status_code"] = response.status_code
        if response.status_code == 400 and want_json and \
                "response_format" in (getattr(response, "text", "") or "").lower():
            _disable_cap(base_url, "json_object", "HTTP 400 on response_format")
            if meta is not None:
                meta["json_mode_rejected"] = True
        response.raise_for_status()
        content, data = _extract_content(response, f"{name}:{model}")
    except AIDecisionError:
        raise
    except requests.exceptions.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            status = getattr(response, "status_code", None)
        body = getattr(response, "text", "") or ""
        raise AIDecisionError(
            f"AI provider HTTP error: {exc} | status={status if status is not None else '?'} "
            f"provider={name} model={model} body={body[:200]}",
            retryable=status in _RETRYABLE_STATUS) from exc
    except requests.exceptions.Timeout as exc:
        raise AIDecisionError(
            f"AI provider timeout after {config.AI_TIMEOUT_SECONDS}s "
            f"(provider={name} model={model})", retryable=True) from exc
    except requests.exceptions.RequestException as exc:
        raise AIDecisionError(
            f"AI provider request failed: {exc} (provider={name} model={model})",
            retryable=True) from exc
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("AI provider malformed response (provider=%s model=%s): %r",
                    name, model, exc)
        body = getattr(response, "text", "") or ""
        raise AIDecisionError(f"AI provider response malformed: {exc} | body={body[:200]}",
                              retryable=True) from exc

    if meta is not None:
        meta["finish_reason"] = _finish_reason(data)
        meta["content_chars"] = len(content or "")

    if not content:
        finish = meta.get("finish_reason") if meta is not None else _finish_reason(data)
        raise AIDecisionError(
            f"AI provider returned empty content (provider={name} model={model}, "
            f"finish={finish}, body={getattr(response, 'text', '')[:500]})", retryable=True)
    return content


_PARSE_CORRECTION = (
    "Your previous answer could not be parsed as the required JSON. Answer again "
    "with the JSON payload ONLY: no analysis, no explanation, no markdown, no "
    "code fences, nothing before the first brace or after the last one. Every "
    "object must carry symbol, signal, confidence and reason.")


def _complete(messages: list, parse=None, deadline: Optional[float] = None,
              max_tokens: Optional[int] = None, temperature: Optional[float] = None,
              json_mode: bool = False):
    """Request a completion, walking the provider pool (config.AI_PROVIDERS)
    in order.

    Each provider gets up to AI_RETRY_MAX attempts, with the same
    truncation/parse-correction escalation as before. A provider is
    abandoned — moving to the next one in the pool — when:
      - its daily budget is exhausted,
      - a request to it fails unretryably (bad request, auth error, etc.), or
      - it exhausts AI_RETRY_MAX retryable attempts (429/5xx/timeout/bad JSON).
    Only when EVERY provider in the pool is exhausted does this raise
    AIDecisionError, which the caller turns into the Python fallback decision.
    """
    providers = config.AI_PROVIDERS
    if not providers:
        raise AIDecisionError("No AI providers configured — check .env "
                              "(AI_BASE_URL/OPENROUTER_API_KEY/AI_MODEL or AI_PROVIDER_N_*)")

    base_tokens = config.AI_MAX_TOKENS if max_tokens is None else max_tokens
    original_messages = messages
    last: Optional[AIDecisionError] = None

    for p_index, provider in enumerate(providers):
        is_last_provider = p_index == len(providers) - 1
        budget = _get_budget(provider["name"])
        active = original_messages
        tokens = base_tokens

        for attempt in range(1, config.AI_RETRY_MAX + 1):
            if deadline is not None and (deadline - time.monotonic()) < 5.0:
                raise AIDecisionError(
                    f"AI deadline reached during provider {provider['name']}, "
                    f"attempt {attempt - 1}" + (f"; last error: {last}" if last else ""))
            if not budget.consume():
                log.warning("Provider %s daily budget exhausted (%s) — switching provider",
                            provider["name"], budget.status())
                last = AIDecisionError(f"provider {provider['name']} daily budget exhausted")
                break
            meta: dict = {}
            try:
                content = _post_once(active, provider, max_tokens=tokens,
                                     temperature=temperature, json_mode=json_mode,
                                     meta=meta, deadline=deadline)
                return content if parse is None else parse(content)
            except AIDecisionError as exc:
                last = exc
                if meta.get("json_mode_rejected"):
                    log.info("provider %s rejected response_format — retrying without it "
                             "(attempt %d/%d)", provider["name"], attempt, config.AI_RETRY_MAX)
                    continue
                if not exc.retryable:
                    if is_last_provider:
                        raise
                    log.warning("%s failed unretryably (%s) — switching provider",
                                provider["name"], exc)
                    break
                log.warning("AI attempt %d/%d on %s failed: %s",
                            attempt, config.AI_RETRY_MAX, provider["name"], exc)
                if attempt < config.AI_RETRY_MAX and getattr(exc, "parse_failure", False):
                    active = list(original_messages) + [{"role": "user", "content": _PARSE_CORRECTION}]
                    truncated = (meta.get("finish_reason") == "length" or meta.get("truncated"))
                    cap = int(getattr(config, "AI_MAX_TOKENS_RETRY_CAP",
                                       max(tokens, config.AI_MAX_TOKENS)) or tokens)
                    if truncated and tokens < cap:
                        grown = min(cap, max(tokens * 2, tokens + 2000))
                        log.warning("AI reply truncated after %s chars — retrying with "
                                    "max_tokens %d -> %d (provider=%s)",
                                    meta.get("content_chars"), tokens, grown, provider["name"])
                        tokens = grown
                if attempt < config.AI_RETRY_MAX:
                    _backoff_sleep(attempt, deadline)
        else:
            if not is_last_provider:
                log.warning("%s exhausted %d attempts — trying next provider",
                            provider["name"], config.AI_RETRY_MAX)

    raise last if last else AIDecisionError("AI call failed with no recorded error")


def complete_chat(messages: list, *, max_tokens: int = 600,
                  temperature: float = 0.3,
                  deadline: Optional[float] = None,
                  json_mode: bool = False) -> str:
    """One provider chat completion for non-decision callers (walks the same
    provider pool as decisions). Raises AIDecisionError when no provider in
    the pool answered — callers must degrade to a clear message, never a
    fabricated reply."""
    return _complete(messages, parse=None, deadline=deadline,
                     max_tokens=max_tokens, temperature=temperature,
                     json_mode=json_mode) or ""


def _messages(user_prompt: str) -> list:
    return [{"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]


# ------------------------------------------------------------- public callers

def nemotron_decision(bundle: dict, deadline: Optional[float] = None) -> dict:
    """Decide one candidate. Raises AIDecisionError once every provider in
    the pool and every retry is exhausted (the caller then uses the Python
    fallback)."""
    def _parse(content: str) -> dict:
        log.info("AI raw response for %s: %.300s", bundle["symbol"], content)
        return parse_ai_response(content, bundle["current_price"])

    return _complete(_messages(build_prompt(bundle)), parse=_parse, deadline=deadline,
                     json_mode=True)


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ------------------------------------------- LLM decision stage (full data)

_VERDICT_MAP = {"LONG": "LONG", "SHORT": "SHORT", "NO_TRADE": "NO_TRADE",
                "BUY": "LONG", "SELL": "SHORT", "HOLD": "NO_TRADE"}
_VERDICT_JSON_SHAPE = ('{"symbol": "...", "signal": "LONG|SHORT|NO_TRADE", '
                       '"confidence": 0, "reason": "short explanation"}')

_DECISION_INSTRUCTIONS = """You are the FINAL decision-maker of this crypto futures signal bot.

Python has already done all the work:
- Collected OHLCV data across 1H, 15M, and 5M timeframes
- Detected market structure (BOS, CHoCH, trend, bias) on each timeframe
- Identified support and resistance zones
- Verified liquidity sweep using 4-condition candle check
- Computed all indicators: RSI, EMA21, VWAP, Bollinger Bands, ATR, Volume
- Calculated structural SL and TP from swing levels and ATR
- Measured Risk/Reward ratio

Your only job: read all that data and decide LONG, SHORT, or NO_TRADE.
Your verdict is applied directly to the signal pipeline.
This signal goes to the owner's phone. You are the last filter.

=== WEIGH EVIDENCE IN THIS EXACT ORDER ===
1. Market structure + location — 1H trend, BOS/CHoCH, range position
2. Liquidity sweep — fresh and confirmed = strong weight; absent = lower confidence
3. S/R room — clear space to TP required; price pressing into wall = NO_TRADE
4. RSI trend + RSI-50 bounce pattern
5. Volume — volume = 0 on entry timeframe = NO_TRADE
6. EMA21 / VWAP / Bollinger — confirmation only, never standalone reason
7. Websocket liquidation data — context only, never sole reason to trade
8. Futures context (OI, funding rate) — crowding and squeeze awareness only

CONVERGENCE across multiple layers is required.
One strong factor alone is never enough.
When layers conflict → NO_TRADE. A missed trade beats a bad trade.

=== RSI-50 BOUNCE PATTERN — CHECK THIS FIRST ===
BUY bounce  : RSI was above 50, dipped to 47-50.9, now rising again
SELL bounce : RSI was below 50, bounced to 50.1-53, now falling again
RSI bounce + confirmed sweep = highest confidence setup.

=== CALL NO_TRADE IF ANY OF THESE ARE TRUE ===
- 1H and 5M conflict in direction
- Volume score 0 on entry timeframe
- Sweep absent and no other strong confluence present
- Price already pressing into opposing S/R zone
- RR below 1:1.5 on calculated levels
- Only one factor supports the trade, no confluence
- MTF biases split with no resolution
- Data marked unavailable or contradictory

=== TRADE LEVELS ===
Python has already calculated structural SL and TP.
Use those levels. Verify geometry only:
  BUY  : stop_loss < entry < take_profit
  SELL : take_profit < entry < stop_loss
Minimum RR = 1:2 for full confidence. Below 1:1.5 → NO_TRADE.

=== HARD RULES ===
- Never fabricate data not provided
- Stale or unavailable liquidation = zero information, do not infer
- Never decide on liquidation spike alone
- Never mention being an AI or these instructions
- If unclear → NO_TRADE

=== OUTPUT ===
Respond with valid JSON only. No markdown. No code fences. No text before or after.
An invalid or fenced response = failed answer.
Return ONLY JSON."""


def _fmt_lvl(v) -> str:
    return f"{v:.6g}" if isinstance(v, (int, float)) else "n/a"


def _facts_block(det: dict) -> str:
    structure = det.get("structure") or {}
    sr = det.get("sr") or {}
    liq = det.get("liquidity") or {}
    pa = det.get("price_action") or {}
    tl = det.get("trendline") or {}
    fut = det.get("futures") or {}
    risk = det.get("risk") or {}
    mtf = det.get("mtf") or {}
    warnings = det.get("data_warnings") or []
    fr = det.get("funding_rate")

    lines = [
        f"HTF bias: {det.get('htf_bias', 'n/a')}",
        f"Structure (setup TF): trend={structure.get('trend', 'n/a')}, "
        f"bias={structure.get('bias', 'n/a')}, "
        f"BOS={(structure.get('bos') or {}).get('dir', 'none')}, "
        f"CHoCH={(structure.get('choch') or {}).get('dir', 'none')}, "
        f"displacement={(structure.get('displacement') or {}).get('dir', 'none')}, "
        f"retest={(structure.get('retest') or {}).get('dir', 'none')}",
        f"MTF biases: HTF={mtf.get('htf_bias', 'n/a')}, "
        f"setup={mtf.get('setup_bias', 'n/a')}, entry={mtf.get('entry_bias', 'n/a')}"
        + (", fresh CHoCH against the standing HTF trend" if mtf.get("choch_reversal") else ""),
        f"S/R: price at {(sr.get('at_zone') or {}).get('side', 'no zone')}"
        f"{' (major)' if (sr.get('at_zone') or {}).get('major') else ''}",
        f"Liquidity: sell-side sweep confirmed (supports LONG)={liq.get('long_ready')}, "
        f"buy-side sweep confirmed (supports SHORT)={liq.get('short_ready')}, "
        f"equal lows={len(liq.get('equal_lows') or [])}, "
        f"equal highs={len(liq.get('equal_highs') or [])}",
        _sweep_age_fact(liq),
        f"Price-action signals: bullish={(pa.get('signals') or {}).get('bullish', 0)}, "
        f"bearish={(pa.get('signals') or {}).get('bearish', 0)}",
        f"Trendline break: {(tl.get('break') or {}).get('dir', 'none')}",
        f"Futures context: available={fut.get('available')}, "
        f"bias={fut.get('bias', 'n/a')}, conviction={fut.get('conviction', 'n/a')}",
        f"Funding rate: {fr if fr is not None else 'n/a'}",
        f"Calculated structural levels: entry={_fmt_lvl(det.get('entry'))}, "
        f"ATR={_fmt_lvl(structure.get('atr'))}, "
        f"SL={_fmt_lvl(risk.get('sl'))}, TP={_fmt_lvl(risk.get('tp'))}, "
        f"RR={_fmt_lvl(risk.get('rr'))}"
        + (f" (target from {risk.get('target_note')})" if risk.get("target_note") else ""),
    ]
    if warnings:
        lines.append(f"Data warnings: {', '.join(warnings)}")
    return "\n".join(lines)


def _sweep_age_fact(liq: dict) -> str:
    sweep = (liq or {}).get("sweep") or {}
    if not sweep:
        return "Most recent sweep: none detected on the setup timeframe"
    side = sweep.get("side") or ("sell-side" if sweep.get("direction") == "BUY"
                                 else "buy-side")
    state = "confirmed" if sweep.get("confirmed") else "not confirmed"
    ready = "ready" if sweep.get("ready") else "not ready"
    return (f"Most recent sweep: {side} pool, "
             f"{sweep.get('age_candles')} candle(s) ago, {state} ({ready})")


def _sweep_fact(bundle: dict) -> str:
    sweep = bundle.get("sweep")
    if sweep is None:
        return "Liquidation sweep: none detected in recent 1H candles"
    return (f"Liquidation sweep: {sweep['direction']} side, "
            f"{sweep['age_candles']} candles ago, swept level={sweep['level']:.6g}, "
            f"wick={sweep['wick']:.6g} ({sweep['wick_body_ratio']:.1f}x body), "
            f"volume={sweep['volume_ratio']:.1f}x avg20")


def _range_line(ind: dict) -> str:
    rp = (ind or {}).get("range_pos")
    rp_s = f"{rp:.3f}" if isinstance(rp, (int, float)) else "n/a"
    span = getattr(config, "CANDLE_LIMIT", 50)
    return (f"1H range position: {rp_s} (0.000 = {span}-candle low, "
            f"1.000 = {span}-candle high); "
            f"swing low/high (20): {_fmt_lvl((ind or {}).get('swing_low_20'))} / "
            f"{_fmt_lvl((ind or {}).get('swing_high_20'))}")


def build_decision_prompt(bundle: dict) -> str:
    det = bundle["deterministic"]
    return f"""{_DECISION_INSTRUCTIONS}

Coin: {bundle['symbol']}
Current price: {bundle['current_price']:.6g}

=== INDICATORS ===
1H: {_fmt_snap(bundle['ind_1h'])}
{_range_line(bundle['ind_1h'])}
15M: {_fmt_snap(bundle['ind_15m'])}
5M: {_fmt_snap(bundle['ind_5m'])}

=== 1H LIQUIDITY SWEEP (candle-based) ===
{_sweep_fact(bundle)}

=== WEBSOCKET LIQUIDATION DATA ===
{_liquidation_line(bundle)}

=== MEASURED MARKET FACTS ===
{_facts_block(det)}

=== YOUR VERDICT ===
Return exactly this JSON object:
{_VERDICT_JSON_SHAPE}"""


def _build_batch_decision_prompt(chunk: list) -> str:
    blocks = []
    for b in chunk:
        det = b["deterministic"]
        blocks.append(f"""--- {b['symbol']} ---
Current price: {b['current_price']:.6g}
1H: {_fmt_snap(b['ind_1h'])}
{_range_line(b['ind_1h'])}
15M: {_fmt_snap(b['ind_15m'])}
5M: {_fmt_snap(b['ind_5m'])}
Sweep: {_sweep_fact(b)}
Liquidation: {_liquidation_line(b)}
Measured facts:
{_facts_block(det)}""")
    return f"""{_DECISION_INSTRUCTIONS}

Setups ({len(chunk)}):
{chr(10).join(blocks)}

=== YOUR VERDICTS ===
Return ONE JSON object and nothing else — no analysis, no markdown, no code
fences, no text before the first "{{" or after the last "}}". Its only key is
"decisions", holding exactly {len(chunk)} objects, one per setup, in the order
above, each shaped exactly like:
{_VERDICT_JSON_SHAPE}

{{"decisions": [{_VERDICT_JSON_SHAPE}]}}

An answer that is not parseable JSON is a failed answer for every setup in it."""


def parse_verdict(content: str, current_price: float = None) -> dict:
    raw = _extract_json(content)
    if not isinstance(raw, dict):
        raise _shape_fail(f"verdict is not a JSON object: {str(raw)[:120]}")
    for key in _BATCH_ENVELOPE_KEYS:
        nested = raw.get(key)
        if isinstance(nested, list) and len(nested) == 1 and isinstance(nested[0], dict):
            raw = nested[0]
            break
    signal = _VERDICT_MAP.get(str(raw.get("signal", "")).strip().upper())
    if signal is None:
        raise _shape_fail(f"invalid verdict signal: {raw.get('signal')!r} "
                          f"(expected LONG/SHORT/NO_TRADE)")
    reason = str(raw.get("reason") or "").strip() or "no reason given"
    confidence = _to_float(raw.get("confidence"), "confidence")
    if confidence is None:
        confidence = 0.0
    confidence = max(0.0, min(100.0, confidence))
    return {"signal": signal, "confidence": confidence, "reason": reason,
            "ai_used": True}


def _parse_verdict_batch(content: str, bundles: list) -> dict:
    elements = _extract_json_array(content)
    by_symbol = {b["symbol"]: b for b in bundles}
    claimed: dict[str, dict] = {}
    unlabelled: list[dict] = []

    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            log.warning("Verdict batch element %d is not an object: %.120r",
                        index, element)
            continue
        symbol = element.get("symbol")
        if isinstance(symbol, str) and symbol in by_symbol and symbol not in claimed:
            claimed[symbol] = element
        elif symbol not in by_symbol:
            unlabelled.append(element)
        else:
            log.warning("Verdict batch returned %s twice, keeping the first", symbol)

    for b in bundles:
        if b["symbol"] in claimed:
            continue
        if unlabelled:
            claimed[b["symbol"]] = unlabelled.pop(0)
        else:
            break

    out: dict[str, dict] = {}
    for symbol, element in claimed.items():
        try:
            out[symbol] = parse_verdict(json.dumps(element))
        except AIDecisionError as exc:
            log.warning("Verdict for %s unusable (%s) — deterministic path", symbol, exc)
    if not out:
        raise AIDecisionError(
            f"verdict batch produced no usable decisions for {len(bundles)} setups",
            retryable=True)
    return out


def llm_verdicts(bundles: list, deadline: Optional[float] = None) -> dict:
    """LLM decision stage: verdicts for a scan's shortlisted coins, batched.

    Returns {symbol: {"signal": LONG|SHORT|NO_TRADE, "confidence": float,
    "reason": str}}. A symbol absent from the result got no AI answer and
    takes the deterministic path — this function never substitutes one.
    Sorted by deterministic quality descending so a mid-scan exhaustion of
    the WHOLE provider pool hits the weakest setups first."""
    if not bundles:
        return {}

    ordered = sorted(bundles,
                     key=lambda b: (b["deterministic"].get("setup_quality") or 0.0),
                     reverse=True)
    out: dict[str, dict] = {}

    if not config.AI_BATCH_ENABLED or len(ordered) == 1:
        for bundle in ordered:
            def _parse(content: str, bundle=bundle) -> dict:
                log.info("LLM verdict for %s: %.200s", bundle["symbol"], content)
                return {bundle["symbol"]: parse_verdict(content)}
            try:
                out.update(_complete(_messages(build_decision_prompt(bundle)),
                                     parse=_parse, deadline=deadline, json_mode=True))
            except AIDecisionError as exc:
                log.warning("LLM verdict unavailable for %s (%s) — deterministic path",
                            bundle["symbol"], exc)
        return out

    for chunk in _chunks(ordered, config.AI_BATCH_MAX):
        symbols = [b["symbol"] for b in chunk]

        def _parse(content: str, chunk=chunk) -> dict:
            log.info("LLM verdict batch for %d setups: %.300s", len(chunk), content)
            return _parse_verdict_batch(content, chunk)

        try:
            out.update(_complete(_messages(_build_batch_decision_prompt(chunk)),
                                 parse=_parse, deadline=deadline, json_mode=True))
        except AIDecisionError as exc:
            log.warning("LLM verdict batch of %d failed (%s) — deterministic path "
                        "for: %s", len(chunk), exc, ", ".join(symbols))
            continue

    status = budget_status()
    log.info("LLM verdicts: %d/%d setups answered, %d/%d requests used today (pool total)",
             len(out), len(bundles), status["used"], status["limit"])
    return out


def nemotron_decisions(bundles: list, deadline: Optional[float] = None) -> dict:
    """Decide a whole scan's candidates, batched into as few requests as possible.

    Returns {symbol: signal dict} for the setups the model answered. A symbol
    absent from the result did not get an AI decision and must take the Python
    fallback.
    """
    if not bundles:
        return {}

    ordered = sorted(bundles, key=lambda b: b.get("confluence") or 0.0, reverse=True)
    out: dict[str, dict] = {}

    if not config.AI_BATCH_ENABLED or len(ordered) == 1:
        for bundle in ordered:
            try:
                out[bundle["symbol"]] = nemotron_decision(bundle, deadline=deadline)
            except AIDecisionError as exc:
                log.warning("AI unavailable for %s (%s) — using Python fallback",
                            bundle["symbol"], exc)
        return out

    for chunk in _chunks(ordered, config.AI_BATCH_MAX):
        symbols = [c["symbol"] for c in chunk]

        def _parse(content: str, chunk=chunk) -> dict:
            log.info("AI batch response for %d setups: %.300s", len(chunk), content)
            decisions = parse_batch_response(content, chunk)
            if not decisions:
                raise AIDecisionError(
                    f"AI batch produced no usable decisions for {len(chunk)} setups",
                    retryable=True)
            return decisions

        try:
            out.update(_complete(_messages(build_batch_prompt(chunk)),
                                 parse=_parse, deadline=deadline, json_mode=True))
        except AIDecisionError as exc:
            log.warning("AI batch of %d failed (%s) — Python fallback for: %s",
                        len(chunk), exc, ", ".join(symbols))
            continue

    status = budget_status()
    log.info("AI decisions: %d/%d setups answered, %d/%d requests used today (pool total)",
             len(out), len(bundles), status["used"], status["limit"])
    return out
