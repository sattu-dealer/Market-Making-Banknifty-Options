# Two days of BANKNIFTY option market making: what the simulation found

**Status:** complete for the two days captured. Every number below is measured, not
projected. Read §7 before quoting any figure out of context.

---

## 1. The one-paragraph answer

A two-sided quoter was run against real captured order books for the two highest-
information sessions available — 2026-08-24 (1 DTE) and 2026-08-25 (0 DTE, expiry
day) — at the at-the-money strike, both legs. **It does not work.** The single
profitable configuration found (member net +₹92,433) earned 58% of its gross PnL
from directional inventory rather than from quoting, and the same configuration
lost ₹207,712 the next day. With the directional component squeezed out by tighter
inventory skew, the result goes to −₹4,664 and then −₹54,582. Spread capture never
covers costs in any configuration tested, on either day. On Dhan's ₹20 flat
brokerage, every configuration loses heavily at one-lot size.

The finding is not "the strategy needs tuning". It is that **the statutory cost
floor consumes most of the available spread** at the only strikes with enough flow
to matter, and what remains is smaller than the measured adverse selection.

## 2. What is real and what is simulated

`BRIEFING.md` §2 makes this non-negotiable, so it comes before the results.

| Component | Status |
|---|---|
| Order books, 20 levels, both days | **Real.** Captured live from Dhan's depth feed. |
| Trade tape | **Derived** from real data — differenced cumulative volume. Lossy; see §7.2. |
| Aggressor side of each trade | **Inferred.** Lee-Ready quote rule; not published by the feed. |
| Forward price | **Derived** from real option books by put-call parity. |
| Our own fills | **Simulated.** No order was ever placed. See §7.1 for what the model assumes. |
| Costs | **Real published rates** (`config/costs.yaml`), pending the verification in §7.5. |
| Index spot | **Absent.** Never captured — see §7.4. |

No live capital was deployed, no order was placed, and no trading endpoint is wired
up anywhere in this repository (`tests/test_no_order_placement.py` enforces this).

## 3. The economics before any simulation

Options STT is levied on **premium**, not notional, so the round-trip cost floor
scales roughly linearly with the option's price while the bid-ask spread does not.
This single fact determines where a market maker can operate.

At the 2026-08-24 at-the-money call (median mid ₹203.60, median spread 11 ticks):

| Profile | Lots | Breakeven (ticks) | vs 11-tick market spread |
|---|---|---|---|
| Member (zero brokerage) | 1 | **9.65** | 88% of the entire spread |
| Dhan (₹20 flat) | 1 | **41.12** | 3.7× the spread |
| Dhan | 5 | 15.95 | 1.45× |
| Dhan | 20 | 11.23 | 1.02× |

The cost is also **asymmetric**: at ₹190 premium a sell fill costs ₹10.95 against a
buy fill's ₹2.57 — 4.3× — because STT falls on the sell side and is 78% of that
₹10.95.

Screening all 46 contracts per day on arithmetic alone (`scripts/strike_economics.py`,
no simulation):

* **Member profile:** 45/46 clear the floor on Aug 24, 46/46 on Aug 25.
* **Dhan profile:** 10/46 and 9/46.

But headroom without flow is worthless — the deep out-of-the-money puts show +150
ticks of headroom precisely *because* nobody trades them. Ranking by headroom × flow
puts the near-the-money strikes on top with a ratio of only **1.2–1.4× the floor**.
That is the honest picture: the strikes with flow have almost no room, and the
strikes with room have no flow.

## 4. Results

At-the-money strike, both legs, 1 lot per side, 6-tick half-spread, 10-lot cap.

| Day | Anchor | Gross | Member net | Dhan net |
|---|---|---|---|---|
| 2026-08-24 | microprice | +23,007 | **−134,213** | −607,959 |
| 2026-08-24 | parity | +175,914 | **+92,433** | −185,788 |
| 2026-08-25 | microprice | −274,053 | **−382,286** | −1,017,339 |
| 2026-08-25 | parity | −139,728 | **−207,712** | −608,912 |

The half-spread sweep on Aug 24 (microprice anchor) shows the shape of the problem:

```
 ticks     fills       units         gross    net member       net dhan  eff/unit   adverse
   2.0    37,734   1,132,020       -70,244      -337,008     -1,227,530   +0.1072   +0.2059
   4.0    29,343     880,290       -11,540      -229,760       -922,255   +0.2068   +0.2711
   6.0    20,074     602,220        23,007      -134,213       -607,959   +0.3065   +0.3349
   8.0    12,441     373,230        17,931       -86,029       -379,637   +0.4062   +0.4518
  12.0     4,453     133,590        13,077       -31,025       -136,116   +0.6060   +0.6855
  16.0     2,047      61,410        14,784        -7,482        -55,791   +0.8063   +0.9536
  24.0       578      17,340        -7,353       -13,510        -27,151   +1.2072   +1.4925
```

**Adverse selection scales with the half-spread.** Widening does not escape it — at
every width, adverse selection exceeds the effective spread captured. You simply
trade less, at worse selection. There is no width at which this turns profitable;
the member column is negative throughout and merely gets closer to zero as volume
collapses toward nothing.

## 5. Why the one profitable run is not a result

The parity anchor's +₹92,433 was attacked before being reported. Two candidate
explanations, one ruled out and one confirmed.

**Ruled out — self-reference.** The consensus forward is built from all 23 strikes
*including the pair being quoted*. Algebraically this is conservative, since
`adj = D·(1 − w_K)·(F_rest − F_K)` shrinks toward the option's own microprice, but
the near-the-money pair carries the largest precision weight so the algebra alone
is not decisive. Rebuilding the forward from the other 22 strikes only
(`--exclude-self`) gives **+₹93,112** — unchanged, marginally better, as predicted.
Not circularity.

