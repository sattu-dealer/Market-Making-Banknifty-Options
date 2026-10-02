# RV strategy #2 — patient liquidity provision: pre-registered NO-GO

**Date:** 2026-10-02. **Branch:** `rv-market-maker`. **Criteria:**
`config/frozen/protocol_rv_patient.yaml`, committed and pushed (`c062633`) before any statistic
below was computed. **Script:** `scripts/rv_patient.py`. **Data:** develop days 2026-08-27/28,
front month, all 46 options of the depth band.

## Question

Can resting orders placed far from fair value — so they never need to win a race — get paid for
absorbing large, temporary dislocations? An order rests at fair·(1 ∓ δ), with fair the book mid
**1 s before** the print (when it was placed). A print at or through its price fills it, at its
price; after a fill the side is empty until a replacement is placed 1 s later. Markout at τ is
measured against the round-trip member cost (the position has to be exited).

## Result

| δ | τ | Fills/day | Mean markout (bp) | Minute-clustered se | t vs 23.71 bp |
|---|---|---|---|---|---|
| 50 bp | 30 s | 6,508.5 | −3.75 | 4.59 | −5.99 |
| 50 bp | 300 s | 6,431.5 | +17.55 | 14.25 | −0.43 |
| 100 bp | 30 s | 687.5 | +16.78 | 10.04 | −0.69 |
| 100 bp | 300 s | 684.5 | +21.63 | 29.91 | −0.07 |
| 200 bp | 30 s | 28.5 | −5.75 | 48.36 | −0.61 |
| 200 bp | 300 s | 28.5 | −4.34 | 49.63 | −0.57 |

GO needed one cell with mean > 23.71 bp, t ≥ 3 and ≥ 50 fills/day. **None passes. NO-GO.**

## What it means

* **Close-in patience is still picked off.** At δ = 50 bp the 30 s markout is negative: a price
  set from a 1-second-old fair value is reached by fast moves, and those fills are the adverse
  ones. Distance from the touch does not remove latency risk unless it exceeds the moves that
  happen within the latency.
* **Further out, dislocations revert — but not by enough.** At δ = 100 bp the 5-minute reversion
  (+21.6 bp) is just under the 23.7 bp round trip, with a standard error larger than the effect.
  There is no evidence of an edge that pays for itself, and at δ = 200 bp almost nothing fills.

## Caveats

* Resting prices were not rounded to the ₹0.05 tick, so a print essentially never lands exactly
  on them; the "at-or-through" and "trade-through" variants therefore coincide. Rounding away
  from the market would move fill prices by at most half a tick and does not change the verdict.
* Two develop days; fills within a minute share shocks (hence clustered errors, which are large).
* The derived tape books an interval's volume at its last print, which is, if anything, generous
  to deep resting orders (a sweep is booked at its deepest price).
