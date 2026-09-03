"""AI summary + market-impact analysis for VERIFIED news events only.

The AI never decides what is true: `analyze_event` is only ever handed
events whose verification status is already VERIFIED (enforced by
NewsEngine.publish and re-enforced here). Its whole job:

  1. summarize the verified facts (what/who/when/what-was-confirmed),
  2. classify market impact direction / strength / time horizons,
  3. separate VERIFIED FACTS from AI MARKET INTERPRETATION,
  4. stay uncertain rather than hallucinate.

Every response passes `_validate`: direction/strength must be legal enums,
horizons must exist, and the interpretation must not contain forbidden
guaranteed-pump language ("will rise", "BUY BTC", "will pump"...). A failed
validation raises AINewsError, and the engine then skips the alert — it
never publishes an incomplete or overreaching message.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import config

log = logging.getLogger("news_analysis")

DIRECTIONS = ("BULLISH", "BEARISH", "MIXED", "NEUTRAL", "UNCERTAIN")
STRENGTHS = ("LOW", "MEDIUM", "HIGH", "EXTREME")
HORIZONS = ("immediate", "short_term", "medium_term")

# Guaranteed-move / direct-signal / unearned-causality language the
# interpretation must never use.
_FORBIDDEN = re.compile(
    r"\b(will\s+(rise|fall|pump|dump|moon|drop|surge|crash|skyrocket)"
    r"|guaranteed\s+(gain|profit|return)"
    r"|\b(buy|long|short|sell)\s+(btc|eth|bitcoin|ethereum)\b"
    r"|\b(btc|eth|bitcoin|ethereum)\s+will\s+(pump|moon)\b"
    r"|(going\s+)?to\s+the\s+moon"
    r"|\bmoon\b"
    r"|\bpump\b.*\btonight\b"
    r"|(news|this)\s+(caused|is\s+causing)"
    r"|\bcaused\s+(btc|eth|bitcoin|ethereum|prices?|the\s+market)\b"
    r"|\bwill\s+continue\s+(falling|rising|dropping|pumping)\b"
    r")",
    re.IGNORECASE | re.VERBOSE)

_SYSTEM_PROMPT = (
    "You are a careful crypto news analyst. You summarize VERIFIED news facts "
    "and analyze POSSIBLE market impact. You never claim certainty about price "
    "moves, never give direct buy/sell commands, and never invent facts. "
    "Anything not supported by the supplied sources must be treated as unknown."
)

_PROMPT_TEMPLATE = """Analyze this VERIFIED crypto news event.

=== VERIFIED FACTS (from {n_articles} sources) ===
Headline: {headline}
{articles_block}
=== AFFECTED ASSETS ===
{assets}

=== OBSERVED MARKET DATA (measured at processing time, NOT caused by the news) ===
{market_block}

=== YOUR TASK ===
1. SUMMARY: 2-4 sentences answering ONLY from the verified sources above:
   What happened? Who/what is involved? When did it happen? What was
   officially confirmed? Do NOT add facts the sources do not state.
2. DIRECTION: one of BULLISH, BEARISH, MIXED, NEUTRAL, UNCERTAIN.
   If the sources do not give enough information to judge, choose UNCERTAIN.
3. STRENGTH: one of LOW, MEDIUM, HIGH, EXTREME (e.g. verified exchange hack
   = BEARISH/HIGH or EXTREME; major institutional adoption = BULLISH/HIGH;
   minor partnership = LOW or MEDIUM).
4. HORIZONS — three separate entries, do not merge them:
   immediate (minutes-hours), short_term (hours-days),
   medium_term (days-weeks). Each 1 sentence starting with words like
   "Could...", "May...", "Might...".
