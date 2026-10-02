# Decisions log

One entry per non-obvious call, with the reasoning and — where there is one —
the evidence. Newest last. The point of this file is that a reader (or an
interviewer) can reconstruct *why* the project looks the way it does, including
the decisions that were wrong and got reversed.

Entries are numbered permanently; a superseded entry is annotated, never edited
away.

---

## 1. Timeline: 8–12 weeks

**Decided at kickoff.** Full scope stands — nothing cut. Tier C synthetic flow
and Avellaneda–Stoikov both stay in.

**Why it was asked:** the core loop (capture → book → queue-aware fill →
attribution) does not compress below about four weeks without gutting the one
part that differentiates the project. Under that deadline the honest advice
would have been to build something smaller.

---

## 2. Data source: Dhan, not Paytm Money

**Reverses the briefing**, which treated `pyPMClient` as the default and listed
its depth-level count as an open question. Resolved by reading both SDKs rather
than their marketing:

| | Paytm Money (`pyPMClient`) | Dhan (`dhanhq`) |
|---|---|---|
| Depth levels | **5** — hardcoded `for i in range(5)` in `WebSocketClient.process_full_packet` | **20** (`wss://depth-api-feed.dhan.co/twentydepth`); 200 for a single instrument |
| Per-level fields | qty, order count, price | qty, **order count**, float64 price |
| On PyPI | **no** — every name variant 404s, git install only | `dhanhq` 2.2.0 |
| Last code push | **Jul 2024**, 13 open issues | Aug 2026, 189 ★, `fulldepth.py` with unit tests |
| Auth | OTP → manual daily token | 24 h token, **TOTP-mintable** |
| Historical | — | 1-min intraday, 5 y; daily to inception |

Five levels to twenty is not a nice-to-have. Queue-position modelling needs the
depth profile behind the touch, and **per-level order count** is what makes
average-order-size and queue-length inference possible at all.

**Side effect:** the briefing's constraint "assume a human supplies fresh tokens
each morning" dissolves. `POST auth.dhan.co/app/generateAccessToken` takes
`(dhanClientId, pin, totp)`, so a scheduled job mints its own. Static-IP
registration applies only to order placement, which this project never touches.
Credentials still come from the environment and are never committed.

*"Dissolves" is too quick — it is a divergence from an explicitly stated
constraint and deserves an argument rather than a clause. Made properly in
entry 14, which also records what is **not** relaxed.*

---

## 3. Framing: an execution-realism study, not a strategy

**Decided at kickoff.** The README leads with the naive-vs-queue-aware fill gap
as a *methodology* finding. Strategy PnL is a supporting exhibit, never the
headline.

**Why:** a student project claiming profitable market making is not believable
and invites exactly the scepticism the briefing's §3 worries about. Inverting it
costs nothing — same code — and converts the deliverable into a measurement that
survives an adversarial reading. See entry 9: the cost analysis later made this
reframe load-bearing rather than merely tactful.

---

## 4. Instrument: flexible on Phase 0 evidence

**Decided at kickoff.** BANKNIFTY futures was the working default, with a Phase 0
decision gate empowered to move it. Resolved in entry 9.

---

## 5. Every contract fact is resolved at runtime, never hardcoded

`config/instruments.yaml` *constrains* the universe (which underlying, how many
strikes, roll rule); `src/bnfmm/data/instruments.py` *resolves* it from the
exchange instrument master at run time.

**Why:** NSE has repeatedly changed BANKNIFTY's expiry day, weekly availability,
lot size and tick size. A constant for any of those is a bug with a delayed
fuse — it keeps working until the day it silently resolves the wrong contract.

Found immediately, and each one would have been wrong as a literal:

- **BANKNIFTY has no weekly options.** `EXPIRY_FLAG` is M for 2,361 contracts
  and W for none. The briefing's concern that weeklies and monthlies "may list
  simultaneously" is moot.
- **Expiry day is Tuesday**, not the Thursday most references still state —
  2026-08-25, 09-29, 10-27, 12-29, 2027-03-30, 06-29 are all Tuesdays.
- **Lot size 30. Futures tick Rs 0.20** — the coarsest of the index futures
  (NIFTY and FINNIFTY are Rs 0.10, MIDCPNIFTY Rs 0.05). **Options tick Rs 0.05.**
- **Freeze quantity 601 units**, a hard exchange ceiling on single-order size and
  therefore on quote size.

Pinned in `tests/test_master_facts.py`. A failure there is not a bug — it means
NSE changed something and the affected decision needs re-deriving.

**Bonus:** the master is public and needs no authentication
(`images.dhan.co/api-data/api-scrip-master-detailed.csv`), so all of this was
settled before any broker account existed.

---

## 6. `TICK_SIZE` in the instrument master is denominated in paise

Not documented anywhere; established by cross-check. The master reports 5.0 for
NIFTY index options, and NSE publishes their tick as Rs 0.05. Rupees would imply
an absurd Rs 5 tick on options that trade near Rs 1.

**Why it matters enough for its own entry:** tick size is the denominator of the
entire viability calculation in entry 9. A 100× error there inverts the
project's central conclusion.

---

## 7. A custom depth client, rather than wrapping `dhanhq.fulldepth.FullDepth`

The SDK's binary protocol work is reused — the exact `struct` formats, message
codes and disconnect codes are read out of `fulldepth.py` — but the class itself
is not usable for capture:

- `get_data()` **prints every book to stdout**, which at 20 levels × N
  instruments is unusable and unbuffered.
- `subscribe_symbols` / `unsubscribe_symbols` call `self.ws.closed`, an attribute
  **removed in `websockets` 17.0.1** (the installed version). Already broken.
- `utc_time` uses `datetime.utcfromtimestamp`, deprecated.

