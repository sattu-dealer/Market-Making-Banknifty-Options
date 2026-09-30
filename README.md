# BNFMM — how much does a naive fill assumption inflate market-making PnL?

A measurement study on NSE BANKNIFTY options, designed around self-captured
20-level order-book depth. *No market data has been captured yet* — see
[Status](#status) for what is built and what is still arithmetic.

**The claim this project makes is a methodology claim, not a profit claim.** Most
retail market-making backtests assume that a resting quote fills whenever the
market trades at its price. Real quotes sit in a FIFO queue behind other orders
and often do not fill at all — and the ones that *do* fill are
disproportionately the ones you wish had not. This repo sets out to measure the
size of that gap and decompose it.

> On captured BANKNIFTY depth, a naive instant-fill backtest reports **X**; a
> queue-position-aware simulator on the same data and the same strategy reports
> **Y**. Here is the gap, decomposed into spread capture, adverse selection,
> inventory, and fees.

`X` and `Y` are deliberately not filled in yet. See [Status](#status). When they
are, **neither will be a single number.** Every PnL figure in this project is a
2×2 grid over the two things that are genuinely uncertain:

|  | pessimistic queue | proportional queue |
|---|---|---|
| **member** (Rs 0 brokerage) | … | … |
| **retail** (Dhan Rs 20/order) | … | … |

The queue axis bounds something [unobservable in
principle](#honest-limitations); the brokerage axis is a deliberate choice not to
pick a flattering cost regime and call it the answer. If the four corners
disagree about the *sign* of the PnL, **naming the corner where it flips is the
finding** — and Finding 2 below suggests that is the likely outcome. Enforced in
code: `PnlGrid` cannot be constructed with a corner missing.

---

## Why this framing

A student project reporting profitable market making is not believable, and
inviting that scepticism wastes the interesting content. Inverting it costs
nothing — the same code produces both numbers — and turns the deliverable into a
measurement that survives an adversarial reading.

The Phase 0 cost analysis then made the reframe load-bearing rather than merely
tactful. At Indian statutory F&O rates, round-trip cost on a BANKNIFTY option is
**0.237% of premium**, which for a Rs 1,000 ATM premium is **Rs 2.37 per lot**
before brokerage and **Rs 3.94 with it** — the same order of magnitude as the
quoted spread itself. The margin is thin enough that **the fill assumption alone
can determine the sign of the PnL.** A naive backtest assuming full spread capture
will report a profit; a queue-aware simulator may not.

That makes the methodology choice decision-relevant rather than merely
quantitative, which is a much stronger result than "queue-aware fills reduce PnL
by some percentage."

---

## Findings so far

Reproduce all of these with `python scripts/phase0_recon.py`. Full write-up:
[reports/phase0_instrument_selection.md](reports/phase0_instrument_selection.md).
Reasoning for every design choice: [DECISIONS.md](DECISIONS.md).

Costs are quoted for both regimes throughout — **member** is Rs 0 brokerage, the
most favourable assumption that could plausibly be modelled and included as a
bound rather than a scenario; **retail** is Dhan's flat Rs 20 per executed order.

**1. BANKNIFTY futures market making is structurally unviable, in either regime.**
One tick is Rs 0.20 (Rs 6.00 per lot of 30). One round trip costs **Rs 980 per lot
at member rates, Rs 1,028 at retail**, of which **Rs 867 is STT alone**, because
futures STT is charged on contract notional — Rs 1.73 crore per lot. Breakeven is
**163 ticks (Rs 32.68) / 171 ticks (Rs 34.25)**. BANKNIFTY futures do not quote a
Rs 33 spread. The conclusion is regime-independent, which is what makes it solid:
brokerage moves it by 5% on a figure already an order of magnitude out of reach.
This is a property of the fee schedule, not of the strategy.

**2. Options are the defensible instrument, and are still marginal — and here the
regime matters.** Options STT is charged on premium, not notional.

| regime | round trip / lot | breakeven | vs futures |
|---|---|---|---|
| member | Rs 71.13 | 47 ticks = **Rs 2.37** | 14× better |
| retail | Rs 118.33 | 79 ticks = **Rs 3.94** | 9× better |

Two Rs 20 orders plus GST add **Rs 47 to a Rs 71 statutory bill — 66%**. At one
lot, brokerage is the *largest single component* of the round trip. A figure
stated without naming its brokerage regime is therefore uninterpretable, which is
why every figure here is paired.

**3. Delta hedging with futures is unaffordable.** Neutralising one ATM option
lot's delta costs **Rs 490 — 6.9× the round-trip cost of the position it hedges**,
or 327 option ticks. The same notional-based STT taxes every hedge. So the design
quotes the CE *and* the PE at one strike and manages residual delta by skewing
quotes, with no hedging leg at all.

**4. A fixed per-order fee makes quote size a decision variable — which the
standard model does not cover.** Statutory charges scale with quantity; brokerage
does not. So at retail, per-unit breakeven falls from **Rs 3.94 at one lot to
Rs 2.45 at the 20-lot freeze cap — a 38% cut in the hurdle rate from size alone**,
while brokerage's share of the round trip drops from 40% to 3%. In the member
regime per-unit cost is flat in size, which isolates the fixed fee as the cause.

Avellaneda–Stoikov (2008) derives an optimal *spread* from inventory risk and
order-arrival intensity and assumes no fixed per-trade cost — in that model,
quoting one lot and quoting twenty are the same decision scaled. With a flat Rs 20
they are not. Cost pushes toward large quotes, inventory risk and adverse
selection push back, and the exchange freeze quantity of 601 units caps the top at
20 lots. The cost half of that trade-off is
[`analysis/quote_size.py`](src/bnfmm/analysis/quote_size.py); the risk half needs
the fill simulator. This is the one piece of original analysis in the repo and is
labelled as a small extension, not a new model.

**5. Three contract facts that most references still get wrong.** BANKNIFTY has
**no weekly options** (withdrawn by NSE). Expiry day is **Tuesday**, not
Thursday. `TICK_SIZE` in the exchange instrument master is denominated in
**paise** — a 100× error there inverts finding 1. All three are read from the
instrument master at run time and pinned in
[`tests/test_master_facts.py`](tests/test_master_facts.py), which fails if NSE
changes them.

### A correction, recorded rather than quietly fixed

The first version of the cost table encoded Dhan's brokerage as
`min(Rs 20, 0.03% of turnover)` for both products. That is wrong: the "whichever
is lower" rule applies to equity delivery/intraday and MTF, **not** to F&O, where
the fee is flat Rs 20. It charged Rs 9 instead of Rs 20 per options order and
understated options breakeven at **Rs 3.08 against a true Rs 3.94 — 22% low**, on
a margin already close to the cost boundary. Futures were unaffected, because
0.03% of Rs 1.73 crore is Rs 520 and the flat fee always bound — which is exactly
why the bug hid.

The failure mode is the interesting part and it is worth stating plainly: the
*rates* were verified against the source, and the *applicability* of one rate was
not. The fix replaced a single scalar with named per-broker, per-product profiles
([DECISIONS.md #12](DECISIONS.md)), and added the two corrections that followed
from getting it right — brokerage is charged per **executed order**, not per fill
(a five-lot quote filling in five pieces pays Rs 20 once, not Rs 100), and quote
size becomes finding 4 above.

---

## Honest limitations

Stated here rather than buried, because they are the first things worth asking
about.

**Retail feeds are throttled snapshots, not event streams.** A broker WebSocket
delivers periodic depth snapshots, not every book event. True L3 event-by-event
queue tracking is therefore **not reconstructable in principle** from this data:
between two snapshots, at one price level,

```
Δqty = adds − cancels − trades
```

Trades are observable, so `adds − cancels` is recoverable — but `cancels` alone
is not, and cancellations *ahead of* a resting order are exactly what determines
whether it fills. Rather than pick a convention and hope, the simulator reports
**both bounds**:

- *pessimistic* — all queue shrinkage happens behind the order; it never advances
  on cancels. Lower bound on fills.
- *proportional* — cancels distributed uniformly across the queue. Upper bound.

Every PnL figure is consequently an **interval, not a point**. This is a stronger
answer than a single number, because the first question about a single number is
"how did you know?"

**And trades are only *partly* observable, which was the surprise.** The 20-level
depth feed carries no trade prints at all, so capture runs a second WebSocket (see
[Method](#method)) whose `volume` field is cumulative for the day — traded quantity
over an interval is a difference between consecutive packets. That gives the
*total* traded quantity but not its distribution across price levels, since only
the most recent print's price is reported. The saving grace is that the ambiguity
is **detectable per interval rather than merely feared**: when the volume
difference equals the last print's size, exactly one trade occurred and its price
is known. So the fraction of intervals where it does *not* is a measurable
quantity, and it gets reported rather than assumed away
([DECISIONS.md #15](DECISIONS.md)).

**Shadow mode is an upper bound, not a live result.** The same strategy code runs
over replayed Parquet and over a live session (`EventSource` + `Clock`
abstractions, so the engine cannot tell them apart). Running live removes two of
the three standard backtest lies: look-ahead becomes *impossible* — the future has
not arrived — and latency becomes real and measured rather than modelled. It does
**not** remove the third. **Queue position is still simulated**, because the order
was never actually in the book: there is no market impact and no self-selection
from having rested there. Shadow PnL is therefore reported as an upper bound and
labelled that way wherever it appears.

**No co-location.** Latency is modelled and its sensitivity reported, not
eliminated. Depth packets carry **no timestamp and no sequence number** — the
field an earlier reading of the protocol took for a sequence counter is not one
([DECISIONS.md #15](DECISIONS.md)) — so capture stamps arrival time locally, and
latency analysis is receive-side only and says so. A dropped depth packet is
therefore undetectable in principle. The only exchange-side clock anywhere in the
system is the second feed's last-trade timestamp, at one-second resolution, which
is enough to bound local clock drift but not to measure latency — and it only
advances when a trade happens, so in a quiet strike the check has no power. The
fraction of each segment where it is static is reported alongside it.

**Capture runs on a laptop, so gaps are the normal case.** The **unit of analysis
is a clean segment, not a session**: a gap ends a segment, and each segment passes
QA independently, because session-level QA would discard a whole day for one
30-second dropout. **A real 30-second dropout and a 30-second NTP step are the same
bytes in wall clock alone**, so capture records a monotonic clock beside the wall
clock on every row: deltas that agree mean a genuine gap, deltas that disagree mean
clock damage. Suspend/resume is the case that produces a 45-minute wall span against
a sub-second monotonic one, and it is *detected* rather than silently turned into
latency noise. Every excluded segment is listed with its reason in
`reports/data_quality.md`. Walk-forward folds split by **calendar day**, never by
segment — adjacent segments from one day on both sides of a split would leak.

**Data tiers are never conflated.** Every figure and table carries its tier:

| Tier | Source | Window | Role |
|---|---|---|---|
| **A — real L2** | Dhan 20-level depth capture | days–weeks | Calibrate and validate the fill model. The load-bearing tier. |
| **B — real 1-min** | Dhan `/charts/rollingoption`, `/charts/intraday` | 5 years | Strategy robustness across vol regimes and expiry cycles |
| **C — synthetic** | flow generator calibrated on Tier A | unlimited | Parameter sensitivity, stress tests |

This is enforced, not conventional: `tier` is a required field on every result
object, and the renderer **raises** if anything but Tier A reaches the headline
slot. Tier C is synthetic; Tier B is real, but 1-minute bars contain no queue at
all, so a queue-position claim built on them would be fiction with real
provenance — arguably the worse of the two failures.

**If the Data API is never subscribed to, the headline degrades and will say so.**
Depth is a paid add-on (see [Status](#status)). If it is not purchased, the
simulator, the attribution and the cost findings above still stand — but
"measured on real BANKNIFTY depth" becomes "measured on flow calibrated to
*assumed* parameters," which is a materially weaker claim. It will be written in
those words rather than blurred.

**Statutory rates need re-verification before publication.** `config/costs.yaml`
carries a `retrieved` date and a source. Futures STT has been revised repeatedly
(0.01% → 0.0125% → 0.02% → 0.05%); every headline number here depends on it.

**No live capital and no order placement.** This is a simulation study, including
in live sessions — shadow mode consumes the feed and places nothing. That is
enforced rather than intended: [`tests/test_no_order_placement.py`](tests/test_no_order_placement.py)
walks the **AST** of every file under `src/` and holds an **import allowlist** over
`dhanhq` submodules (so `_order`, `_super_order` and `_forever_order` are
unreachable by construction, including via aliased imports), plus checks for
`place_order`, `modify_order`, `cancel_order` and friends as identifiers, as
attribute accesses, and as string literals — so `getattr(dhan, "place_order")` and
a raw `/orders` route are caught too. No `broker/`, `execution/` or `oms/` package
exists, and the tests assert that too.

The AST is not incidental. The first version was a text grep, and it flagged
`data/auth.py` for a docstring that *explains* why it avoids `set_ip` — forcing
modules to stay silent about their own restraint to keep the guard green. **Prose
may name these things; code may not.** The detectors are themselves tested against
a synthetic offending module and a synthetic innocent one, so the guard cannot
quietly stop matching ([DECISIONS.md #14](DECISIONS.md)).

---

## Status

**There is no market data in this repo yet**, and nothing above claims otherwise.
Every number so far is arithmetic on a published fee schedule plus contract specs
read from the exchange instrument master.

| Phase | State |
|---|---|
| 0 — Instrument resolution, cost model, viability gate | **done** — findings above |
| 0b — Brokerage profiles, order-level costs, paired-reporting spine, guard tests | **done** |
| 0b — Auth + entitlement probe (`scripts/check_entitlement.py`) | **done** — awaiting credentials to run |
| 1 — Wire decoders for both feeds | **done** — 73 tests, no network needed |
| 1 — Buffered Parquet capture store | **done** — 44 tests, crash-safety and clock discipline included |
| 1 — Segment QA + Tier C flow generator | in progress |
| 2 — Clock/source abstractions, book reconstruction, OFI, microprice, put–call parity | not started |
| ★ **Gate** — subscribe to the Data API for one month | after phase 2 |
| 3 — ★ Fill simulator: naive / touch / queue, both cancellation bounds | not started |
| 4 — Quoting: fixed-spread baseline, Avellaneda–Stoikov, inventory skew | not started |
| 5 — ★ Attribution, walk-forward, latency sensitivity | not started |
| 5.5 — Shadow sessions on frozen parameters | not started |
| 6 — Write-up | not started |

293 tests, none of which need a network connection or a credential.

**All market data is behind one paid entitlement.** WebSocket disconnect code
`806` (`"Subscribe to Data APIs to continue"`) and REST error `DH-902` are the two
faces of the same gate — so it blocks the 1-minute historical backfill as well as
the depth feed, not just depth. Dhan's *trading* APIs are free; market data is
not, and its price is not published on the marketing site.

That single fact sets the build order, and it is the reverse of the obvious one:
**everything is built against synthetic fixtures first, then the subscription is
bought.** Both decoders are already written and tested
([`src/bnfmm/data/protocol.py`](src/bnfmm/data/protocol.py)) without a single byte
of real data, because the wire layouts are recoverable from the vendor SDK's own
`struct` formats ([DECISIONS.md #7](DECISIONS.md), [#15](DECISIONS.md)) — so
[`tests/fixtures/packets.py`](tests/fixtures/packets.py) manufactures well-formed
frames and drives the decoders to exhaustion offline. Day 1 of the subscription
dumps 60 seconds of real bytes, asserts the decoders handle them unchanged, and
commits a real golden file *alongside* the synthetic builders rather than replacing
them: a real capture will not produce a malformed frame on demand, and malformed
frames are what an unattended month-long laptop capture will actually meet. The
alternative — subscribing now and spending the paid month debugging a decoder —
costs money to learn nothing.

There is one risk that offline testing creates rather than removes, and it is worth
naming because the fix is the interesting part. The fixtures pack with the *same*
format strings the decoder unpacks, so a wrong format string would produce a
fixture and a decoder that **agree with each other and disagree with NSE** — an
error that would surface on day 1 of a paid month, against a live stream, with no
way to tell a decoder bug from a feed anomaly. So the tests pin every frame size as
a literal and hand-assemble one frame from format strings typed out independently,
both checked against the SDK's own slice bounds rather than derived from the
constants they verify. The suite was then mutation-tested: corrupting the price
rounding, the level format, the header's byte order, the five-level layout, the
level count and the subscription request code each produced failures.

The same check on the capture store was the more useful one, because **two of six
mutations survived** — and both survivals were gaps in the tests, not harmless
changes. Deleting the tie-breaker from the read sort passed, because the obvious
ordering test used distinct timestamps and so had no tie to mis-break; the frames
that actually tie are the ones decoded from a single `recv()`. And reverting the
reader from an explicit file list to directory discovery passed, because Arrow skips
dotfiles unaided — while still reading a stray `part-...parquet.bak` and silently
doubling every row in that partition. Tests that pass on the first attempt are worth
distrusting, which is the whole argument for spending the twenty minutes
([DECISIONS.md #16](DECISIONS.md)).

The paid window is therefore **one month, not a standing cost**: capture and
shadow mode need entitlement, and every re-run afterwards reads saved Parquet.

---

## Method

**Two feeds, not one.** This was the first real finding of Phase 1 and it changed
the capture design. The 20-level depth feed sends the order book and *nothing
else* — no trade prints, no volume, no timestamp — and a queue-position simulator
is built entirely out of "how much traded at this level." So capture runs two
WebSocket connections:

| Feed | Gives |
|---|---|
| 20-level depth | 20 levels per side, with **per-level order count** — the queue-length input |
| General market (`Full` packets) | last traded price and size, cumulative volume, open interest, top 5 levels |

Finding this in Phase 1 rather than Phase 3 is the whole reason the build order
puts the decoder before the subscription: it is a missing dependency that no amount
of simulator design would have worked around, and it cost nothing to discover.
The second feed pays for itself twice over — it carries its own five-level book, so
the two feeds independently report the same top five levels, which is a free
decoder cross-check and an inter-feed latency measurement; and it is the only
packet carrying **open interest**, which an options study needs to distinguish
opening flow from closing.

**One strategy path, three modes.** The third is permanently out of scope.

| Mode | Feed | Clock | Fills | Orders |
|---|---|---|---|---|
| **Replay** | Parquet | `SimClock` (event time) | simulated | none |
| **Shadow** | live WebSocket | `WallClock` | simulated against the live book | **none** |
| ~~Live~~ | — | — | — | **out of scope** |

`sim/engine.py` takes `(EventSource, Clock, FillEngine, Quoter)` and cannot tell
replay from live. These abstractions are written *before* the simulator, because
retro-fitting a live feed into a replay-only backtester is a rewrite. A test
asserts no module under `strategy/` or `sim/` calls `time.time` or
`datetime.now` directly — without that, look-ahead-free replay and live execution
cannot share code.

**Out-of-sample discipline**, so shadow results are provably not fitted: the
loader **refuses** to read holdout dates without an explicit `--unlock-holdout`
flag and logs every unlock, and parameter manifests in `config/frozen/` are
stamped with a git SHA and freeze timestamp — so any shadow session run after that
timestamp is out-of-sample by construction, checkable by a third party.

**Fill models** — three implementations behind one interface, so the comparison
is a table rather than an argument:

1. `NaiveFill` — any trade at or through the quote fills it in full. The strawman,
   implemented deliberately so it can be beaten.
2. `TouchFill` — fills only when a trade strictly crosses the level, capped by
   trade size.
3. `QueueFill` — tracks queue position `Q`. On join, `Q = displayed_qty_at_level`.
   `Q` decrements by trade volume attributed to that level and by *inferred*
   cancellations ahead. Fills when `Q ≤ 0`, capped by remaining trade size.
   "Attributed" rather than "observed" is deliberate: the feed gives total traded
   quantity and the last print's price, so single-print intervals are exact and
   multi-print ones are attributed under a stated rule.

**PnL attribution**, marked to microprice rather than mid. For a fill at time `t`,
price `p`, signed size `q`, fair value `m`:

- spread capture — `q · (m_t − p)`
- adverse selection — `q · (m_{t+τ} − m_t)` for `τ ∈ {1s, 5s, 30s}`
- inventory / directional — mark change on held inventory, fills excluded
- fees — the full statutory stack, plus per-*order* brokerage

The four components plus fees must sum to total PnL. **That identity is a unit
test**, not an aspiration — the cheapest available guard against the attribution
being decorative.

**Fair value** — Stoikov microprice fitted non-parametrically from Tier A, with
weighted mid as the baseline to beat; put–call parity synthetic future
`F = (C − P) + K·e^{−rT}` for cross-checking against the actual futures basis.

---

## Layout

```
config/         instruments.yaml, costs.yaml — no magic numbers in code
                frozen/ — parameter manifests, git-SHA stamped
src/bnfmm/
  data/         auth, instrument resolution, wire protocol (both feeds),
                capture, Parquet writer, backfill, segment QA, holdout lockbox
  book/         snapshots, flow reconstruction, imbalance / OFI features
  fairvalue/    microprice, put-call parity, Black-Scholes delta
  strategy/     fixed-spread baseline, Avellaneda-Stoikov, inventory skew
  sim/          engine, clock, sources/{replay,live}, fills/{naive,touch,queue},
                latency, costs
  analysis/     results (PnlGrid, tiers), report, quote_size,
                PnL attribution, metrics, walk-forward
  synthetic/    Tier C flow generator
scripts/        thin CLIs: refresh_master, check_entitlement, phase0_recon,
                capture, run_sim, shadow_run
tests/          weighted to sim/fills and analysis/pnl, plus structural guards
reports/        findings, figures, data_quality.md, holdout_log.md
```

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Download the exchange instrument master (public, no authentication needed):

```bash
.venv/bin/python scripts/refresh_master.py
```

Reproduce the Phase 0 findings:

```bash
.venv/bin/python scripts/phase0_recon.py --report
```

Run the tests:

```bash
.venv/bin/python -m pytest
```

Tests that need the instrument master skip cleanly without it; nothing in the
suite needs credentials or a network connection.

Everything above runs with no broker account. The one script that needs
credentials is the entitlement probe, which reports which market-data surfaces
this account can reach and writes nothing:

```bash
.venv/bin/python scripts/check_entitlement.py
```

Copy [`.env.example`](.env.example) to `.env` (gitignored) first, or export the
variables directly. With nothing set it explains what is missing and exits without
contacting anything.

Credentials are read from the environment only, never written to disk, and the
access token is redacted from `repr()` — because the realistic leak is a traceback
pasted into a chat window, not an attacker. The TOTP seed is **optional**: leave it
unset and you are prompted for the six digits, so the seed need never exist on the
machine. Reasoning, including why computing a TOTP locally is a defensible
divergence from the brief's "don't automate around the OTP step", is in
[DECISIONS.md #14](DECISIONS.md).
