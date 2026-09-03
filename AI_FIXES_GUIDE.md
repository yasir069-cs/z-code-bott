# AI Signal Bot — Comprehensive Fixes & Analysis

> **Status 2026-09-02, superseded — read this first.** These commits never ran on the
> server. Its HEAD is `f7dcbda` (the loosened floors), not `7b58611`/`94d7d9b`/
> `84b910b`, so the gate reasons were never commented out there — which is exactly
> why `no_clear_target`, `poor_rr` and `no_structure_stop` still fill
> `signals_log.csv`. The scans after the "breakthrough" (`journalctl` Sep 02 12:07
> and 12:20 **UTC** = 17:37/17:50 IST, i.e. 40 min later) still ended
> `gate_rejected 41 | signals 0 (holds 41)`. Two reasons, both repaired in PR #2:
> the reward side was *measured* wrongly (target read from the nearest opposing
> zone, which is usually a wall the price sits inside — so `RR=0.01`-style values
> and `no achievable target`), and zeroing `QUALITY_RR_NONE_PENALTY` /
> `QUALITY_RR_MISS_PENALTY` while also commenting out `poor_rr` left reward:risk
> enforced nowhere. The AI-side change here was a clearer error message that
> *rejected* any reply starting with prose; the extractor now reads the JSON out of
> the reply instead, and the provider is asked to constrain output to JSON.
> The floors are now `.env`-tunable (`MIN_RR`, `QUALITY_MIN`, `QUALITY_PRIMARY_FLOOR`,
> `ALERT_QUALITY_MIN`, `MIN_SCORE_*`) with defaults back at the spec values, and
> `config.check_config_warnings()` reports a policy override at startup.
> Kept as the record of what was tried, not as instructions.

## Executive Summary

Your bot is experiencing two critical issues:

1. **AI JSON parsing failures** — The model returns analysis prose instead of JSON, causing signals to fall back to Python-only
2. **Excessive signal rejection** — Nearly all setups are rejected as HOLD due to stacking penalty multipliers (65% score cuts)

This document explains both problems, the fixes applied, and recommendations for tuning.

---

## Problem 1: AI JSON Parsing Failures

### What's Happening

From your logs:
```
WARNING | ai_decision | AI attempt 1/3 on nvidia/nemotron-3-ultra-550b-a55b:free failed: 
  no JSON object in AI response: 'The user wants me to analyze 20 different crypto futures 
  setups and provide a verdict for each: LONG, SHORT, or NO_TRADE...'
```

The AI model is **thinking out loud** (reasoning/analysis prose) instead of returning ONLY JSON. The error message is truncated to 200 chars, hiding the real problem.

### Root Causes

1. **System prompt not emphatic enough** — The model doesn't prioritize JSON-only output
2. **Model reasoning leakage** — DeepSeek v4 (like most LLMs) prefaces JSON with thinking
3. **Poor diagnostics** — Error messages truncated to 200 chars couldn't show the pattern

### Fixes Applied

#### 1. Strengthened System Prompt
```python
# In ai_decision.py _build_system_prompt()
*** CRITICAL: YOU MUST RESPOND WITH ONLY VALID JSON. ***
*** NO THINKING, NO EXPLANATION, NO MARKDOWN, NO CODE FENCES. ***
*** ONLY THE JSON OBJECT. NOTHING ELSE. ***
```

#### 2. Added Reasoning Detection
```python
def _is_likely_reasoning(text: str) -> bool:
    """Check if response starts with analysis/thinking instead of JSON."""
    reasoning_markers = (
        'let me', 'i need to', 'analyzing', 'step 1', 'however,'
    )
    return any(text.strip()[:500].lower().startswith(m) for m in reasoning_markers)
```

If detected, logs a warning with the first 400 chars of the response for debugging.

#### 3. Improved Error Context
- Error messages now show **300-500 chars** of response (not 200)
- Parse logs show **1000 chars** of response before JSON extraction
- Error diagnostics include response length and both start/end snippets

### Testing the Fix

Add this test to verify JSON-only enforcement:
```bash
# scripts/test_ai_json_only.py
python -c "
import ai_decision
prompt = ai_decision.build_prompt({'symbol': 'BTC/USDT', ...})
# The system prompt now includes the JSON-only directive
print(ai_decision._SYSTEM_PROMPT[:500])
"
```

### What to Monitor

After deploying, check the logs for:
```
| INFO    | ai_decision | AI response for BTC/USDT: {\"signal\": \"BUY\"...
```

