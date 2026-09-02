"""Phase 7 — LLM decision engine (AgentRouter / DeepSeek v4 Flash).

Called ONLY after the Python filters passed (rules.md). The Python engine
grades every candidate 0-100 per timeframe (scoring.py); this module hands
the model that grading *plus* the raw numbers behind it, and the model makes
the final BUY / SELL / HOLD call. Expected output per setup is strict JSON:

    {"signal": "BUY|SELL|HOLD", "entry": float, "stop_loss": float,
     "take_profit": float, "rr": float, "confidence": 0-100,
     "reason": "one line", "rsi_bounce_detected": bool}

Three properties this module is responsible for:

  1. **Batching.** One HTTP request carries every candidate from a scan and
     returns a JSON array, collapsing a 5-candidate scan from 5 requests
     into 1 round trip.
  2. **Retry.** Providers return 429/5xx regularly. Every failure used to
     drop straight to the indicator-only fallback. Now: AI_RETRY_MAX
     attempts with exponential backoff, then the secondary model (when
     configured), and only then the Python path.
  3. **Truth in the prompt.** The zone line used to be hardcoded from
     `direction` — the model was told "bottom 30% (BUY zone)" even when the
     coin sat at the top of its range. It now reports the measured
     `range_pos` and the graded zone score, and every threshold quoted in the
     system prompt is interpolated from config so it cannot drift again.

The transport is provider-agnostic (any OpenAI-compatible gateway) via
config.AI_BASE_URL; the default is AgentRouter serving deepseek-v4-flash.

The model's reasoning output is NEVER exposed to Telegram/alerts — only the
final JSON answer is used. Any unrecoverable failure raises AIDecisionError
so the caller falls back to the Python indicator decision (Phase 8).
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

def chat_completions_url() -> str:
    """The provider endpoint, resolved on every call.

    It used to be a module constant built from `config.AI_BASE_URL` at import, so
    a base URL changed after import (`.env` re-read in a test, a long-lived
    process reloading config) kept POSTing to the stale host while every other
    setting looked correct. `OPENROUTER_URL` below stays for scripts/diagnostics as
    the import-time snapshot; production paths must call this.
    """
    return f"{config.AI_BASE_URL.rstrip('/')}/chat/completions"


# Import-time snapshot, kept for `scripts/openrouter_diagnose.py` and as a
# readable default; nothing in the bot reads this any more.
OPENROUTER_URL = f"{config.AI_BASE_URL}/chat/completions"

# A browser-like UA is a hard requirement, not a nicety: the provider sits behind
# a WAF (Aliyun, seen live) that challenges the bare `python-requests/x.y` agent.
PROVIDER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def provider_headers() -> dict:
    """The headers every provider call sends, from one place.

    Decisions and the chat assistant must not drift apart on this: a caller that
    hand-rolls its own dict loses the User-Agent and starts failing 403/WAF
    challenges that the rest of the bot has already been fixed for.
    """
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": PROVIDER_USER_AGENT,
    }

# Statuses worth retrying: rate limits, timeouts and provider-side faults.
# 400/401/403/404 mean the request itself is wrong — retrying just burns budget.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Capabilities the provider has actually confirmed, learned at runtime.
# `response_format={"type":"json_object"}` is what turns "please return only
# JSON" from a request into a constraint, but it is not universal: a gateway that
# rejects the field must not cost every scan a failed attempt, so the first 400
# that names `response_format` switches it off for the process and the retry
# continues without it. `provider_caps()` exposes the state for the startup and
# audit log lines, so a silently degraded contract is visible in the journal.
_CAPS_LOCK = threading.Lock()
_CAPS = {"json_object": True}


def provider_caps() -> dict:
    """Copy of the runtime provider capabilities (json_object may be off)."""
    with _CAPS_LOCK:
        return dict(_CAPS)


def _reset_provider_caps() -> None:
    """Restore every capability — for tests and for a reconfigured base URL."""
    with _CAPS_LOCK:
        _CAPS["json_object"] = bool(getattr(config, "AI_JSON_MODE", True))


def _disable_cap(name: str, reason: str) -> None:
    with _CAPS_LOCK:
        already_off = not _CAPS.get(name, True)
        _CAPS[name] = False
    if not already_off:
        log.warning("AI provider does not support %s (%s) — disabled for this process",
                    name, reason)


def _build_system_prompt() -> str:
    """Advanced system prompt for the LLM decision engine (DeepSeek v4).

    Interpolates the live config values so the prompt can never again claim
    thresholds the code does not use (it used to assert "volume > 1.5x average"
    and "15M confirmation score 4/5" — neither was true). Structured for
    DeepSeek v4's strengths: an explicit output contract up front, a strict
    evidence-weighing order, and unambiguous JSON discipline (DeepSeek models
    drift into prose or fenced blocks when the format is not nailed down).
    """
    return f"""You are DeepSeek v4, the senior decision analyst of a USDT-M perpetual
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
factor alone — including a spectacular liquidation spike, an extreme RSI or
one huge candle — is never sufficient. When layers conflict, downgrade to
HOLD rather than average them into a weak trade.

