# RV strategy #1 — smile relative value: pre-registered NO-GO

**Date:** 2026-10-02. **Branch:** `rv-market-maker`. **Protocol:** `config/frozen/protocol_rv.yaml`
(sha256 `9b070f78…`), committed and pushed before any statistic below was computed.
**Script:** `scripts/rv_persistence.py`. **Data:** develop days 2026-08-27 and 2026-08-28, front
month, 23 strikes, sampled every second. Pure measurement — no strategy, no fills.

## Question

The classic market maker's edge decays inside the ~0.5 s it takes a non-co-located participant to
act (`DECISIONS.md` #23). Do *cross-strike* mispricings — an option rich or cheap against the
smile its neighbours imply — last long enough, and pay enough, to trade with that latency?

## Method

Each second: parity forward F; implied vol of each strike's OTM option from its mid; a quadratic
smile in ln(K/F) weighted by (vega / half-spread)²; each option valued **leave-one-out** (by the
smile the other strikes imply), residual r = mid − smile value.

## Result

| | Measured | Pre-registered bar | |
|---|---|---|---|
| Residual autocorrelation at 5 s | **0.587** (1 s 0.718, 30 s 0.467, 300 s 0.297) | ≥ 0.5 | PASS |
| Correction toward the smile after a 1 s entry delay, \|r\| > half-spread | **+4.46 bp** at 300 s (se 0.29); +1.62 at 30 s; +0.68 at 5 s | > 11.85 bp (per-side member cost) | **FAIL** |

For comparison, with no entry delay: +1.65 / +2.63 / +5.50 bp at 5 / 30 / 300 s. Typical residual
size 11.6 bp against a typical half-spread of 21.6 bp (medians over options).

## What it means

* **The latency problem is solved by this signal and the cost problem is not.** Smile
  mispricings are persistent and predictive, and a full second's delay costs only about 1 bp of
  the 300 s correction. But the correction (~4.5 bp) is less than half the statutory cost of a
  single fill (11.85 bp), and most mispricings sit inside the bid-ask spread.
* By the pre-registered rule, strategy #1 is **not built**. In particular it is not rescued by
  re-defining the bar after seeing the number.
* The signal remains a candidate *ingredient* — e.g. a better fair value for a passive quoter,
  where it would add to spread capture rather than have to pay a full fill cost alone. That is a
  new hypothesis and would need its own pre-registration.

## Caveats

Two develop days, one expiry cycle, 25–33 DTE. Quadratic smile only (the pre-registered default).
Observations overlap heavily in time, so the standard errors above understate uncertainty; the
conclusion does not depend on them — the gap to the cost bar is a factor of 2.7.