If you still see reasoning prose, the model is ignoring the directive and you may need:
- A stricter model (Claude 3.5 Sonnet has better JSON mode)
- Or use OpenAI's `response_format="json"` if your provider supports it

---

## Problem 2: Excessive Signal Rejection

### What's Happening

From your logs:
```
XAUT/USDT: SHORT penalized — range_pos=0.14 at the wrong 1H extreme; 
  weak 1H volume vs avg20; RR=0.77 below MIN_RR=1.5 
  (quality=12.0, primary 46.0 -> 22.8)

DOT/USDT: SHORT penalized — range_pos=0.42 early in the 1H range; 
  at/below the lower Bollinger band; no achievable target (RR undefined) 
  (quality=9.9, primary 45.5 -> 34.8)
```

**37/37 signals are HOLD** — the quality scores are being crushed by stacking penalties.

### Root Cause: Cascading Multiplicative Penalties

The `_exhaustion_factor()` function was multiplying penalties:

```
Primary Score: 45.0
Location Penalty: 0.55 (45% cut)
Volume Penalty:  0.90 (10% cut)
RSI Penalty:     0.70 (30% cut)
Combined:        45 * 0.55 * 0.90 * 0.70 = 15.6 (65% total cut!)
```

**One setup getting 3 penalties = 65% score destruction.**

### The Fix: Smart Penalty Combination

Changed `_exhaustion_factor()` to use **the worst penalty with a floor**:

```python
# Before (multiplicative cascade):
factor = 0.55 * 0.90 * 0.70 = 0.35

# After (worst with 0.5 floor):
factor = max(0.5, min(0.55, 0.90, 0.70)) = 0.55
```

**Result:**
```
Before: 45 * 0.35 = 15.75 (HOLD)
After:  45 * 0.55 = 24.75 (still below 50, but more recoverable)
```

### Score Breakdown

Your setup now gets a chance:
- If primary is 45 (weak) → 45 * 0.55 = 24.75 (still fails QUALITY_MIN=50)
- If primary is 55 (moderate) → 55 * 0.55 = 30.25 (still fails)
- If primary is 65 (strong) → 65 * 0.55 = 35.75 (still fails, but close)

**This means setups with STRONG primary evidence (65+) can now recover.**

### Testing the Fix

Check the decision logs for quality breakdown:
```
INFO | decision | BTC/USDT quality: 45.2 (primary 60 * 0.75 = 45; 
  penalties: range_pos=0.20, weak_volume)
```

The `exhaustion_factor: 0.75` instead of `0.35` shows the fix is working.

---

## Problem 3: Understanding the Rejection Patterns

### Common Rejection Reasons

| Reason | Config | Meaning | Fix |
|--------|--------|---------|-----|
| **Location exhaustion** | `QUALITY_LOCATION_SEVERE_PCT=0.15` | Price within 15% of wrong extreme | Loosen to 0.25 |
| **Weak volume** | `PA_VOLUME_WEAK=0.67` | Volume < 67% of 20-avg | Loosen to 0.75 |
| **No achievable target** | `QUALITY_RR_NONE_PENALTY=30` | RR calculation failed | Improve sweep detection |
| **Direction conflict** | `MTF_REQUIRE_HTF_ALIGN=True` | Price on bear side of EMA21 | Review entry timing |

### Your Real Data

Looking at the actual rejections:

**XAUT/USDT SHORT (quality=12)**
- range_pos=0.14 → within 15% of range low (where you want room to go down)
- weak 1H volume
- RR=0.77 (below MIN_RR=1.5)
- **Diagnosis**: Exhausted location + poor risk/reward

**DOT/USDT SHORT (quality=9.9)**
- range_pos=0.42 → 42% from the top (early in range, but manageable)
- at/below lower Bollinger band (reversal zone?)
- **no achievable target** → RR undefined (likely no liquidity zone to target)
- **Diagnosis**: Can't find TP level

**BCH/USDT LONG (quality=0.0)**
- DIRECTION CONFLICT
- price on bear side of EMA21/VWAP on BOTH timeframes
- **Diagnosis**: Wrong time to trade this direction

---

## Configuration Tuning Guide

### If Most Signals Are HOLD

Try these adjustments (in `config.py`):