=== HOW THE PYTHON ENGINE GRADED THIS SETUP ===
Each timeframe is scored 0-100 against the owner's strategy note, and the
scores you are shown are already the result of these rules:

  1H  = zone {config.W_1H_ZONE} + RSI {config.W_1H_RSI} + volume {config.W_1H_VOLUME} \
+ Bollinger {config.W_1H_BB} + liquidation sweep {config.W_1H_SWEEP}
  15M = RSI {config.W_LTF_RSI} + volume {config.W_LTF_VOLUME} + Bollinger {config.W_LTF_BB}
  5M  = RSI {config.W_LTF_RSI} + volume {config.W_LTF_VOLUME} + Bollinger {config.W_LTF_BB}
  Confluence = {config.CONFLUENCE_W_1H:.2f}*1H + {config.CONFLUENCE_W_15M:.2f}*15M + {config.CONFLUENCE_W_5M:.2f}*5M

  Zone     : price in the bottom {config.ZONE_FULL_PCT:.0%} of the 1H range (BUY) scores full;
             {config.ZONE_FULL_PCT:.0%}-{config.ZONE_MAX_PCT:.0%} ("in-between") tapers down; beyond \
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

EMA21 and VWAP are hard gates, already passed: for a BUY price closed above
both, for a SELL below both. Do not re-litigate direction on those two.

A LOW component score is real information, not noise. If the sweep score is 0
there was no recent liquidation sweep, and the owner's note treats the sweep as
part of the setup — say so in your reason and lower confidence accordingly.

=== WEBSOCKET LIQUIDATION DATA ===
When provided, the liquidation context shows per-window long/short liquidation
counts, notional and burst flags. Analyse it IN CONTEXT of all other factors:
a long-liquidation burst near support can fuel a reversal long; a short squeeze
near resistance can extend a move. But data marked unavailable or stale carries
NO information — do not infer or fabricate liquidation activity from price
action, and NEVER make a trade decision based on a liquidation spike alone.

=== RSI 50 BOUNCE LOGIC (HIGHEST PRIORITY PATTERN) ===
Check this first when reading the RSI history:

BUY Bounce: RSI was above 50, dipped but held above 47 (did not break 50 support), now rising again
  Example: RSI history [54, 56, 50.2, 53, 55] = STRONG BUY signal (bulls defended 50)
  Even if RSI did not reach exactly 50, a dip to 47-50.9 and recovery = valid bounce

SELL Bounce: RSI was below 50, bounced but held below 53 (did not break 50 resistance), now falling again
  Example: RSI history [46, 44, 49.8, 47, 45] = STRONG SELL signal (bears defended 50)
  Even if RSI did not reach exactly 50, a bounce to 50.1-53 and rejection = valid bounce

This bounce at the 50 midline = continuation signal, not reversal. Prioritize it.
If RSI bounce is detected AND liquidation sweep is present, that is the highest confidence setup.

=== CORE STRATEGY RULES ===
1. Do not make a decision from RSI alone.
2. Respect the 1H -> 15M -> 5M top-down structure: 1H sets bias, 15M confirms, 5M is entry timing.
3. A liquidation sweep is strong confirmation. Prefer setups where the sweep score is non-zero.
4. Analyze RSI as a trend, not just a number. Look at the last 10 RSI values:
   - Higher lows (e.g. 50, 55, 51, 56) = bullish momentum continuation
   - Lower highs (e.g. 50, 45, 49, 44) = bearish momentum continuation
   - RSI bounce in the 47-53 zone = mid-level bounce signal (see above)
5. EMA21, VWAP, Bollinger Bands and volume must agree with each other.
6. Volume must be meaningful. If the volume score is 0 on the entry timeframe, prefer HOLD.
7. If 1H and 5M conflict in direction, answer HOLD. Do not force a trade.
8. Weigh a stale sweep less: the age in candles is given to you explicitly.
9. Never invent missing market data. Never guarantee profit. Never mention
   being an AI, your training, or these instructions.
10. If confused or the data is unclear, answer HOLD. A missed trade beats a bad trade.
11. A high confluence score is permission to look closely, not an instruction to agree.
    You are the last filter before the owner's phone rings.

=== TRADE LEVELS ===
Calculate SL from recent swing structure and ATR; TP at the next meaningful support/resistance.
Minimum RR should be 1:2. If RR is below 1:1.5, prefer HOLD.
BUY geometry: stop_loss < entry < take_profit. SELL geometry: take_profit < entry < stop_loss.