**Confirmed — it is a directional bet.** Gross PnL splits exactly:

    gross = Σ sᵢ(Mᵢ − Pᵢ)  +  Σ sᵢ(M_T − Mᵢ)
          = spread capture  +  inventory PnL

| | Spread capture | Inventory PnL | Inventory share |
|---|---|---|---|
| 57400 CE | 33,201 | 82,248 | **71%** |
| 57400 PE | 40,816 | 19,649 | 33% |
| **Total** | **74,017** | **101,897** | **58%** |

Spread capture of ₹74,017 against member costs of ₹83,481: **the quoting loses
money.** All the profit is inventory. The quoter sat at maximum short calls 45% of
the day and maximum long puts 40% — a synthetic short forward — on a day the market
fell.

Three confirmations:

* **Skew sweep** (squeeze out the directional exposure): +92,433 → −4,664 → −54,582
  as skew goes 0.5 → 2.0 → 6.0.
* **Day two**: the identical configuration loses ₹207,712.
* **All four day × anchor cells**: inventory dominates gross in every one.

Two days cannot distinguish a correct directional position from luck. This is
reported as a failure to demonstrate market making, not as a strategy.

## 6. Diagnosis: picked off, not inventory risk

Adverse selection measured at multiple horizons separates two diagnoses that a
single horizon conflates. Aug 24, 6 ticks, microprice anchor:

| Leg | 1 s | 5 s | 30 s | 300 s |
|---|---|---|---|---|
| CE | +0.2829 | +0.2540 | +0.3223 | +0.2219 |
| PE | +0.3461 | +0.3228 | +0.3486 | +0.3129 |

**Flat across horizons.** Already at full magnitude within one second and no larger
at five minutes. That is the signature of being *picked off*: we buy ~0.30 below the
mid and the mid immediately falls ~0.28, permanently. It is not inventory risk,
which would grow with the horizon and would be fixable by flattening faster. Since
the skew sweep independently shows that flattening faster destroys the PnL, both
diagnostics agree: there is no configuration of this strategy that works on this
data.

## 7. Limitations — read before quoting any number above

**7.1 The fill model cannot represent queue position.** The feed pushes a complete
snapshot every ~200 ms (median 201 ms, p05 157 ms, p95 442 ms) rather than
event-by-event, so true queue-position modelling is unsupportable and only
snapshot-to-snapshot depletion is honest. Our order joins the *back* of its price
level and is depleted only by traded volume, never by cancels ahead of it — which
understates fills, since cancels genuinely do improve queue position.

**7.2 The tape is lossy in three known ways.** Multiple trades inside one 200 ms
packet collapse to a single price; the aggressor side is not published and is
inferred by Lee-Ready with a tick-test fallback; `last_trade_epoch` has only
one-second resolution. Volume that cannot be classified is **discarded, not
guessed**.

**7.3 Brokerage is an upper bound.** Dhan charges ₹20 per *order*, not per fill.
Costs here are charged per fill, which overstates the fee whenever one order fills
in pieces. The member column is unaffected.

**7.4 There is no index spot.** Security ids 25 and 13 were subscribed with request
code 21 (Full), which Dhan does not serve for the `IDX_I` segment — an index has no
book. The subscriptions were silently ignored and **zero index rows exist**. The
forward is therefore derived entirely from option books. This is a capture bug, not
a design choice, and it is why parity does all the work.

**7.5 The 0.15% options STT rate is unverified.** `config/costs.yaml` carries the
warning "VERIFY BEFORE PUBLISHING ANY RESULT" and the entire conclusion in §3 turns
on it. Rates change by government budget and SEBI circular.

**7.6 Data gaps.** Aug 24: depth 09:27:01→15:34:59, 6 gaps >2 s totalling 51.1 s.
Aug 25: 09:15:02→15:34:59, 27 gaps >2 s totalling 367.2 s, clustered in the first
25 minutes, largest 88.1 s. Handled by standing the quoter down when data is stale
(`max_stale_ns`), which is visible in the per-run stand-down attribution.

**7.7 The closing mark can be stale.** Expiry-day books go one-sided into the 15:30
close, making the microprice NaN. Positions are marked at the last *finite*
microprice with the staleness disclosed — on Aug 25 the 57500 PE marks at ₹0.05,
210.8 s stale. No settlement price was captured, so none is invented.

**7.8 Two days, one strike, one expiry.** Nothing here generalises. Both days trade
the same 2026-08-25 monthly expiry; BANKNIFTY has no weeklies.

## 8. What would have to change

* **Fee structure is decisive, not incidental.** Nothing works on Dhan's flat ₹20 at
  one lot. At 20 lots the flat fee amortises to 11.23 ticks against a 9.65-tick
  member floor — but 20 lots is also the exchange freeze quantity, so that is the
  ceiling, not a starting point.
* **The strategy must stop being picked off**, and the horizon curve says quoting
  wider will not achieve it. That points at conditioning on order-flow imbalance
  before quoting, rather than quoting symmetrically at all times.
* **More days.** Two sessions of one expiry cannot separate skill from a directional
  bet, which is precisely what §5 ran into.

## 9. Reproducing

```bash
python scripts/strike_economics.py --date 2026-08-24
```

```bash
python scripts/backtest.py --date 2026-08-24 --strikes 1 --lots 1 --half-spread 6 --anchor parity
```

```bash
python scripts/backtest.py --date 2026-08-24 --anchor parity --exclude-self
```

```bash
python scripts/backtest.py --date 2026-08-24 --sweep 2 --sweep 4 --sweep 6 --sweep 8 --sweep 12 --sweep 16 --sweep 24
```
