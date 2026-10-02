# Phase 2 — a portfolio options market maker: profitable at zero latency, not at realistic latency

**Date:** 2026-09-30. **Strategy:** `mm_v1`, frozen in `config/frozen/mm_v1.yaml`
(sha256 `4f144314…`) before any holdout run. **Code:** `src/bnfmm/sim/portfolio.py`,
`scripts/mm.py`. **Data:** Tier A only — real captured 20-level books and a trade tape
differenced from the real quote feed. **No order was placed; no trading endpoint exists in this
repository** (`tests/test_no_order_placement.py`).

## ⚠ Read this first — the result does not survive realistic latency

Everything below the next heading was produced with **zero order latency**: a quote is live the
instant the book that motivated it is received, and can be filled by any trade received after
that. Re-running the *same frozen strategy* with a symmetric order-latency model (a new order
becomes fillable, and a cancelled one stops being fillable, only after the latency has elapsed)
changes the answer (added 2026-09-30, `DECISIONS.md` #23):

| Frozen mm_v1, holdout (5 days) | Net, member | Liquidated at close | Days positive | R² range |
|---|---|---|---|---|
| 0 ms (headline below) | +199,537 | +155,159 | 5/5 | 0.002–0.18 |
| **500 ms** | **−40,500** | **−84,754** | 1/5 | 0.001–0.16 |
| 1000 ms | −96,808 | −136,953 | 1/5 | 0.001–0.07 |

Develop days (Aug 27+28) show the same slope: +127,594 → +13,323 → −33,823.

**What latency is realistic.** Trades reach us 0.8–1.0 s (median) after their exchange
timestamp, which is truncated to the second, so the true print delay is ≈0.3–0.5 s. An order
therefore competes only for prints received at least (print delay + order latency) after it was
placed — ≈0.4–0.8 s for a non-co-located participant on these feeds. That is the range where
the strategy goes from breakeven to losing.

**So the defensible conclusion is:** the four structural changes below turn a strategy that was
being picked off into one that genuinely captures spread and is market-neutral — but on this
data, the captured edge is smaller than the cost of not being fast. In BRIEFING §11.6's terms,
*the latency slope answers "strategy or co-location bet"*, and it answers "co-location bet".
The zero-latency figures remain the right way to measure *what the quoting logic captures*;
they are not a statement of what a real participant would have made.

Two further robustness checks, same frozen strategy, develop days:

* **Book source.** With the depth feed merged causally with the 225 ms-fresher quote feed
  (strictly more information), net falls from +127,594 to +69,666. Built from the quote feed
  alone (on the same 200 ms decision clock) it is −39,520. Roughly half the zero-latency edge
  depends on the depth feed's representation of the book.
* **Longer-dated options (Oct series, and Sep series on Aug 24/25).** These exist only on the
  quote feed. Because the quote-feed adapter could not reproduce the depth-feed development
  result (above), the frozen strategy was **not** run on them: a number from an unvalidated
  adapter would not mean anything.

## The zero-latency result

On five unseen BANKNIFTY sessions (2026-08-31 → 2026-09-04, 25–29 DTE), the frozen strategy made
money on **every day**, at exchange-member cost:

| Holdout day | DTE | Net, member | Net, liquidated at close | Net, Dhan ₹20/order | Spread capture | Inventory | R² on forward |
|---|---|---|---|---|---|---|---|
| 2026-08-31 | 29 | +50,263 | +40,062 | −42,013 | 226,794 | −82,777 | 0.050 |
| 2026-09-01 | 28 | +29,558 | +20,047 | −41,619 | 175,183 | −68,710 | 0.002 |
| 2026-09-02 ½ day | 27 | +30,429 | +24,497 | −10,210 | 110,300 | −39,018 | 0.183 |
| 2026-09-03 | 26 | +43,109 | +32,642 | −34,276 | 141,067 | −31,937 | 0.004 |
| 2026-09-04 | 25 | +46,178 | +37,911 | +6,624 | 82,350 | +10 | 0.103 |
| **Total** | | **+199,537** | **+155,159** | **−121,494** | **735,694** | **−222,432** | |

Rupees, one lot (30 units) per quote. *Liquidated* closes every open position at the touch —
longs at the bid, shorts at the ask — and pays that trade's statutory cost; it is the number that
does not depend on any mark.

* **It is market making, not a directional bet.** Spread capture is 3.3× the size of the
  inventory term, and the inventory term is *negative* — the strategy pays for its inventory
  rather than profiting from it. Per-minute PnL regressed on the parity forward gives R² between
  0.002 and 0.18 (the 0.18 is the half-day session, 180 observations). Phase 1's only profitable
  run was 58% inventory PnL; this one is −30%.
* **It is statistically distinguishable from zero.** Per-fill realised PnL at five minutes, net
  of member cost: mean ₹13.64 over 13,603 fills; 95% CI **[₹6.34, ₹20.40]** by a 5-minute block
  bootstrap (309 blocks), which respects autocorrelation. Five of five days positive: one-sided
  sign test p = 0.031.
* **It degrades out of sample, by about a third.** Develop days (Aug 27, 28): +₹63.8k/day.
  Holdout: +₹39.9k/day. Reported, not hidden: a strategy that did not degrade would be the
  suspicious one.
* **It needs exchange-member costs.** On Dhan's flat ₹20 per order at one lot it loses
  ₹121,494 over the holdout (one day positive). This is BRIEFING §17.6's conclusion, now measured
  on a working strategy: the statutory floor is survivable, the flat retail fee at one lot is not.

**How many configurations were tried: 26**, all on develop days, logged in
`reports/config_log.jsonl`. The holdout was read once for this evaluation and once more for the latency sensitivity above (`reports/holdout_log.md`).

## What changed from Phase 1, and why each change is principled

Phase 1 (BRIEFING §17) quoted one strike at a time and lost money on every configuration. Four
changes turned it around. Each was motivated by a measurement, not by the PnL it produced.

1. **One portfolio, one delta, one vega.** Phase 1 ran each leg with its own inventory, so short
   calls and long puts — one bet taken twice — were never netted. `mm_v1` quotes every strike in
   the ±11 band on one clock, measures exposure with Black-76 delta and vega (implied from the
   option's own fair value; used for *risk only*, never for price), skews quotes against
   portfolio delta, and hard-limits |delta| to 5 lots and |vega| to 3 ATM-lots.
2. **Orders keep their place in the queue.** Phase 1 cancelled and re-placed every quote every
   200 ms, so it was always at the back of its level and could only fill when an interval
   cleared the whole displayed queue — the large, informed flow. Here an order whose price is
   unchanged keeps resting and its queue depletes against traded volume (the unchanged
   `DepletionSimulator`).
3. **Round-trip cost, split symmetrically.** Gating each side on its own cost placed asks
   further from fair value than bids (sell-side STT is 19.2 bp against 4.5 bp on the buy side),
   so the book bought more than it sold and drifted to 150 ATM-lots long vega. Splitting the
   same 23.7 bp round trip evenly balances the fill rates.
4. **Quote only books wide enough to pay for themselves.** Cost and adverse selection both
   scale with premium; the spread in ticks does not. A book is quoted only while its spread is
   at least **37.8 bp of fair value = round-trip cost 23.7 bp + 2 × measured adverse selection
   7.05 bp**. The threshold was derived from those two measured quantities, not read off a PnL
   table; neighbouring values (30, 45 bp) were run afterwards on develop days and were also
   profitable, so the result does not rest on the exact number.

Plus one rule written before any data existed: no quoting within 3 days of expiry
(`config/instruments.yaml`, 2026-08-23). It excludes the Aug 24/25 sessions, where the realised
spread was zero — expiry-day gamma turns every fill into a pick-off.

**Fair value** is a precision-weighted blend of the option's own microprice and its parity value
(the other leg's microprice + D(F − K), with F the cross-strike parity forward). A wide ITM book
defers to its tight OTM partner. This is where the money is: the profitable books are the wide
in-the-money ones, whose fair value the liquid out-of-the-money option at the same strike pins
far more tightly than their own spread does.

## Levers tried and rejected (develop days)

| Lever | Result |
|---|---|
| Fresher book: merge depth with the 225 ms-fresher quote feed, causally (`book/merge.py`) | No change in realised spread |
| Pull the threatened side on 5-level imbalance > 0.4 | Failed the falsification test: realised spread unchanged |
| Vega skew (0.5, 2.0) | More trading into adverse fills; worse |
| Join the touch instead of improving it | Lower realised spread per unit |
| Microprice vs mid as the fair value (scored against future prints) | Indistinguishable; the L1 microprice's lean reverts within a second |

## Robustness (develop days, Aug 27 + 28, member net / liquidated)

| Spread gate | Pessimistic queue | Optimistic queue |
|---|---|---|
| 30 bp | +119,201 / +96,838 | +123,635 / +102,142 |
| **37.8 bp (frozen)** | **+127,594 / +105,441** | +123,705 / +102,710 |
| 45 bp | +94,203 / +75,853 | +103,345 / +84,473 |

The queue convention moves the result by under 10%. Note the optimistic queue is *not* always
better: more fills from the front of the queue include more adverse ones. "Pessimistic ≤
optimistic" holds for fill counts, not for PnL, and is not claimed.

## Limitations — read before quoting any number

* **Simulated fills.** Snapshot-to-snapshot depletion on a ~200 ms feed (BRIEFING §17.1): queue
  position is approximated, hidden orders are invisible, and the tape is inferred from cumulative
  volume with the aggressor side classified by Lee-Ready. Every quote improving the touch is
  assumed first in its level; the pessimistic convention governs quotes that join a level.
* **Latency is decisive** — see the section at the top. The headline tables are zero-latency;
  at 500 ms the holdout loses. The depth feed itself already lags the quote feed by ~225 ms
  (`reports/data_integrity.md`).
* **Exchange-member costs** are the favourable bound. Retail at one lot loses.
* **One expiry cycle, 25–33 DTE.** Five holdout sessions of one monthly series cannot show how
  the edge varies across volatility regimes or expiry cycles. Near-expiry sessions are excluded
  by rule, and the result says nothing about them except that the realised spread there is zero.
* **One lot per quote; capacity untested.** Our fills are ~0.3–0.4% of the depth band's traded
  option volume (0.36% on 2026-08-31, 0.32% on 2026-09-03), but no market-impact model exists,
  and larger size was not run out of sample.
* **The 7.05 bp adverse-selection input** was measured on the develop days it was then applied
  to. The holdout is the test of whether it transferred; it did.
* **The holdout is now spent.** It was read twice: once for the frozen evaluation, once for the
  latency sensitivity of the *same* frozen strategy (`reports/holdout_log.md`). Any change to
  `mm_v1` is `mm_v2`, and it has no clean holdout left in this corpus.

## Reproducing

```bash
.venv/bin/python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8
```

That runs the develop days. The holdout requires `--unlock-holdout "REASON"`, which is logged.
Per-fill data from the holdout run is in `reports/mm/fills-holdout-mm_v1-v0-*.npz`.