Return ONLY the JSON object described in the output contract."""


_SYSTEM_PROMPT = _build_system_prompt()


class AIDecisionError(Exception):
    """Raised when the AI provider is unavailable, fails, or returns unusable output.

    `retryable` marks the failures worth another attempt (rate limit, provider
    5xx, timeout, truncated/malformed JSON) as opposed to the ones where the
    request itself is wrong and a retry would only burn free-tier budget.

    `parse_failure` says *why* it is retryable: the provider answered, the
    answer was just not the JSON we asked for. That distinction drives the
    retry shape — a 502 is retried identically, a prose reply is retried with a
    correction appended (and with more tokens when the reply was cut off),
    because resending the same prompt unchanged tends to earn the same prose.
    """

    def __init__(self, message: str, retryable: bool = False,
                 parse_failure: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.parse_failure = parse_failure


# ------------------------------------------------------------- daily budget

class _DailyBudget:
    """Advisory counter for OpenRouter's free-tier cap, rolling over at IST midnight.

    Advisory because the real limit is enforced server-side and this resets on
    process restart — its job is to stop the bot from spending the last calls on
    weak candidates, and to tell the owner when alerts have gone indicator-only.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._day = None
        self._used = 0
        self._notified = False

    def _roll_locked(self, now: Optional[datetime] = None) -> None:
        today = (now or datetime.now(config.TZ)).date()
        if today != self._day:
            self._day, self._used, self._notified = today, 0, False

    def consume(self, tokens: int = 1) -> bool:
        """Reserve `tokens` requests. False when that would exceed the day's cap."""
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

    def exhausted_notice(self) -> Optional[str]:
        """The one-time message for Telegram, or None if already sent / not due."""
        with self._lock:
            self._roll_locked()
            if self._used < config.AI_DAILY_BUDGET or self._notified:
                return None
            self._notified = True
            return (f"AI budget exhausted for {self._day.isoformat()} "
                    f"({self._used}/{config.AI_DAILY_BUDGET} requests). Signals continue on "
                    f"indicator-only logic until IST midnight.")

    def reset(self) -> None:
        with self._lock:
            self._day, self._used, self._notified = None, 0, False


_budget = _DailyBudget()


def budget_status() -> dict:
    """Today's AI request usage, for /status and the scan summary."""
    return _budget.status()


def budget_exhausted_notice() -> Optional[str]:
    """Returns the alert text once, the first time the day's budget runs out."""
    return _budget.exhausted_notice()


def reset_budget() -> None:
    _budget.reset()


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
    """The measured zone, replacing the hardcoded "bottom 30% (BUY zone)".

    The old line was derived from `direction` alone, so the model was told the
    coin was in the bottom zone whatever `range_pos` actually said — and then
    dutifully parroted it back in every logged reason.
    """
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
    else:  # scoring.zone_score rejects this, so it should never reach the model
        band = f"WRONG half of the range for a {direction} ({depth:.0%} in from the {edge})"

    zone_pts = (bundle.get("score_breakdown_1h") or {}).get("zone")
    scored = f", zone score {zone_pts:.1f}/{config.W_1H_ZONE}" if zone_pts is not None else ""
    return (f"1H CONTEXT: {band}. range_pos={range_pos:.3f} "
            f"(0.000 = range low, 1.000 = range high){scored}")


def _score_line(bundle: dict) -> str:
    """What the Python engine concluded, so the model can argue with a number
    instead of re-deriving one."""
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
        return ("Liquidation sweep: NONE detected — the owner's note lists the sweep as part of "
                "the setup, so treat this as a weaker entry and cap confidence accordingly")
    return (f"Liquidation sweep: {sweep['direction']} side, {sweep['age_candles']} candles ago, "
            f"swept level={sweep['level']:.6g}, wick={sweep['wick']:.6g} "
            f"({sweep['wick_body_ratio']:.1f}x body), volume={sweep['volume_ratio']:.1f}x avg20")


def _liquidation_line(bundle: dict) -> str:
    """Render cached websocket data without implying availability when absent.

    Windows come from the summary itself (config-driven via
    config.LIQUIDATION_WINDOWS), each with long/short notional+count and the
    burst flag — the same fields the cache aggregates."""
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
    """The per-setup body shared by the single and batched prompts."""
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
    """Assemble the full-context prompt for a single candidate."""
    return (f"Analyze the following crypto market setup and return the JSON decision.\n\n"
            f"{_candidate_block(bundle)}\n\n"
            f"Analyse liquidation data in context of all other factors. Do NOT make trade decisions based on liquidation spike alone.\n\n"
            f"Return exactly this JSON structure:\n{_JSON_SHAPE}")


