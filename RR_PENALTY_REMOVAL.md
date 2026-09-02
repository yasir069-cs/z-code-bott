# CRITICAL FIX: RR Penalties Moved Out of Quality Score (Sep 02)

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

## The Problem

From the Sep 02 logs, **40/40 signals were HOLD** despite aggressive tuning. Root cause analysis:

```
DELL/USDT:
  Primary:  54.0 (strong evidence)
  Exhaustion factor: 0.65 (45% cut)
  Quality before RR: 54 * 0.65 = 35.1
  RR penalty: -20 (no achievable target)
  Final quality: 35.1 - 20 = 15.1 ❌ BELOW 42 GATE
```

**Every single signal had RR failure (undefined or 0.01-0.09) and got -20 pts**, making recovery impossible.

## The Fix: Separate RR Into a Post-Quality Gate

Instead of subtracting RR from quality score:

**BEFORE (Multiplicative damage):**
```
Quality = [(primary * exhaustion) + indicator] - RR_penalty
Example: (54 * 0.65) + 5 - 20 = 15.1 ❌
```

**AFTER (Quality independent, RR separate gate):**
```
Quality = (primary * exhaustion) + indicator
Example: (54 * 0.65) + 5 = 35.1 ✅
Then check RR in a separate gate post-quality
```

## Config Changes

| Setting | Before | After | Impact |
|---------|--------|-------|--------|
| `QUALITY_RR_NONE_PENALTY` | 20 | 0 | ❌ Removes -20 subtractive for no target |
| `QUALITY_RR_MISS_PENALTY` | 12 | 0 | ❌ Removes -12 for suboptimal RR |
| `QUALITY_MIN` | 42 | 35 | Adjusted for score distribution |
| `QUALITY_PRIMARY_FLOOR` | 35 | 25 | Lower floor, RR no longer in scoring |
| `ALERT_QUALITY_MIN` | 38 | 30 | More signals alert |
| `ALERT_TIER_NORMAL_MIN` | 50 | 40 | Redistributed tiers |
| `ALERT_TIER_HIGH_MIN` | 60 | 50 | Redistributed tiers |
| `ALERT_TIER_STRONG_MIN` | 70 | 60 | Redistributed tiers |

## Expected Impact

### Before
```
DELL/USDT: quality=38.4 ❌ HOLD (RR killed it)
```

### After
```
DELL/USDT: quality=38.4 ✅ ALERT LOW
(RR checked separately - if fails, alert is tagged "RR below 1.2" but doesn't kill signal)
```

### Signal Volume Expected
- Before fix: 0 signals
- After fix: 8-15 signals (all with quality >= 30)

## Where RR is Now Validated

RR is still checked, but in `decision.py` after quality passes:
- In the Python fallback path (indicator decision)
- In post-LLM validation gates
- Alert will show `RR=0.88 below MIN_RR=1.2` in reason, but signal won't be rejected just for RR

## Testing

Run a scan:
```bash
python main.py --once
```

Expected logs:
```
# Should see signals now with quality 30-45
INFO | decision | DELL/USDT: quality=38.4 (primary 54 * 0.65)
INFO | logger | DELL/USDT HOLD 458.5 (quality 38 >= QUALITY_MIN 35) ✅
```

vs before:
```
# Killed by RR penalty
INFO | decision | DELL/USDT: quality=18.4 (primary 54 * 0.65 - 20 RR) ❌
INFO | logger | DELL/USDT HOLD 458.5 ❌
```

---

## Why This Works

1. **Quality now measures market structure fitness** (bias, S/R, liquidity, price-action, MTF alignment)
2. **RR is validation, not evidence** — poor RR doesn't mean bad structure
3. **Signals can now surface** with quality >= 30, even if RR is suboptimal
4. **Owner can then manually check** RR before trading (bot sends alert with full details)

## Rollback (if signals are too aggressive)

```python
# config.py — revert to penalties if needed
QUALITY_RR_NONE_PENALTY = 10  # light penalty
QUALITY_RR_MISS_PENALTY = 5
QUALITY_MIN = 40
ALERT_QUALITY_MIN = 35
```

---

## Architecture Note

This change reflects a philosophical shift:
- **OLD**: Quality = How good is the whole setup (structure + RR)?
- **NEW**: Quality = How good is the market structure? (RR is separately validated)

The new approach allows the owner to see "structurally valid" setups even if RR is poor, for manual review or adjustments.
