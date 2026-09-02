# Config Tuning — Aggressive Loosening to Allow Signals Through

## Changes Made (Sep 02)

### Primary Quality Gates
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `QUALITY_PRIMARY_FLOOR` | 45 | 35 | -28% — let weak primary through to quality calc |
| `QUALITY_MIN` | 50 | 42 | -16% — final gate now more permissive |
| `IND_CONFIRM_BONUS_MAX` | 10 | 15 | +50% — indicators can rescue more |
| `IND_CONFLICT_PENALTY_MAX` | 15 | 10 | -33% — less punitive for conflicts |

### Location Exhaustion Penalties
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `QUALITY_LOCATION_SEVERE_PCT` | 0.15 | 0.20 | +33% — more room to edge allowed |
| `QUALITY_LOCATION_MODERATE_PCT` | 0.30 | 0.35 | +17% — wider moderate zone |
| `QUALITY_LOCATION_MILD_PCT` | 0.45 | 0.50 | +11% — bigger mild penalty zone |
| `QUALITY_LOCATION_SEVERE_FACTOR` | 0.55 | 0.65 | -35% cut instead of -45% |
| `QUALITY_LOCATION_MODERATE_FACTOR` | 0.75 | 0.80 | -20% cut instead of -25% |
| `QUALITY_LOCATION_MILD_FACTOR` | 0.90 | 0.95 | -5% cut instead of -10% |

### Volume & RSI Penalties
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `QUALITY_WEAK_VOLUME_FACTOR` | 0.90 | 0.95 | -5% cut instead of -10% |
| `QUALITY_DECLINING_VOLUME_FACTOR` | 0.95 | 0.98 | -2% cut instead of -5% |

### Reward/Risk Penalties
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `QUALITY_RR_NONE_PENALTY` | 30.0 | 20.0 | -33% — less harsh on no-target |
| `QUALITY_RR_MISS_PENALTY` | 20.0 | 12.0 | -40% — gentler on suboptimal RR |
| `MIN_RR` | 1.5 | 1.2 | -20% — accept tighter RR setups |

### Confluence Gates (Pre-Quality)
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `MIN_SCORE_1H` | 55 | 50 | -9% — more 1H candidates pass |
| `MIN_SCORE_15M` | 50 | 45 | -10% — more 15M candidates pass |
| `MIN_SCORE_5M` | 50 | 45 | -10% — more 5M candidates pass |
| `MIN_CONFLUENCE` | 55 | 50 | -9% — overall confluence gate looser |

### Alert Thresholds
| Config | Before | After | Change |
|--------|--------|-------|--------|
| `ALERT_QUALITY_MIN` | 50 | 38 | -24% — more signals trigger alerts |

---

## What This Means

### Scoring Example (XAUT/USDT from earlier)

**Before:**
```
Primary: 46.0
Exhaustion factor: 0.35 (0.55 * 0.90 * 0.70)  ← multiplicative cascade
Quality: 46 * 0.35 - 30 (RR) = 16 - 30 = -14 → 0 (HOLD)
```

**After:**
```
Primary: 46.0
Exhaustion factor: 0.65 (worst of penalties with floor)  ← smart max
Quality: 46 * 0.65 - 20 (RR) = 30 - 20 = 10 → boosted by indicators → ~25-30
Result: Might now alert if indicator agreement helps (IND_CONFIRM_BONUS_MAX raised)
```

### Expected Signal Volume

**Before tuning:**
- 37/37 candidates → 0 signals

**After tuning (target):**
- 37/37 candidates → 3-7 signals expected
- Mix of NORMAL (50-60) and HIGH (60-70) confidence alerts

---

## Testing

Run a scan and check for:

```bash
python main.py --once
```

Look for in logs:
1. `signals X (log_only Y, holds Z)` — X should be > 0
2. `quality:` scores in the 38-50 range (alerts)
3. `AI response for` showing JSON (not prose)
4. `exhaustion_factor: 0.6-0.8` (not 0.3-0.4)

---

## If Still No Signals

This tuning is aggressive. If still 0 signals, check:

1. **Market conditions** — All 37 coins may be genuinely in poor setups
   - Check TradingView for EMA21/VWAP alignment
   - Look for range-bound vs trending markets

2. **Gate-out locations** — Check logs for gate rejection reasons:
   ```bash
   grep "insufficient_frame" bot.log | wc -l
   grep "no_directional_bias" bot.log | wc -l
   grep "counter_htf" bot.log | wc -l
   ```

3. **Score distribution** — Check actual scores:
   ```bash
   grep "quality:" bot.log | head -20
   ```
   If all scores < 38, then primary evidence is weak across the board.

4. **Further loosening** (last resort):
   - `QUALITY_PRIMARY_FLOOR = 25` (from 35)
   - `QUALITY_MIN = 35` (from 42)
   - `MIN_CONFLUENCE = 45` (from 50)

---

## Rollback

If signals are *too aggressive* after tuning:

```python
# config.py rollback
QUALITY_PRIMARY_FLOOR = 40       # middle ground
QUALITY_MIN = 45
MIN_CONFLUENCE = 52
QUALITY_LOCATION_SEVERE_FACTOR = 0.60  # between old/new
```

---

## Files Changed
- `config.py` — 10 threshold adjustments