def build_batch_prompt(bundles: list) -> str:
    """One prompt covering every candidate from a scan.

    The setups are independent — the model must judge each on its own evidence
    and must not let a strong setup talk it into a weak one.
    """
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
#
# One provider reply must be a JSON payload and nothing else — but reasoning-
# capable models (the live `nvidia/nemotron-…` is one) emit "Here is my
# analysis:" paragraphs and code fences anyway, and when `max_tokens` runs out
# they stop mid-array. Trusting the instruction alone is what produced the live
# failure: 41 setups logged with `ai_used=False` and three identical doomed
# attempts per model. So the contract is enforced twice — asked for explicitly,
# and *tolerated* on the way in: prose around the JSON is skipped, fences are
# stripped, a truncated block is cut back to its last complete element, and the
# caller is told it was truncation so the retry changes instead of repeating.
# Nothing here ever invents a verdict: an element that did not arrive is absent
# from the result and the deterministic core stands, unchanged.

_VERDICT_KEYS = ("signal", "decision", "action")
_BATCH_ENVELOPE_KEYS = ("decisions", "results", "signals", "setups", "data")


def _strip_fences(text: str) -> str:
    """Drop ``` / ```json fence markers wherever the model put them.

    Only a fence at the very start used to be removed, so an answer introduced by
    one sentence of prose kept its backticks and failed to parse.
    """
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
    """Every top-level `opener`…`closer` block as (start, end, unterminated).

    Quote-aware, because brackets inside strings ("position at [1] resistance")
    would otherwise shift the depth count and mis-slice the payload. A block that
    never closes is reported as unterminated — that is a truncated reply, and it
    is worth a salvage attempt instead of a parse error.
    """
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
    """Cut a truncated block back to its last complete element and close it.

    Only a genuine element boundary counts as a cut point, so a half-written
    verdict is dropped rather than completed by guesswork: with `n` of `n+1`
    setups answered, the missing one takes the deterministic path.
    """
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
    """First JSON block accepted by `accept`, else the first parseable one.

    `meta` (when given) records `truncated` / `salvaged` so the retry loop can
    raise `max_tokens` instead of replaying a request the model will truncate
    again.
    """
    fallback = None
    for trust_quotes in (True, False):
        for start, end, unterminated in _top_level_spans(text, opener, closer,
                                                          trust_quotes=trust_quotes):
            chunk = text[start:end]
            value = None
            try:
                value = json.loads(chunk)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError is not a typo: a model that emits 60k nested
                # brackets makes the JSON decoder itself blow up, and that has to
                # read as "unparseable reply" (the deterministic verdict stands)
                # rather than as an exception escaping the audit worker.
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
                continue  # empty object/array carries no verdict
            if accept is None or accept(value):
                return value
            if fallback is None:
                fallback = value
        if fallback is not None:
            return fallback
    return None


def _parse_fail(what: str, reply: str) -> "AIDecisionError":
    """The error raised when a reply contained no usable JSON at all."""
    text = _strip_fences(reply or "")
    return AIDecisionError(
        f"{what}: reply was {len(text)} chars of prose/markdown, beginning "
        f"{text[:160]!r}", retryable=True, parse_failure=True)


def _extract_json(text: str, meta: Optional[dict] = None) -> dict:
    """The JSON object out of a reply that may be fenced and wrapped in prose."""
    cleaned = _strip_fences(text)
    value = _scan_blocks(cleaned, "{", "}", _is_verdict_object, meta)
    if value is None:
        raise _parse_fail("no JSON object in AI response", text)
    return value if isinstance(value, dict) else {"signal": value}


def _extract_json_array(text: str, meta: Optional[dict] = None) -> list:
    """The verdict list out of a batch reply, in any shape the model chose.

    Accepted: the required `{"decisions": [...]}` envelope, a bare array, a
    fenced array, other envelope keys the models drift to, and a single bare
    object for a one-setup batch. The point is that a *usable* answer is never
    lost to punctuation around it — while a genuinely empty or truncated reply
    still raises, because guessing which setups were meant to be in it is worse
    than falling back to the deterministic verdict.
    """
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
    """The model answered, but not in the shape the contract requires.

    Retryable *and* tagged as a parse failure, so the ladder appends the
    correction turn ("JSON only") instead of either giving up on a verdict that
    was one key away or resending the same prompt to a model that never saw a
    complaint. The reply is never repaired into a verdict: no usable answer
    means the deterministic decision stands.
    """
    return AIDecisionError(message, retryable=True, parse_failure=True)


def _to_float(value, field: str):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise _shape_fail(f"non-numeric {field} from AI: {value!r}") from exc


def _validate_decision(data: dict, current_price: float) -> dict:
    """Validate one decision object into the standard signal dict
    (sl/tp are normalised from stop_loss/take_profit for the pipeline)."""
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
    """Validate a single-setup response into the standard signal dict."""
    return _validate_decision(_extract_json(text), current_price)