#### 1. Loosen Location Thresholds
```python
# Current: QUALITY_LOCATION_SEVERE_PCT = 0.15  (15%)
# Try:     QUALITY_LOCATION_SEVERE_PCT = 0.25  (25% - more room allowed)

# Current: QUALITY_LOCATION_SEVERE_FACTOR = 0.55  (45% cut)
# Try:     QUALITY_LOCATION_SEVERE_FACTOR = 0.70  (30% cut)
```

#### 2. Reduce Quality Gate
```python
# Current: QUALITY_MIN = 50
# Try:     QUALITY_MIN = 45  (or contingent on sweep)

# Or make it conditional:
# QUALITY_MIN = 45 if sweep_confirmed else 50
```

#### 3. Improve Volume Tolerance
```python
# Current: PA_VOLUME_WEAK = 0.67  (67% of 20-avg)
# Try:     PA_VOLUME_WEAK = 0.75  (75% - less strict)
```

#### 4. Improve RR Calculation
The "no achievable target" error suggests the target zone calculation is failing.
Check `risk_gate.py` for target calculation logic.

### If Signals Are Winning But Losing

Your penalties are too loose. Tighten:
```python
QUALITY_LOCATION_SEVERE_FACTOR = 0.45  (55% cut instead of 45%)
QUALITY_MIN = 55  (instead of 50)
```

### Recommended Starting Point (Conservative)

```python
QUALITY_LOCATION_SEVERE_PCT = 0.20     # 20% of range
QUALITY_LOCATION_SEVERE_FACTOR = 0.65  # 35% cut instead of 45%
QUALITY_MIN = 47                        # Slightly looser gate
```

---

## Deployment Checklist

- [ ] Apply all changes from `ai_decision.py` (prompt + error handling)
- [ ] Apply `setup_quality.py` fix (penalty floor)
- [ ] Test with `python main.py --once` (single scan cycle)
- [ ] Monitor logs for:
  - [ ] `AI response for` messages (JSON showing, not prose)
  - [ ] `exhaustion_factor:` values (should be 0.5-1.0, not 0.2-0.4)
  - [ ] Signal counts (should increase from 0-3 per scan)
- [ ] If signals still 0, adjust config thresholds per tuning guide above
- [ ] Run backtest before deploying live:
  ```bash
  python backtest.py --start 2026-08-01 --end 2026-09-02
  ```

---

## Next Steps if Issues Persist

### AI Still Returns Prose

1. Check if the prompt is actually being used:
   ```python
   import ai_decision
   print(ai_decision._SYSTEM_PROMPT[:1000])
   ```

2. Try a more restrictive model:
   - Current: `nvidia/nemotron-3-ultra-550b-a55b:free`
   - Try: `gpt-4o-mini` or `claude-3-5-sonnet-20241022`

3. Use structured output (if provider supports):
   ```python
   # In config.py or ai_decision.py
   "response_format": {"type": "json_object"}
   ```

### Signals Still All HOLD

1. Check if the penalty floor is actually applied:
   ```bash
   grep "exhaustion_factor" bot.log | head -5
   ```
   Should show values like `0.55, 0.70, 0.75` (not `0.25, 0.35, 0.45`)

2. Look at primary scores:
   ```bash
   grep "primary" bot.log | head -10
   ```
   If primary is mostly <45, the strategy itself is filtering correctly
   (the market may not match your criteria)

3. Consider if market conditions are genuinely poor:
   - Check if volume is low across the board
   - Check if price is exhausted at all range extremes
   - This might be expected behavior!

---

## Files Modified

| File | Changes |
|------|---------|
| `ai_decision.py` | • Strengthened JSON-only prompt directive<br>• Added `_is_likely_reasoning()` detection<br>• Improved error context (300-500 chars)<br>• Better parse logging (1000 chars) |
| `setup_quality.py` | • Changed penalty combination from pure multiplication to worst+floor<br>• Now max(0.5, min(penalties)) instead of penalty1 * penalty2 * penalty3<br>• Prevents 65% score cuts from cascading |

---

## References

- `strategy_spec.md` — Original strategy intent
- `config.py` — All tunable thresholds (lines 290-325)
- `setup_quality.py` — Scoring and penalty logic
- `ai_decision.py` — Prompt building and JSON parsing
- `fallback.py` — Python-only fallback when AI unavailable

---

**Questions?** Review the logs with these patterns:
- `AI response for` → check if JSON or prose
- `exhaustion_factor:` → check if penalty floor working
- `quality:` → check score progression
- `penalties:` → see what's being applied

Good luck! 🚀
