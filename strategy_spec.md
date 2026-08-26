# Strategy Specification — SECONDARY (indicator confirmation layer)

> **Status (owner-directed):** the bot now decides on **market structure and
> market context first** — see [`price_action_spec.md`](price_action_spec.md),
> the **primary authority**. The six indicator conditions transcribed below are
> now the **secondary confirmation layer**: they fold into `setup_quality` as a
> bounded sub-score (`scoring.indicator_confirmation`) and **cannot trigger or
> veto a trade on their own**. RSI / EMA21 / VWAP / Bollinger are no longer hard
> gates or the direction-chooser.
>
> This file remains the faithful transcription of the owner's **handwritten
> indicator note** and the authority for *how those indicators are read*. The
> "Hard gates" and "confluence = 0.40/0.30/0.30" descriptions below document the
> **historical** indicator-checklist engine (`scoring.py`'s graded fns are
> retained and reused as the secondary scorer); they no longer describe how a
> trade is *decided*. For that, read `price_action_spec.md`.

## The note, verbatim

```
BUY Entry                                 SEL Entry

RSI 50 above to 70                        RSI 50 below to 35
EMA21 above                               EMA21 below
VWAP above                                VWAP below
volume increase                           volume increase
Bollinger band                            Bollinger band
  } Bottom to inbetween + liquidation       } Top to inbetween + liquidation
    sweep                                     sweep

TIME Frame 1H - 15M - 5M                  TIME Frame 1H - 15M - 5M
```

## Reading of the note

Six conditions, evaluated top-down across three timeframes (1H context →
15M confirmation → 5M entry).

| # | Condition | BUY | SELL |
|---|---|---|---|
| 1 | **RSI** | rising through 50 toward 70 | falling through 50 toward 35 |
| 2 | **EMA21** | price above | price below |
| 3 | **VWAP** | price above | price below |
| 4 | **Volume** | increasing | increasing |
| 5 | **Bollinger** | at/near the band | at/near the band |
| 6 | **Zone + sweep** | price in the **bottom → in-between** part of the range, with a liquidation sweep | price in the **top → in-between** part of the range, with a liquidation sweep |

The brace in the note groups the Bollinger line with "Bottom to inbetween +
liquidation sweep" — the band alone is not the point; **where in the range**
the setup sits is.

### "Bottom to inbetween" — the graded zone

The note does not say "bottom 30% only". It says **bottom to in-between**,
which is a spectrum, so the bot scores it as one:

| `range_pos` (BUY) | Meaning | Score |
|---|---|---|
| ≤ 0.30 | "Bottom" | full (25) |
| 0.30 – 0.60 | "in-between" | tapered 25 → 10 |
| > 0.60 | wrong half of the range | **reject** |

SELL mirrors this on `1 - range_pos`. `range_pos` is the close's position in
the 50-candle high/low range (`indicators.compute_indicators`).

### Liquidation sweep

A sweep is a wick that pierces a recent swing level, closes back inside, has a
wick > 2× its body, and carries a volume spike (`filter_1h.detect_sweep`).

The note lists it as a condition, so it carries the **heaviest single weight**
(25 points) and decays with age. It is **not** a hard gate: a setup with
everything else aligned but no sweep still alerts, at a reduced score, with its
confidence capped below HIGH and the alert explicitly labelled **"no sweep"**.
This is a deliberate, owner-approved choice — see the deviations table.

## Scoring model

`scoring.py` turns the six conditions into a 0–100 score per timeframe.

**Hard gates** (fail → coin is dropped, no score):
- EMA21 side and VWAP side — these *define* direction, so they cannot be partial
- RSI outside the wide tolerance band
- RSI overbought (BUY) / oversold (SELL)
- Bollinger bandwidth below `BB_BANDWIDTH_MIN` (sideways/squeeze)
- Zone in the wrong half of the range (BUY `range_pos` > 0.60)

**Graded** (0 → full points):

| Condition | 1H | 15M / 5M |
|---|---|---|
| Zone | 25 | — (1H only) |
| RSI | 20 | 40 |
| Volume | 15 | 30 |
| Bollinger | 15 | 30 |
| Sweep | 25 | — (1H only) |

`confluence = 0.40 × score_1h + 0.30 × score_15m + 0.30 × score_5m`

The confluence total is used three ways: to **rank** candidates so the strongest
get the scarce AI budget first, as a numeric **input to the AI prompt**, and as a
**line in the Telegram alert**.

## Timeframe roles

- **1H — context.** Decides the direction and carries zone + sweep. Fail here and the coin is dropped before 15M/5M are ever fetched.
- **15M — confirmation.** Same direction must still hold on the 3 graded indicator conditions.
- **5M — entry.** Final trigger; its close becomes the alert's entry price.

## Sanctioned deviations

Deliberate differences between this note and the code, with reasons.

| Deviation | Reason |
|---|---|
| Sweep is heavily weighted, not a hard gate | Owner's decision. Requiring it produced whole sessions with zero alerts (Aug 22: `pass_1h: 0`). Alerts without a sweep are labelled and confidence-capped. |
| RSI has a tolerance band (BUY 45–80) outside the note's 50–70 | The note's band scores **full**; the wider band scores **half** and is what production ran before. Prevents a hard cliff at exactly 50.0 / 70.0. |
| Zone "in-between" extends to 0.60 | The note gives no number. 0.60 is the midpoint of the range, i.e. the last point still on the favourable half. |
| Volume "increase" is graded, not binary | `> previous candle` scores full, `> 20-candle average` scores partial — a coin trending on sustained high volume shouldn't be rejected for one flat candle. |

## Out of scope (unchanged)

Signals only. The bot never places a trade, holds no exchange API keys, and
uses only public market data.