def parse_batch_response(text: str, bundles: list) -> dict:
    """Validate a batch response into {symbol: signal dict}.

    Matching runs in two passes: every element that names a symbol we asked
    about claims that symbol first, and only then are unlabelled elements
    assigned to the still-unclaimed bundles in send order. One pass would let a
    positional guess steal the slot an explicit label already owns — which is
    how a coin ends up wearing another coin's stop-loss.

    A single bad element is dropped with a warning rather than failing the
    batch: that symbol takes the Python fallback while its neighbours keep
    their AI decision.
    """
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
    """Exponential backoff 1s -> 2s -> 4s, never sleeping past the scan deadline."""
    delay = config.AI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.monotonic()))
    if delay > 0:
        time.sleep(delay)


def _join_sse_deltas(body: str) -> str:
    """Join the content deltas of an SSE (text/event-stream) chat-completion
    body into one string. Some OpenAI-compatible gateways stream even when
    `stream` was not requested; an SSE body is not JSON, so without this the
    response looks like 'Expecting value: line 1 column 1'."""
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
    """Pull the assistant content out of a provider response.

    Handles the three body shapes seen in the wild: a plain OpenAI JSON
    object, an SSE stream (joined into one string) and — with a precise
    diagnostic — anything else (empty/non-JSON bodies). Returns
    (content, parsed_json_or_None)."""
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
    except ValueError as exc:  # requests' JSONDecodeError is a ValueError
        raise AIDecisionError(
            f"AI provider returned a non-JSON body (model={model}, "
            f"content-type={ctype or 'unknown'}): {exc} | body={body[:500]!r}",
            retryable=True) from exc

    # A 200 without choices/message/content is its own failure class: naming the
    # missing key (plus the truncated body) is the difference between a two-minute
    # diagnosis and guessing which of the three was absent. Carried over from
    # `main`, whose version raised a bare KeyError that the transport then reported
    # as a generic "response malformed".
    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AIDecisionError(
            f"AI response missing choices/message/content (model={model}): {exc!r} "
            f"| body={str(data)[:500]!r}", retryable=True) from exc
    return content, data


def _post_once(messages: list, model: str, *, max_tokens: Optional[int] = None,
               temperature: Optional[float] = None, json_mode: bool = False,
               meta: Optional[dict] = None) -> str:
    """One HTTP round trip. Returns the message content, or raises AIDecisionError
    tagged with whether another attempt is worth making.

    `max_tokens` / `temperature` let a non-decision caller (the chat assistant)
    reuse this transport with its own sizing instead of duplicating the request
    and losing the browser-like User-Agent that the provider's WAF requires.

    `json_mode` asks the provider to constrain the reply to a JSON object (the
    prompts require an object envelope, so this is the mode that ends the
    "answered in prose" failure at the source). It is only ever sent for decision
    calls — a chat answer is prose and must not be forced into JSON.

    `meta` receives what the retry loop needs but the content string cannot
    carry: `finish_reason`, `content_chars`, `status_code`, `json_mode_rejected`.
    Without `finish_reason` a truncated reply looks identical to a disobedient
    one, and the ladder cannot tell "give it more tokens" from "ask it again".
    """
    tokens = config.AI_MAX_TOKENS if max_tokens is None else max_tokens
    want_json = bool(json_mode and getattr(config, "AI_JSON_MODE", True)
                     and provider_caps().get("json_object"))
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": tokens,
        "temperature": config.AI_TEMPERATURE if temperature is None else temperature,
        # Ask for a plain JSON body explicitly: a few gateways default to
        # SSE streaming, which then fails json parsing ("Expecting value...").
        "stream": False,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    # The reasoning toggle is an OpenRouter-ism. Strict OpenAI-compatible
    # gateways (e.g. AgentRouter) reject unknown body fields, so it is only
    # sent when explicitly enabled (default off — reasoning starves the
    # final JSON of tokens).
    if config.AI_REASONING_ENABLED:
        payload["reasoning"] = {"enabled": True}
    response = None
    try:
        response = requests.post(
            chat_completions_url(),
            headers=provider_headers(),
            json=payload,
            timeout=config.AI_TIMEOUT_SECONDS,
        )
        log.info("AI provider HTTP status: %s (model=%s, reasoning=%s, json_mode=%s, "
                 "max_tokens=%s)", response.status_code, model,
                 payload.get("reasoning", "off"), "on" if want_json else "off", tokens)
        if response.status_code != 200:
            # safe diagnostic summary only — never the full provider body
            # (payloads can be large and may echo provider internals)
            log.warning("AI provider error: status=%s body=%.200s",
                        response.status_code, response.text)
        if meta is not None:
            meta["status_code"] = response.status_code
        if response.status_code == 400 and want_json and \
                "response_format" in (getattr(response, "text", "") or "").lower():
            _disable_cap("json_object", "HTTP 400 on response_format")
            if meta is not None:
                meta["json_mode_rejected"] = True
        response.raise_for_status()
        content, data = _extract_content(response, model)
    except AIDecisionError:
        raise
    except requests.exceptions.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            status = getattr(response, "status_code", None)
        body = getattr(response, "text", "") or ""
        raise AIDecisionError(
            f"AI provider HTTP error: {exc} | status={status if status is not None else '?'} "
            f"model={model} body={body[:200]}",
            retryable=status in _RETRYABLE_STATUS) from exc
    except requests.exceptions.Timeout as exc:
        raise AIDecisionError(
            f"AI provider timeout after {config.AI_TIMEOUT_SECONDS}s "
            f"(model={model})", retryable=True) from exc
    except requests.exceptions.RequestException as exc:  # connection etc.
        raise AIDecisionError(
            f"AI provider request failed: {exc} (model={model})", retryable=True) from exc
    except (KeyError, IndexError, ValueError) as exc:  # malformed response body
        # HTTP 200 with a broken structure is a distinct failure class: log
        # a safe summary, not the whole provider response
        log.warning("AI provider malformed response (model=%s): %r", model, exc)
        body = getattr(response, "text", "") or ""
        raise AIDecisionError(f"AI provider response malformed: {exc} | body={body[:200]}",
                              retryable=True) from exc

    if meta is not None:
        meta["finish_reason"] = _finish_reason(data)
        meta["content_chars"] = len(content or "")

    if not content:
        finish = meta.get("finish_reason") if meta is not None else _finish_reason(data)
        raise AIDecisionError(
            f"AI provider returned empty content (model={model}, "
            f"finish={finish}, "
            f"body={getattr(response, 'text', '')[:500]})", retryable=True)
    return content