Protocol facts extracted and worth recording, since they constrain the capture
design:

- Header `<hBBiI` (12 bytes): msg_length, msg_code, exchange_segment,
  security_id, sequence. **There is no exchange timestamp in the depth packet** —
  so capture must stamp arrival time locally, and latency analysis must treat
  that stamp as receive-side, not exchange-side.

  *The last field is not a sequence number. `fulldepth.py` never reads it on
  codes 41/51, reads it as `no_of_rows` on the 200-level feed, and as the reason
  code on code 50 — so on a 20-level frame its meaning is simply unknown, and
  calling it "sequence" here was a guess dressed as a fact. `protocol.py` exposes
  it as `header_extra`. Corrected in entry 15, which also records what the
  correction costs the QA plan.*
- Body `<dII` × 20: float64 price, uint32 quantity, uint32 order count.
- msg_code 41 = bid side, 51 = ask side — **separate packets**, paired by
  `security_id`. 50 = disconnect.
- 20 slots are always sent, so padding entries (price 0, qty 0) must be filtered.
- `combine_and_format_depth` sorts bids descending and asks ascending, implying
  the wire order is not guaranteed price-sorted.
- Disconnect code **806 = "Subscribe to Data APIs to continue"** — proof that
  depth data is a paid entitlement, not included by default.

---

## 8. Cost model is config-driven and tested against hand-computed values

`config/costs.yaml` carries the full statutory stack — STT, exchange transaction
charge, SEBI turnover fee, stamp duty, NSE IPFT, and GST on the correct subset —
with a `retrieved` date and a source URL.

**Why not hardcode it in Python:** these rates change by government budget and
SEBI circular, and futures STT in particular has been revised repeatedly
(0.01% → 0.0125% → 0.02% → 0.05%). Entry 9 shows the whole project's conclusion
turns on them, so they need a visible provenance date and a single place to
update. **Any published number must be re-verified against the current circular
first.**

GST applies to brokerage, exchange transaction charge, SEBI fee and IPFT — but
**not** to STT or stamp duty. Getting that subset wrong overstates costs by
~Rs 156 per futures lot, which is 26 ticks.

---

## 9. Quoted instrument: BANKNIFTY **options**, not futures

**Supersedes the working default in entry 4, and diverges from the briefing.**
Fires the Phase 0 decision gate on evidence, as entry 4 authorised.

Reproduce with `python scripts/phase0_recon.py`; full write-up in
[reports/phase0_instrument_selection.md](reports/phase0_instrument_selection.md).

**Finding 1 — futures market making is structurally unviable at these rates.**
One futures tick is Rs 0.20, i.e. Rs 6.00 per lot of 30. One round trip costs
**Rs 980 per lot**, of which **Rs 867 is STT alone**, because futures STT is
levied on contract *notional* (Rs 1.73 crore per lot). Breakeven is therefore
**163 ticks — Rs 32.68 of captured spread per round trip.** BANKNIFTY futures do
not quote a Rs 33 spread in any normal condition. This is a property of the fee
schedule, not of the strategy, and no amount of quoting sophistication closes it.

**Finding 2 — options are the defensible instrument, and are still marginal.**
Options STT is charged on *premium*. At a Rs 1,000 premium the base is Rs 30,000
rather than Rs 1.73 crore: round trip **Rs 71 per lot**, breakeven **47 ticks =
Rs 2.37**. That is 14× more favourable than futures and the same order of
magnitude as a plausible ATM spread — but only that. It sits close to the cost
boundary, not comfortably above it.

The invariant behind both: round-trip cost is a fixed **0.0566% of futures
notional** and a fixed **0.2371% of options premium**. Breakeven in ticks scales
with price level; that percentage does not.

**Finding 3 — no futures hedging leg.** Neutralising one ATM option lot's delta
takes half a futures lot, costing **Rs 490 — 6.9× the entire round-trip cost of
the option position being hedged**, or 327 option ticks. The same notional-based
STT that kills futures market making taxes every futures hedge. So: quote the CE
*and* the PE at the same strike, so arriving inventory partially offsets in
delta, and manage the residual by **skewing quotes** — which is what the
Avellaneda–Stoikov reservation-price shift already does, generalised from a
directional inventory to a delta inventory.

**Net scope effect: roughly neutral.** One small module added
(`fairvalue/greeks.py` — a Black–Scholes delta, testable against published
values). No vol surface, no gamma or vega hedging, and no hedging execution
layer. Futures stay in the project as the reference leg for the put–call parity
synthetic future, and Finding 1 is itself a reportable result.

**Why this strengthens the thesis rather than damaging it.** Entry 3 reframed the
deliverable as an execution-realism measurement. Finding 2 makes that reframe
load-bearing: the margin is thin enough that **the fill assumption alone can
determine the sign of the PnL.** A naive instant-fill backtest assuming full
spread capture will report a profit; a queue-aware simulator on the same data and
the same strategy may not. That is a far stronger claim than "queue-aware fills
reduce PnL by some percentage" — it makes the methodology choice
decision-relevant rather than merely quantitative.

**Caveat, stated plainly:** every spread figure above is a *hypothesis*. The
comparison that settles viability needs measured top-of-book spread over real
sessions, which is the depth-feed half of Phase 0. Nothing here is a market-data
result — it is arithmetic on a published fee schedule plus contract specs.

---

## 10. Spot bootstrap: restricted to the nearest expiry

`estimate_spot` infers the underlying level from deep-ITM option circuit bands
(`mid(band) ≈ |spot − strike|`), so Phase 0 can locate the ATM strike before any
broker account exists.

**The bug worth recording:** the first version pooled contracts across all
expiries and returned **58,130** where a hand-check of two deep-ITM bands gave
**57,785** — 345 points high, about 3.5 strike steps, enough to pick the wrong
ATM. Two causes:

1. Each expiry's option prices reference *that expiry's forward*, not spot, so
   pooling mixes forwards and adds carry.
2. Long-dated bands carry thousands of points of time value — the 2027-06 81000
   PE implied 61,494.

Fixed by restricting to the nearest expiry inside the function, so a future
caller cannot reintroduce it. Restricted that way, calls and puts — computed from
opposite ends of the chain with no shared inputs — agree to **5.3 points on a
57,786 index (0.01%)**, well inside the 100-point strike step. That agreement is
now reported by `SpotEstimate.disagreement` and asserted on real data, because an
estimator that can check itself should.

---

## 11. `_expiry` accessed by bracket, not attribute

A DataFrame column with a leading underscore collides with pandas' internal
attribute namespace. `df._expiry` happens to work today and is one pandas
release away from resolving to something else entirely. Bracket access
throughout.

---

## 12. Brokerage correction: Dhan charges a *flat* ₹20 on all F&O

The first `config/costs.yaml` encoded Dhan's brokerage as `min(₹20, 0.03% of
turnover)` for both products. That is wrong, and it was wrong in the direction
that flatters the project.

Dhan's published schedule (retrieved 2026-08-23) is **₹20 per executed order for
equity & commodity F&O, flat**. The "whichever is lower" rule is real but it
applies to *equity delivery/intraday and MTF*, not to derivatives. Encoding a cap
the broker does not apply understates cost.

**What it cost:** on a ₹1,000 premium the model charged ₹9 per options order
instead of ₹20 — ₹11 per order, ₹26 per round trip once GST is counted, on a
statutory bill of ₹71. Options breakeven was understated at **₹3.08 (61.6 ticks)
against a true ₹3.94 (78.9 ticks)** — a 22% understatement of the hurdle rate on
a margin already close to the cost boundary. Futures were unaffected in practice:
0.03% of ₹1.73 crore is ₹520, so the flat fee always bound.

**The fix is structural, not a number change.** A single `retail_pct_cap` scalar
assumed every broker and every product share one rule. Replaced with named
profiles — `member` (₹0), `dhan` (flat ₹20 both products), `zerodha` (comparison
only: futures `min(₹20, 0.03%)`, options flat ₹20) — reached through
`CostModel.for_profile(name)`. The percentage branch is kept and tested via the
`zerodha` profile rather than deleted, because the rule exists somewhere and a
dead branch is how it silently comes back.

`analysis.quote_size.pct_cap_breakpoint` reports where such a cap stops binding:
₹66,667 of turnover on Zerodha futures, well under one BANKNIFTY lot. That is why
the bug hid — it only ever changed the options number.

**Two further corrections followed from getting this right:**

1. **Brokerage is per executed order, not per fill.** A resting five-lot quote
   that fills in five one-lot pieces is one order and pays ₹20 once. Charging per
   fill would have inflated it to ₹100 — 32 ticks on a hurdle of 79. Handled by
   `OrderCost`, which accumulates statutory charges per fill and *recomputes*
   brokerage from the running executed value. Recomputed rather than accumulated
   because a percentage cap depends on the order's cumulative value, so the
   charge is not knowable from any single fill.
2. **Quote size is now a decision variable.** Statutory charges scale with
   quantity, brokerage does not, so per-unit breakeven falls from ₹3.94 at one
   lot to ₹2.45 at the 20-lot freeze cap — a 38% cut in the hurdle rate from size
   alone. Avellaneda–Stoikov derives an optimal *spread* and assumes no fixed
   per-trade cost, so it has nothing to say about this. `analysis/quote_size.py`
   computes the cost half; the risk half needs the fill simulator.

Everything statutory was independently correct and unchanged. Recorded because
the failure mode is the interesting part: the rate table was verified against the
source, and the *applicability* of one rate was not.

---

## 13. Every figure is a 2×2, and there is no headline cost number

User decision, overriding a recommendation to lead with the retail case: report
**both brokerage regimes, paired, everywhere**. Combined with the two
cancellation conventions the fill simulator already had to bound, every PnL
figure is a four-corner grid — brokerage regime × queue convention.

|  | pessimistic queue | proportional queue |
|---|---|---|
| **member** (₹0 brokerage) | … | … |
| **retail** (Dhan ₹20/order) | … | … |

**Why this is stronger than a point estimate.** The member regime is not a
scenario this project could trade; it is the most favourable cost assumption that
could plausibly be modelled, so it functions as a bound. The pair therefore
brackets the answer instead of asserting it. And when the corners disagree about
the *sign*, naming the corner where it flips is the finding: "profitable only in
the member/proportional corner" is a far more defensible sentence than any single
number, and it is the outcome Finding 2 suggests is likely.

**Enforced, not conventional.** `analysis/results.py` defines `PnlGrid` with all
four corners required at construction — a three-corner grid raises `TypeError`,
and `tests/test_results.py` asserts the fields never acquire defaults, because a
default of `0.0` would silently report zero for an unmeasured regime. Non-finite
corners and empty labels are rejected too. There is no `float` PnL type
downstream; a single number is unrepresentable without reaching past the type.

The same object carries a required `tier` field (`A_REAL_L2` / `B_REAL_1MIN` /
`C_SYNTHETIC`), and `analysis.report.headline()` **raises** for any tier outside
`HEADLINE_TIERS = {A}`. Tier C is synthetic; Tier B is real but 1-minute bars
contain no queue at all, so a queue-position claim built on them would be fiction
with real provenance — arguably the worse of the two failures. This is the
mechanical form of `BRIEFING.md` §2's non-negotiable labelling constraint: a rule
that depends on remembering to follow it will not survive to the writeup.

---

## 14. TOTP is computed locally — a deliberate divergence from the brief

`BRIEFING.md` §2 says token refresh "should not be automated around the OTP step —
assume a human supplies fresh tokens each morning." That was written about Paytm
Money, and it is the right instinct for that broker: the OTP arrives out of band,
so automating it means intercepting a message.

Dhan's second factor is different in kind. It is a **TOTP** — six digits derived
from a shared base32 secret the user already holds, by the same arithmetic an
authenticator app performs. Computing it locally involves no interception, no
channel, and no third party. `DhanLogin.generate_token(pin, totp)` is the SDK's
documented login path and expects exactly that.

So the letter of the constraint is relaxed and **the intent is kept where it
matters**:

- The secret is **human-supplied via the environment**, never committed, never
  written to disk by this code. `.env` is gitignored; `.env.example` is blank and
  a test asserts it stays blank.
- **The seed is optional.** With `BNFMM_TOTP_SECRET` unset, the six digits are
  prompted for — so the base32 seed need never exist on the machine at all. That
  path is the exact behaviour the brief asked for, and it is one env var away.
- `BNFMM_DHAN_ACCESS_TOKEN` accepts a token minted anywhere else, so a PIN need
  never reach this process either.
- `Session.__repr__` redacts the token, because the realistic leak is not an
  attacker but a traceback pasted into a chat window. Tested.

**What is *not* relaxed:** `auth.py` does not touch `set_ip` or `modify_ip`.
Static-IP registration is a prerequisite for order placement and nothing else, so
wiring it would be the first step toward a capability this project has none of.
`tests/test_no_order_placement.py` fails if either name is referenced in `src/`.

That guard also drove a design change worth recording. Its first version was a
text grep, which flagged `auth.py`'s docstring for *explaining* that it avoids
`set_ip`. A guard that fires on correct code gets relaxed, and a relaxed guard
guards nothing — so it was rewritten over the AST: **prose may name these things,
code may not.** Docstrings are identified structurally and exempted; identifiers,
attribute accesses, `getattr` string literals and raw route strings are not. The
detectors are themselves tested against a synthetic offending module and a
synthetic innocent one, so the guard cannot quietly stop matching.

---

## 15. Capture needs **two** WebSockets, and there is no sequence number

Writing `src/bnfmm/data/protocol.py` meant reading both SDK decoders line by line
rather than skimming for `struct` formats. Five findings, in descending order of
how much they change the plan.

### 15.1 The depth feed carries no trades — so it cannot feed the fill simulator

The 20-level feed (`depth-api-feed.dhan.co/twentydepth`) sends the book and
nothing else: no trade prints, no volume, no timestamp. The queue-aware fill
simulator — `BRIEFING.md` §1's "single highest-value, most differentiating piece"
— needs traded quantity at a price level to decide whether a resting order would
have been reached. **That number is not on the depth feed at all.**

So capture runs two connections, not one:

| Feed | Endpoint | Gives |
|---|---|---|
| 20-level depth | `depth-api-feed.dhan.co/twentydepth` | 20 levels/side, per-level order count |
| General market | `api-feed.dhan.co` (`Full`, request code 21) | LTP, last-traded qty, cumulative volume, OI, top 5 levels |

This is a **missing dependency in the approved plan**, not scope creep — Phase 3
would have hit a wall that no amount of simulator design could get around. Found
in Phase 1, before the subscription month is bought, which is the cheapest
possible place to find it.

Two side benefits fall out of the second connection. `Full` carries its own
five-level book, so the two feeds independently report the same top five levels:
a free decoder cross-check and an inter-feed latency measurement, tested in
`test_the_five_level_block_is_a_cross_check_on_the_twenty_level_feed`. And `Full`
is the only packet with **open interest** — `Quote` (50 bytes) omits it — which an
options study needs to tell opening flow from closing.

### 15.2 There is no sequence number (correcting entry 7)

Entry 7 recorded the depth header's trailing `uint32` as `sequence`. It is not.
The SDK reads that offset as `no_of_rows` on the 200-level feed and as the reason
code on a disconnect frame, and **never reads it at all** on codes 41/51. Its
meaning on a 20-level frame is unknown, so `protocol.py` names it `header_extra`
and refuses to interpret it.

This costs a QA check. The approved plan's per-segment list includes
"non-monotonic sequence" — **not implementable**, because there is no sequence to
be monotonic in. Nor is gap detection: a dropped depth frame is undetectable in
principle from the depth feed alone.

The replacement is the general feed's `last_trade_epoch`. It is the only
exchange-side clock anywhere in the system, and comparing it against local arrival
time bounds clock drift and suspend/resume damage — the hazard entry 4 of the plan
was worried about. Two honest limits: **one-second resolution**, so it bounds drift
but cannot measure latency; and **it only advances when a trade happens**, so in a
quiet strike it is static and the check has no power. Segments are therefore
flagged on drift *when the clock moves*, and the fraction of a segment where it
does not move is itself reported in `reports/data_quality.md`.

### 15.3 Traded quantity is a difference; its price distribution is a second unobservable

`volume` is **cumulative for the day**, so traded quantity over an interval is a
difference between consecutive snapshots. `last_quantity` is only the most recent
print and undercounts whenever two trades land between packets.

That gap is a second unobservable alongside the cancellation ambiguity of entry
7's `Δqty` identity: the *total* traded quantity over an interval is observable,
its *distribution across price levels* is not. But the ambiguity is **detectable
per interval, not merely feared**:

- `volume_diff == last_quantity` → exactly one print, and `ltp` gives its price.
  The interval is fully identified.
- `volume_diff > last_quantity` → more than one print, and only the last one's
  price is known.

So the fraction of intervals of the second kind measures how often the ambiguity
actually bites, and that fraction gets reported rather than assumed away. Whether
it collapses into the existing pessimistic/proportional bound or needs an axis of
its own is a Phase 3 design question, deliberately left open here — it depends on
a number (that fraction) that does not exist until Tier A data does.

### 15.4 Both SDK decoders lose data on realistic buffers

Worked around rather than inherited, with a test named for each:

1. `MarketFeed.process_data` reads `data[0:1]`, decodes **one** packet, and
   returns — every further packet in the same `recv()` is discarded. At
   option-chain subscription counts that is most of the feed.
2. `FullDepth.process_20_depth_data` returns `None` on an unrecognised code, which
   ends its caller's loop and **drops the rest of the buffer** — including every
   frame after a disconnect frame.
3. Neither carries an incomplete trailing frame. A 332-byte frame does not fit a
   socket read boundary, so this is the normal case, not an edge case.

`protocol.py` instead frames by a known size-per-code table, returns an explicit
remainder, and skips unknown codes with a counter. It raises `ProtocolError` only
when an unknown code's declared length is unusable — neither feed has a magic
marker or checksum, so a misaligned stream cannot be resynchronised and guessing
is worse than starting a new segment.

The header's own `msg_length` is **counted, not obeyed**: the SDK never reads it,
so nothing confirms the server fills it correctly, and a decoder that trusts an
unverified field inherits a bug the moment it is wrong.

### 15.5 float32 prices are snapped to paise; the token rides in the URL

General-feed prices are float32. `57785.05` arrives as `57785.05078125` — an error
of 7.8e-4, negligible against a ₹0.05 tick and **fatal to `ltp == best_bid`**,
which is the comparison the entire fill simulator is built out of. NSE prices are
paise-denominated, so rounding to **2** decimals *recovers* the intended value.
4 decimals does not: it leaves `57785.0508`. Depth-feed prices are float64 and are
left exactly as sent. Tested with an assertion that the correction is always
smaller than one tick — otherwise it is not a de-quantisation, it is a price
change.

Separately: both feeds take the access token as a **query parameter** on the
connect URL, so the URL is itself a credential (`FullDepth.connect` prints it).
`redact_url` exists for logging and `feed_url` carries a "never log the return
value" warning, consistent with entry 14.

### Why this is tested this hard before any real bytes exist

`tests/test_protocol.py` is 73 tests over a module that cannot be run against the
real feed until the subscription is bought. The risk that makes it worth it: the
fixtures pack with the *same* format strings the decoder unpacks, so a wrong
format string would produce a fixture and a decoder that **agree with each other
and disagree with NSE** — and the error would surface on day 1 of a paid month,
against a live stream, with no way to tell a decoder bug from a feed anomaly.

Two things guard against that. One frame is hand-assembled from format strings
typed out independently in the test file, and every frame size is asserted as a
literal (332 / 162 / 112 / 50 / 16 / 12 / 10 / 8). Both were confirmed against the
SDK's own slice bounds rather than derived from the constants they check. The
suite was then mutation-checked: flipping `PRICE_DP` to 4, `<dII` to `<dHH`,
`<hBBiI` to big-endian, the five-level format to float64, `DEPTH_LEVELS` to 5, and
the capture request code from `Full` to `Quote` each produced failures.

Synthetic fixtures stay after the golden file arrives. A real capture is unlikely
to contain an interleaved padding row or a lying length header on demand, and
those are exactly the cases an unattended month-long laptop capture will meet.

---

## 16. Capture storage: one row per frame, two clocks, and a rename per flush

`src/bnfmm/data/store.py`. Four choices, each driven by a specific way an
unattended month-long laptop capture fails rather than by convention.

### 16.1 One row per frame, never one row per book

Bids and asks arrive as **separate frames** (entry 7) and the depth feed carries no
identifier tying a bid frame to its ask. Pairing them is therefore a
*reconstruction* decision with a rule behind it — nearest preceding partner, within
some tolerance — and rules like that belong in `bnfmm.book` where they can be
varied and tested, not baked into the file format.

The argument that settles it: a schema of paired books has no way to represent a
frame whose partner never arrived, so the writer would have to either drop it or
invent a partner. Both destroy the same thing — **an unpaired frame is evidence of
a drop, and drops are what a laptop capture is for measuring.** Storing frames as
they arrived keeps that evidence; a paired schema would silently launder it into
plausible-looking books.

### 16.2 `recv_seq`, because timestamps tie

Every frame decoded from one `recv()` shares an arrival timestamp, because that is
what was actually measured — sub-chunk times would be fabricated. But a sort on a
tied key permutes the tied rows arbitrarily, so book reconstruction would depend on
Arrow's sort implementation, which is a reproducibility bug that appears as an
occasional changed number rather than as an error.

`recv_seq` is the recorded arrival order, and `read_capture` sorts
`(recv_wall_ns, recv_seq)`. Worth stating what nearly hid this: the obvious test —
write frames interleaved across two instruments, read them back in order — **passes
even with `recv_seq` removed from the sort key**, because with distinct timestamps
there is no tie to mis-break. It only bites when the tie is real *and* the file
order differs from the arrival order, which is why
`test_arrival_order_survives_a_timestamp_tie_across_partitions` constructs exactly
that and asserts the discrepancy exists before asserting it is fixed.

`recv_seq` also counts frames that are *not* stored — disconnects, unknown codes —
so a gap in the sequence has the reason for the gap sitting in the manifest at that
index. Numbering only stored rows would hide that a gap was explained.

### 16.3 Both clocks on every row

The depth feed has no exchange timestamp, so local arrival time is the only clock,
and **a real 30-second dropout and a 30-second NTP step are the same bytes in wall
clock alone.** `time.monotonic` is immune to both NTP and suspend, so the two
recorded together separate the cases: deltas that agree mean a genuine gap (a
segment boundary), deltas that disagree mean clock damage (excluded from latency
analysis, per the plan's clock-discipline requirement).

`ClockWitness` accumulates the divergence in the writer, which is the only
component that sees every row in arrival order — measuring it later means a second
pass over a month of Parquet. It reports the worst single step *and the row it
happened at*, so one damaged segment is excluded instead of a whole session, plus
end-to-end span disagreement, which catches slow drift that no single step reveals.
Suspend-and-resume is the case that produces a 45-minute wall span against a
sub-second monotonic one.

### 16.4 A rename per flush, and temp files nothing will read

The process will be SIGKILLed at some point in a month. Each flush writes to a temp
path and `os.replace`s it into position, so a kill costs the current buffer and
never leaves a truncated Parquet in the dataset.

The subtle part is the temp *name*. `part-x-00000.parquet.tmp` was the first
version and it is wrong: a **fully written but unrenamed** temp file parses cleanly,
so a reader globbing `*.parquet` would silently double every row in that partition.
A wrong number is worse than a crash. Temp files are therefore `.<name>.tmp` — the
leading dot is the convention Arrow, Spark and Hive all treat as ignorable, and the
suffix is not `.parquet`.

`read_capture` additionally builds an **explicit file list** instead of handing
Arrow the directory. Mutation-testing showed these are two independent defences,
not one belt-and-braces pair: Arrow skips dotfiles on its own, so reverting to
directory discovery still passed the `.tmp` test — but directory discovery reads
*anything else* in the partition, and a `part-...parquet.bak` left by an operator
inspecting an old capture silently doubles the rows while a stray `notes.txt` makes
the whole read raise. Both are now tested.

### What the 44 tests are for

Round-trip identity is the one property a storage layer must have and the one a
schema review cannot confirm, so every frame variant is written and read back and
compared field for field — including a frame with an interleaved padding row and a
priced-but-empty level, because **anomaly flags that get recomputed on read would
be re-derived from already-filtered levels and always report clean**.

The suite was mutation-checked like `test_protocol.py`: dropping `recv_seq` from
the sort key, naming temp files `*.parquet.tmp`, computing the trading date in UTC
instead of IST, zero-filling `open_interest`, numbering only stored rows, and
reading the directory instead of the file list. Two of those six survived the first
draft of the tests. Both survivals were test gaps rather than harmless mutations,
and fixing them is what 16.2 and 16.4 record.

## 17. Two simulator bugs that each manufactured profit, and how they were caught

Both were found by disbelieving a good result, not by a failing test. Recording
them because the *method* — treat every favourable number as a bug report until it
survives an attack — is the transferable part.

### 17.1 The sweep rule filled us for free

The depletion model lets an order fill without its queue clearing if the level
*trades through*: a sweep takes every resting order at the price. The first
implementation tested only whether the touch had moved past our price:

    if order.side == "bid": return best_bid < order.price

That condition is **permanently true for any quote resting inside the spread**.
The quoter improves the book 79.5% of the time on the 1 DTE at-the-money call —
its median spread is 11 ticks against our 2-tick half-spread — so a single
eligible print filled the entire order, bypassing `queue_ahead` completely.

The tell was not the PnL. It was the **effective half-spread of −0.0493**: a
market maker cannot be paid a negative spread on its own quotes, so the number was
structurally impossible and the simulator was paying itself. 15,218 of 18,012
fills (84%) were sweeps.

The repair requires three conditions jointly — the queue ahead must have cleared,
the interval must contain actual prints (a level that vanished with no prints was
*cancelled*, not executed), and the touch must have moved through. Sweeps fell to
1,925.

### 17.2 Huang-Stoll marked fills after the move they caused

With 17.1 fixed the effective half-spread was still −0.0489. The second cause was
that fills were marked at `mark[i]` — the book at the *end* of the interval the
fill happened in. That move is partly the market impact of the very flow that
filled us, so charging it to the effective spread makes the term that measures
what we were *paid* absorb the adverse selection instead. Huang-Stoll's `M_t` is
the **prevailing** midpoint; Lee-Ready lag theirs for the same reason. Marking at
`mark[i-1]` moves the move into the realised-spread term, where it belongs.

**The validation that both fixes are right** is not that the sign flipped. It is
that the effective half-spread then tracks the quoted half-spread almost exactly
across a sweep — 2 ticks → +0.1072, 4 → +0.2068, 6 → +0.3065, 8 → +0.4062,
12 → +0.6060, 16 → +0.8063, 24 → +1.2072 — which is what it must do by
construction, and did not before.

## 18. The parity anchor's edge is real, but it is not market making

The parity-anchored quote (value = other leg + cross-strike consensus forward)
produced the only positive net result found: member net **+₹92,433** on
2026-08-24, against **−₹134,213** for the microprice anchor. A 7.6× jump in gross
demanded an attack before it could be reported.

### 18.1 Self-reference was ruled out, analytically and by measurement

The consensus forward is built from all 23 strikes *including the pair being
quoted*, so the leg's own book feeds the value that prices it. The algebra says
this is conservative rather than circular:

    adj = fair_value − micro = D·(F − F_K) = D·(1 − w_K)·(F_rest − F_K)

Including yourself scales the adjustment by `(1 − w_K) < 1` — it can only shrink
the signal toward the option's own microprice. But the near-the-money pair carries
the ladder's largest precision weight, so `w_K` is not small and the algebra alone
is not enough. `--exclude-self` rebuilds the forward from the other 22 strikes:
member net **+₹93,112**, versus +₹92,433 with self included. Unchanged, and
marginally better, exactly as predicted. **Not circularity.**

### 18.2 It is a directional bet, which the PnL decomposition exposes

Gross PnL telescopes exactly into two terms with completely different meanings:

    gross = Σ sᵢ(Mᵢ − Pᵢ)  +  Σ sᵢ(M_T − Mᵢ)
          = spread capture  +  inventory PnL

On the winning run: spread capture **₹74,017**, inventory PnL **₹101,897** — 58%
of gross, and 71% on the call leg alone. Spread capture of ₹74,017 against member
costs of ₹83,481 means **the quoting itself loses money**; every rupee of profit
came from inventory. The quoter sat pinned at maximum short calls (45% of the day)
and maximum long puts (40%) — a synthetic short forward — on a day the market fell.

Three independent confirmations:

* **Skew sweep.** Squeezing the directional exposure out destroys the profit:
  skew 0.5 → +₹92,433, skew 2.0 → −₹4,664, skew 6.0 → −₹54,582. Spread capture
  never covers costs at any setting.
* **Day two.** The same configuration loses **−₹207,712** on 2026-08-25, when the
  bet went the other way.
* **All four day × anchor cells.** Inventory PnL dominates gross in every one.

This is why the decomposition is now computed for every run and tested with an
exact-identity check (`tests/test_pnl_decomposition.py`, 9 tests including a
brute-force cross-check). Reporting gross PnL alone would have called this a
working market maker.

## 19. A NaN closing mark silently valued open positions at zero

`Position.unrealised` returns 0.0 for a non-finite mark. The final snapshots of an
expiry-day book are routinely one-sided as liquidity leaves into the 15:30 close,
which makes the microprice NaN — so on 2026-08-25 the 57500 PE ended **long 150
units marked at NaN and booked at zero PnL**, and its decomposition silently
dropped out of the report entirely. That is how the bug was noticed: a contract
missing from an output table, not a wrong number.

`run_day` now falls back to the last *finite* microprice and records
`final_mark_stale_ns` so the staleness is disclosed rather than buried (that PE
marks at ₹0.05, 210.8 s stale). The fallback is deliberately a price the book
actually showed, not an extrapolation and not a settlement value — the capture
does not include settlement prices, and inventing one would be exactly the kind of
synthetic number `BRIEFING.md` §2 forbids passing off as real.

## 20. Stale-packet replays were booked as trades; volume is now differenced on its running maximum

*2026-09-30.* The data-integrity pass (`reports/data_integrity.md`, `src/bnfmm/data/qa.py`)
found that the quote feed occasionally replays a stale packet: its cumulative volume **and** its
`last_trade_epoch` are older than a packet already received. `book/tape.py` dropped the negative
step and then counted the climb back to the true level as new volume — volume booked twice.
Phantom share of option volume was ≤0.01% on nine days and **5.8% on 2026-09-04**. Phantom
trades become phantom fills, in the flattering direction.

Traded quantity is now the increment of the counter's running maximum
(`tape.volume_increments`, `tests/test_tape.py`). Effect on the Phase 1 result: ≤0.01% of volume,
not material. Any tape computed before this date should not be reused.

The same pass established three things the simulator must respect, recorded here because each
could otherwise be "rediscovered" as a bug: the 20-level depth feed arrives a stable ~225 ms after
the quote feed (a vendor property — agreement peaks at that offset on every day, hour and
instrument); `last_trade_epoch` is IST wall-clock seconds, not UTC; and 2026-09-04 carries a
two-hour burst of unchanged packets every ~33 ms, which is harmless.

## 21. The market maker became one portfolio with one delta, orders that keep their queue place, and a cost gate in basis points

*2026-09-30.* Phase 1's quoter could not be tuned into profit (§17–19 of BRIEFING). Four
structural changes, each motivated by a measurement, produced `sim/portfolio.py`:

1. **Portfolio risk, not per-leg inventory.** Black-76 delta and vega, implied from each option's
   own fair value, are used *only* to measure exposure (never to price — #9's objection to a vol
   model stands). Quotes skew against portfolio delta; |delta| ≤ 5 lots and |vega| ≤ 3 ATM-lots
   are hard limits.
2. **Queue priority.** `run_day` cancelled and re-placed every quote every 200 ms, so it could only
   ever be filled by an interval that cleared a whole level. Unchanged quotes now rest.
3. **Symmetric round-trip cost split.** Per-side gating put asks further from fair value than bids
   (sell STT 19.2 bp vs buy 4.5 bp); the book bought more than it sold and drifted to 150 ATM-lots
   long vega, which showed up as β −4.3 lots, R² 0.28 on the forward.
4. **A spread gate in basis points of premium.** Cost and adverse selection both scale with premium;
   the spread in ticks does not. Minimum book spread = round-trip cost 23.7 bp + 2 × measured
   300 s adverse selection 7.05 bp = **37.8 bp**. *Derived*, then checked for robustness (30 and
   45 bp also profitable), not swept for the best value.

Rejected on develop data and recorded so they are not re-tried as new: the causally merged
fresher book, the 5-level imbalance pull (failed the realised-vs-effective falsification test),
vega skew, and join-only placement. The L1 microprice was also found no better than the plain mid
as a forecast of future prices.

## 22. mm_v1 was frozen, then run once on the holdout, and made money on every day

*2026-09-30.* After 26 configurations on develop days, the strategy was frozen in
`config/frozen/mm_v1.yaml` (sha256 `4f144314…`) and run once on the five holdout sessions
(2026-08-31 → 09-04). Result, member profile: **+₹199,537 net, +₹155,159 liquidated at the close,
5/5 days positive**; spread capture ₹735,694 against an inventory term of −₹222,432; R² of
per-minute PnL on the forward 0.002–0.18. Per-fill 95% CI (5-minute block bootstrap)
[₹6.34, ₹20.40]. Dhan retail at one lot: −₹121,494. Out-of-sample degradation about a third
(+₹63.8k/day develop → +₹39.9k/day holdout).

The near-expiry sessions (2026-08-24/25) were excluded by `min_days_to_expiry: 3`, written into
`config/instruments.yaml` on 2026-08-23 before any data existed — the realised spread there is
zero. Full account and limitations: `reports/phase2_portfolio_market_maker.md`. The holdout is
spent; any change is mm_v2 and has no clean out-of-sample data left in this corpus.

## 23. The mm_v1 profit does not survive realistic order latency

*2026-09-30.* Three robustness checks on the *frozen* strategy, none of which changes a strategy
parameter:

1. **Order latency.** `DepletionSimulator(activation_ns=...)` now lets a print count toward an
   order only if received at least that long after the order was placed, and — symmetrically —
   a cancel takes the same latency to arrive, so a repriced order stays fillable until then
   (`cancel_at`). A first, one-sided version (cancels instant, new orders delayed) left the
   quoter with no order in the gap and was discarded as biased. With the symmetric model:
   develop +127,594 (0 ms) → +13,323 (500 ms) → −33,823 (1000 ms); **holdout +199,537 → −40,500
   → −96,808**, market-neutral throughout. Trades reach us ≈0.3–0.5 s after they execute, so
   ≈0.4–0.8 s is the realistic activation delay for anyone not co-located.
2. **Book source.** Depth merged causally with the fresher quote feed: +69,666 on develop.
   Quote feed alone, on a 200 ms decision clock: −39,520. (Evaluated per-packet instead of on a
   decision clock it was −59,172 on Aug 27 alone: a quiet leg's quotes sat on a stale fair value
   while the forward moved. Recorded because it is an easy mistake to repeat.)
3. **Longer-dated series** (Oct; Sep on Aug 24/25) exist only on the quote feed. Since the
   quote-feed adapter failed to reproduce the depth result, mm_v1 was **not** run on them.

The holdout was read a second time for (1), logged with that reason. The conclusion that stands:
the structural changes of #21 make the quoter capture spread and stay market-neutral, but on this
data the edge is smaller than the cost of not being fast — a co-location bet, not a strategy a
retail-latency participant could run. The zero-latency figures measure what the quoting logic
captures; they are not what a real participant would have made.

## 24. The rv-market-maker fork: both latency-tolerant strategies fail their pre-registered bars

*2026-10-02, branch `rv-market-maker`.* After #23 showed the classic edge is latency-bound, `main`
was tagged `classic-mm-v1` and this branch was forked to look for an edge that is not speed.
Each candidate got a go/no-go committed and pushed *before* its statistic was computed
(`config/frozen/protocol_rv.yaml`, `protocol_rv_patient.yaml`), measured on Aug 27/28 only:

1. **Smile relative value** — leave-one-out smile residuals persist (autocorrelation 0.587 at
   5 s, 0.30 at 5 min) and survive a 1 s entry delay almost intact, but the correction toward the
   smile is at best +4.46 bp against 11.85 bp per side. **NO-GO.**
   `reports/rv_strategy1_smile_go_no_go.md`
2. **Patient liquidity provision** — orders δ bp from a 1 s-old fair value. Close in (50 bp) they
   are picked off (−3.75 bp at 30 s); further out, dislocations revert +17–22 bp over 5 minutes,
   below the 23.71 bp round trip and within noise. **NO-GO.**
   `reports/rv_strategy2_patient_go_no_go.md`

Neither was built, so the validation days (Aug 31 – Sep 4) remain untouched by this family.

**The combined conclusion, which is the branch's result:** on this market the two constraints
bind from opposite sides. Edges large enough to pay Indian statutory costs on option premium
(~24 bp round trip) decay faster than ~0.5 s; edges that survive the latency are smaller than
the cost. Market making BANKNIFTY options passively from a non-co-located seat is squeezed out
by speed on one side and STT on the other.

## 25. Three more bugs found while building and stress-testing Phase 2

*2026-09-30.* Recorded alongside #17 and #19 because each would have produced a wrong number.

1. **Order ids from Python `id()`.** The first portfolio runs grouped fills into orders by
   `id(order)`. Python reuses ids after an object is garbage-collected, so later orders shared ids
   with dead ones: sell fills were merged into "buy" orders and their STT was dropped. Member cost
   was understated by ~16% (₹277,018 vs ₹329,055 on the first band-3 run) and Dhan brokerage was
   charged on 32 "orders" instead of 13,007. Found because "32 orders for 13,007 fills" was
   impossible. Fixed before any reported number: the fill model now assigns order ids and every
   fill carries its own.
2. **Event-driven books evaluated per packet.** On the quote feed a quiet option sends nothing
   for seconds. Re-quoting a leg only when its own packet arrived left its quotes on a stale fair
   value while the forward moved (Aug 27: −₹59,172 vs +₹80,643 on depth). Fixed by resampling onto
   a 200 ms decision clock; the adapter still failed validation (§22.3 of BRIEFING) and was not
   used for any reported result.
3. **One-sided latency.** The first latency model delayed new orders but cancelled old ones
   instantly, leaving a repricing quoter with no order in every gap. Replaced by symmetric latency
   (`DepletionSimulator.cancel_at`) before the reported latency results.

## 26. Branch policy: the classic study stays on `main`, new strategy families fork

*2026-10-02.* At the user's request ("this must be a fork so that the classic market maker has its
own place and importance in quant interviews"): `main` holds the complete classic study, pinned by
tag `classic-mm-v1`; `rv-market-maker` holds the latency-tolerant strategy work and never merges
back. `BRIEFING.md` and `DECISIONS.md` are kept identical on both branches so either is a complete
handover; each branch has its own `README.md`. Every new strategy family gets its own frozen
protocol file, committed and pushed before its first statistic.