5. INTERPRETATION: concise AI market interpretation of the likely impact.
   Use hedged language ("Potentially bullish because...", "Could create
   short-term selling pressure..."). NEVER "will rise"/"will pump". NEVER
   "BUY"/"SELL" commands. Mention only sectors genuinely affected
   (BTC/ETH/alts/DeFi/AI tokens/L1-L2/memecoins/stablecoins/exchanges/
   mining/RWA/ETF flows/regulation) — do not list unaffected sectors.
   Separate observed data from interpretation and never claim the news
   CAUSED any price move unless a source explicitly establishes that.
6. KEY_RISK: the single biggest uncertainty.

=== OUTPUT ===
Return ONLY a JSON object with exactly these keys:
{{
  "summary": "...",
  "direction": "...",
  "strength": "...",
  "horizons": {{"immediate": "...", "short_term": "...", "medium_term": "..."}},
  "interpretation": "...",
  "key_risk": "..."
}}
"""


class AINewsError(Exception):
    """AI analysis failed (transport, parsing or validation)."""


def _fmt_num(v) -> str:
    """Render a market measurement, or 'n/a' when it was not available.

    A missing value must never be formatted as 0.0 — that would fabricate a
    flat reading the exchange never reported."""
    if isinstance(v, (int, float)):
        return f"{v:+.4g}" if -1 < v < 1 else f"{v:.6g}"
    return "n/a"


def build_prompt(event, market: dict) -> str:
    """Assemble the source-grounded prompt (verified claim + source contents +
    timestamps + assets + observed market data)."""
    arts = []
    for a in event.articles[:6]:
        arts.append(f"- [{a.source_domain}] {a.published_at:%Y-%m-%d %H:%M UTC}: "
                    f"{a.title}\n  {a.summary[:300]}")
    market_block = "No market data available — do not make market claims."
    observed = market.get("observed") or []
    if observed:
        rows = []
        for o in observed:
            rows.append(f"- {o.get('symbol', '?')}: price={_fmt_num(o.get('price'))}, "
                        f"1h={_fmt_num(o.get('change_1h_pct'))}%, "
                        f"24h={_fmt_num(o.get('change_24h_pct'))}%, "
                        f"volume_change={_fmt_num(o.get('volume_change_pct'))}, "
                        f"oi={_fmt_num(o.get('open_interest'))}, "
                        f"funding={_fmt_num(o.get('funding_rate'))}")
        market_block = ("OBSERVED (do not attribute causality to the news):\n"
                        + "\n".join(rows))
    return _PROMPT_TEMPLATE.format(
        n_articles=len(event.articles),
        headline=event.headline,
        articles_block="\n".join(arts),
        assets=", ".join(sorted(event.assets)) or "none identified",
        market_block=market_block,
    )


def _validate(raw: dict, event) -> dict:
    """Enforce the spec guardrails. Raises AINewsError on any violation."""
    if not isinstance(raw, dict):
        raise AINewsError("AI response is not a JSON object")

    summary = str(raw.get("summary") or "").strip()
    interpretation = str(raw.get("interpretation") or "").strip()
    if not summary or not interpretation:
        raise AINewsError("missing summary or interpretation")

    direction = str(raw.get("direction") or "").strip().upper()
    strength = str(raw.get("strength") or "").strip().upper()
    if direction not in DIRECTIONS:
        raise AINewsError(f"illegal direction {direction!r}")
    if strength not in STRENGTHS:
        raise AINewsError(f"illegal strength {strength!r}")

    horizons_raw = raw.get("horizons") or {}
    if not isinstance(horizons_raw, dict):
        raise AINewsError("horizons must be an object")
    horizons = {}
    for h in HORIZONS:
        val = str(horizons_raw.get(h) or "").strip()
        if not val:
            raise AINewsError(f"missing horizon {h}")
        horizons[h] = val

    if _FORBIDDEN.search(interpretation) or _FORBIDDEN.search(summary):
        raise AINewsError("overstated/guaranteed-move language rejected")

    return {
        "summary": summary,
        "direction": direction,
        "strength": strength,
        "horizons": horizons,
        "interpretation": interpretation,
        "key_risk": str(raw.get("key_risk") or "").strip() or
                    "Impact assessment based on limited information",
    }


def _parse_json(content: str) -> dict:
    """Parse the model's object — through the SHARED extractor, not a local regex.

    The private version stripped a fence only when the reply *started* with one and
    then fell back to `re.search(r"{.*}")`, which spans prose greedily and dies on a
    reply truncated mid-object. `ai_decision` already learned how to read JSON out of
    a messy reply (and how to report truncation); the news path now fails and
    succeeds exactly like the decision path instead of having its own rules.
    """
    import ai_decision
    try:
        parsed = ai_decision._extract_json(content)
    except ai_decision.AIDecisionError as exc:
        raise AINewsError(f"AI response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AINewsError(f"AI response is not a JSON object: {type(parsed).__name__}")
    return parsed


def _post(payload: dict) -> str:
    """The news transport is the shared one.

    This was the fourth hand-rolled `requests.post` in the codebase: same endpoint,
    none of the guarantees — no browser-like User-Agent (the provider sits behind a
    WAF that challenges the bare python-requests agent), no retry ladder, no
    fallback model, and invisible to the daily budget it was spending. It also kept
    its own `response_format` probe, so a gateway that rejects that field cost the
    news path a request every time instead of learning once per process.

    `analyze_event(event, market, _fetch=…)` keeps its payload-in signature, so the
    injection point used by tests and callers is unchanged; only the transport under
    it moved. `AIDecisionError` is re-thrown as `AINewsError` because the news
    engine's contract to its caller is "skip this alert", never "raise at the scan".
    """
    import ai_decision

    try:
        return ai_decision.complete_chat(
            payload["messages"],
            max_tokens=int(payload.get("max_tokens") or config.AI_MAX_TOKENS),
            temperature=float(payload.get("temperature", config.AI_TEMPERATURE)),
            json_mode=bool(payload.get("response_format")),
        )
    except ai_decision.AIDecisionError as exc:
        raise AINewsError(str(exc)) from exc


def analyze_event(event, market: Optional[dict] = None,
                  _fetch=_post) -> dict:
    """Summarize + classify one VERIFIED event. Raises AINewsError on any
    transport, parsing or validation failure — the caller then skips the
    alert rather than publishing something incomplete."""
    if event.status != "VERIFIED":
        raise AINewsError("refusing to analyze a non-VERIFIED event")

    market = market or {}
    prompt = build_prompt(event, market)
    content = _fetch({
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": config.AI_TEMPERATURE,
        "max_tokens": config.AI_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    })
    parsed = _parse_json(content)
    validated = _validate(parsed, event)
    validated["fact_interpretation_separated"] = True
    log.info("News AI analysis: '%s' -> %s/%s",
             event.headline[:60], validated["direction"], validated["strength"])
    return validated