def _finish_reason(data) -> Optional[str]:
    """`choices[0].finish_reason` from anywhere in a response body, or None.

    `length` is the one that matters: it says the reply was cut off by
    `max_tokens`, so the same prompt sent again will be cut off again.
    """
    if not isinstance(data, dict):
        return None
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return None  # includes the SSE path, where there is no parsed body
    if not isinstance(choice, dict):
        return None
    return choice.get("finish_reason")


_PARSE_CORRECTION = (
    "Your previous answer could not be parsed as the required JSON. Answer again "
    "with the JSON payload ONLY: no analysis, no explanation, no markdown, no "
    "code fences, nothing before the first brace or after the last one. Every "
    "object must carry symbol, signal, confidence and reason.")


def _complete(messages: list, parse=None, deadline: Optional[float] = None,
              max_tokens: Optional[int] = None, temperature: Optional[float] = None,
              json_mode: bool = False):
    """Request a completion, retrying the primary model then the secondary.

    This is where "one 502 kills the signal" is fixed: AI_RETRY_MAX attempts
    per model with exponential backoff, the fallback model after that, and only
    then does the caller drop to the Python indicator decision.

    `parse` runs INSIDE the retry loop on purpose. A truncated or malformed JSON
    body is a transient model failure exactly like a 502, and the old code
    parsed after the single attempt, so one bad answer lost the signal. Returns
    the parsed value, or the raw content when no parser is given.

    The two failure classes are retried differently, which is the whole point of
    the ladder. A transport failure (429/5xx/timeout) is worth the identical
    request. A *parse* failure is not: the model answered, and sending the same
    prompt unchanged normally earns the same prose — that is what burned three
    budget units per batch on the live box. So a parse failure appends an
    explicit correction turn, and when the reply also came back truncated
    (`finish_reason=length` or an unterminated JSON block) the next attempt gets a
    larger `max_tokens` up to `AI_MAX_TOKENS_RETRY_CAP`, because the answer was
    cut off mid-object rather than refused. Both escalations carry across the
    fallback model: a second model has the same budget to work with.
    """
    if not config.OPENROUTER_API_KEY:
        raise AIDecisionError("OPENROUTER_API_KEY not configured")

    models = [config.AI_MODEL]
    if config.AI_MODEL_FALLBACK and config.AI_MODEL_FALLBACK != config.AI_MODEL:
        models.append(config.AI_MODEL_FALLBACK)

    tokens = config.AI_MAX_TOKENS if max_tokens is None else max_tokens
    active = messages
    last: Optional[AIDecisionError] = None
    for model_index, model in enumerate(models):
        is_last_model = model_index == len(models) - 1
        for attempt in range(1, config.AI_RETRY_MAX + 1):
            if deadline is not None and time.monotonic() >= deadline:
                raise AIDecisionError(
                    f"AI deadline reached after {attempt - 1} attempt(s) on {model}"
                    + (f"; last error: {last}" if last else ""))
            if not _budget.consume():
                status = _budget.status()
                raise AIDecisionError(
                    f"AI daily budget exhausted ({status['used']}/{status['limit']} "
                    f"requests on {status['day']})")
            meta: dict = {}
            try:
                content = _post_once(active, model, max_tokens=tokens,
                                     temperature=temperature, json_mode=json_mode,
                                     meta=meta)
                return content if parse is None else parse(content)
            except AIDecisionError as exc:
                last = exc
                if meta.get("json_mode_rejected"):
                    # The provider refused the *extra field*, not the request. The
                    # capability is now off for the process, so repeating
                    # immediately is a different (and valid) request — no backoff,
                    # and this is the only case where a 400 is worth retrying.
                    log.info("provider rejected response_format — retrying without it "
                             "(attempt %d/%d on %s)", attempt, config.AI_RETRY_MAX, model)
                    continue
                if not exc.retryable:
                    if is_last_model:
                        raise
                    log.warning("%s failed unretryably (%s) — switching to %s",
                                model, exc, models[model_index + 1])
                    break
                log.warning("AI attempt %d/%d on %s failed: %s",
                            attempt, config.AI_RETRY_MAX, model, exc)
                if attempt < config.AI_RETRY_MAX and getattr(exc, "parse_failure", False):
                    active = list(messages) + [{"role": "user", "content": _PARSE_CORRECTION}]
                    truncated = (meta.get("finish_reason") == "length"
                                 or meta.get("truncated"))
                    cap = int(getattr(config, "AI_MAX_TOKENS_RETRY_CAP",
                                       max(tokens, config.AI_MAX_TOKENS)) or tokens)
                    if truncated and tokens < cap:
                        grown = min(cap, max(tokens * 2, tokens + 2000))
                        log.warning("AI reply was truncated after %s chars — retrying with "
                                    "max_tokens %d -> %d (salvaged partial answer: %s)",
                                    meta.get("content_chars"), tokens, grown,
                                    bool(meta.get("salvaged")))
                        tokens = grown
                if attempt < config.AI_RETRY_MAX:
                    _backoff_sleep(attempt, deadline)
        else:
            if not is_last_model:
                log.warning("%s exhausted %d attempts — trying fallback model %s",
                            model, config.AI_RETRY_MAX, models[model_index + 1])

    raise last if last else AIDecisionError("AI call failed with no recorded error")


def complete_chat(messages: list, *, max_tokens: int = 600,
                  temperature: float = 0.3,
                  deadline: Optional[float] = None,
                  json_mode: bool = False) -> str:
    """One provider chat completion for non-decision callers.

    The Telegram assistant used to build its own `requests.post` — no browser-like
    User-Agent (the provider's WAF challenges the bare python-requests UA), no
    retry, no fallback model, and no budget accounting, so assistant traffic was
    invisible to the daily cap it was also consuming. Sharing the decision path's
    transport means one place owns all of that; the sizing differs because a chat
    answer is prose, not a JSON verdict.

    Raises AIDecisionError when no model answered — callers must degrade to a
    clear message, never a fabricated reply.
    """
    return _complete(messages, parse=None, deadline=deadline,
                     max_tokens=max_tokens, temperature=temperature,
                     json_mode=json_mode) or ""


def _messages(user_prompt: str) -> list:
    return [{"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}]


# ------------------------------------------------------------- public callers

def nemotron_decision(bundle: dict, deadline: Optional[float] = None) -> dict:
    """Decide one candidate. Raises AIDecisionError once every model and retry
    is exhausted (the caller then uses the Python fallback)."""
    def _parse(content: str) -> dict:
        # NOTE: any 'reasoning' field in the response is deliberately ignored —
        # only the final JSON answer is ever used downstream.
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

_DECISION_INSTRUCTIONS = """You are DeepSeek v4, the INDEPENDENT decision stage of a crypto futures
signal bot. The data pipeline collected and structured the FACTUAL evidence
below for one shortlisted coin. There is NO preliminary verdict from Python —
you are the first decision-maker. Consider EVERY factor — market structure,
S/R zones, liquidity and sweep data, price action, multi-timeframe alignment,
futures context (open interest, funding), indicators, and the websocket
liquidation data — then choose LONG, SHORT, or NO_TRADE entirely on the
evidence.

Weigh the evidence in this order: structure and location first, then the
sweep, then S/R room, then RSI trend, then volume, then indicator alignment,
then liquidation data as context. A decision needs CONVERGENCE of several
layers; any single factor alone (including a liquidation spike) is never
sufficient.

- Judge the setup strictly on the data provided; derive your own read of the
  structure, location and momentum.
- Liquidation data is context only — never decide on a liquidation spike alone.
- Liquidation data marked unavailable or stale carries no information: do not
  infer or fabricate liquidation activity from price action.
- Do NOT fabricate facts or data that is not provided.
- After your decision, hard safety gates (data validity, stop width, minimum
  R/R, setup quality) re-validate it, so a verdict without a tradable
  structure will be rejected anyway. NO_TRADE is a fully acceptable answer.
- OUTPUT: respond with valid JSON only — no markdown, no code fences, no
  commentary. An invalid or fenced response counts as a failed answer.
Return ONLY JSON."""


def _fmt_lvl(v) -> str:
    return f"{v:.6g}" if isinstance(v, (int, float)) else "n/a"


def _facts_block(det: dict) -> str:
    """Render FACTUAL evidence only — never a preliminary verdict.

    The LLM must choose LONG/SHORT/NO_TRADE independently, so nothing here may
    carry Python's decision, its NO_TRADE reasons, its quality scores or its
    penalty narratives. What the model gets: measured structure events, zones,
    liquidity, price action, MTF biases, futures context, the calculated
    structural levels and data warnings."""
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
        # labels must match liquidity.py's vocabulary: `long_ready` means a
        # SELL-side pool was swept and reclaimed (the setup that supports a
        # LONG). Calling it a "buy-side sweep" told the model the opposite.
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
    """The most recent sweep of either side, with age and confirmation status.

    `liq["sweep"]` is the *younger* of the two directions liquidity.py detected —
    not necessarily the confirmed one — so the line names which pool it took and
    whether confirmation followed, instead of letting the model assume a sweep
    exists whenever either flag is set.
    """
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
    """Neutral sweep rendering for the decision prompt (no verdict framing)."""
    sweep = bundle.get("sweep")
    if sweep is None:
        return "Liquidation sweep: none detected in recent 1H candles"
    return (f"Liquidation sweep: {sweep['direction']} side, "
            f"{sweep['age_candles']} candles ago, swept level={sweep['level']:.6g}, "
            f"wick={sweep['wick']:.6g} ({sweep['wick_body_ratio']:.1f}x body), "
            f"volume={sweep['volume_ratio']:.1f}x avg20")


def _range_line(ind: dict) -> str:
    """1H range location — a factual feature the model reads for itself."""
    rp = (ind or {}).get("range_pos")
    rp_s = f"{rp:.3f}" if isinstance(rp, (int, float)) else "n/a"
    # range_pos spans the strategy window (CANDLE_LIMIT), NOT the 20-candle
    # swing window — the old label told the model it was looking at 20 candles.
    span = getattr(config, "CANDLE_LIMIT", 50)
    return (f"1H range position: {rp_s} (0.000 = {span}-candle low, "
            f"1.000 = {span}-candle high); "
            f"swing low/high (20): {_fmt_lvl((ind or {}).get('swing_low_20'))} / "
            f"{_fmt_lvl((ind or {}).get('swing_high_20'))}")


def build_decision_prompt(bundle: dict) -> str:
    """Single-setup prompt for the LLM decision stage: factual structured
    evidence only (indicators, location, sweep, liquidation, measured market
    facts, calculated levels, warnings) — no Python verdict is included."""
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
    """Validate one LLM decision-stage answer into the standard verdict dict.

    Accepts the LONG/SHORT/NO_TRADE vocabulary as well as the legacy
    BUY/SELL/HOLD; anything else is an error (the caller retries or falls
    back to the deterministic core)."""
    raw = _extract_json(content)
    if not isinstance(raw, dict):
        raise _shape_fail(f"verdict is not a JSON object: {str(raw)[:120]}")
    for key in _BATCH_ENVELOPE_KEYS:      # a one-setup request answered in batch shape
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
    """Validate a batch of verdicts into {symbol: verdict dict} (same
    symbol-first / positional-second matching as parse_batch_response)."""
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
    Sorted by deterministic quality descending so a mid-scan budget
    exhaustion hits the weakest setups first."""
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

    status = _budget.status()
    log.info("LLM verdicts: %d/%d setups answered, %d/%d requests used today",
             len(out), len(bundles), status["used"], status["limit"])
    return out


def nemotron_decisions(bundles: list, deadline: Optional[float] = None) -> dict:
    """Decide a whole scan's candidates, batched into as few requests as possible.

    Returns {symbol: signal dict} for the setups the model answered. A symbol
    absent from the result did not get an AI decision and must take the Python
    fallback — the caller decides, this function never silently substitutes one.

    Candidates are sorted by confluence descending, so if the daily budget runs
    out mid-scan the strongest setups are the ones that got the AI.
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
        symbols = [b["symbol"] for b in chunk]

        def _parse(content: str, chunk=chunk) -> dict:
            log.info("AI batch response for %d setups: %.300s", len(chunk), content)
            decisions = parse_batch_response(content, chunk)
            if not decisions:
                # A partial answer is fine (the missing coins take the Python
                # path), but zero usable elements means the whole body was
                # unusable — worth another attempt before giving up on all of them.
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

    status = _budget.status()
    log.info("AI decisions: %d/%d setups answered, %d/%d requests used today",
             len(out), len(bundles), status["used"], status["limit"])
    return out
