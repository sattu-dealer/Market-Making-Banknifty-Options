# BNFMM — Project Briefing and Handover

**BANKNIFTY options market making: an execution-realism study.**

> **What this document is.** The working brief and complete handover for this repo. It is
> written for someone — human or model — picking the work up cold, with no access to the
> conversations that produced it. Reading §0 gives the whole current state; §16 gives a reading
> order for the rest. Every number in §0 is traceable to a report, a log or a commit named there.
>
> The original kickoff brief is preserved verbatim at
> [docs/kickoff_brief_original.md](docs/kickoff_brief_original.md); it is the provenance of the
> binding constraints in §2.
>
> **Last updated: 2026-10-02.** Capture is closed. Two lines of work are complete: the classic
> market-making study (branch `main`, tag `classic-mm-v1`) and a fork exploring latency-tolerant
> strategies (branch `rv-market-maker`). Both reach conclusive, negative-but-explained results.
>
> **How to read the rest.** §0 is current. §1–§12 are the design record (framing, interfaces,
> architecture, mathematics) and remain accurate except where a ⚙/⚠ status note says otherwise.
> §13–§19 are a dated history of open items and plans written between 2026-08-23 and
> 2026-09-04; each now carries a status banner saying what became of it. §20–§26 describe the
> work done from 2026-09-30 onward. Appendix A keeps superseded status snapshots for the record.
>
> Dates are absolute. If you find a surviving "today", treat it as a bug in this document.

---

## 0. START HERE — the complete current state (2026-10-02)

### 0.1 The project in one paragraph

A placements-focused research project: simulate passive market making in NSE **BANKNIFTY
monthly options** on order-book data captured live from the broker Dhan, and measure honestly
whether it can make money. Ten sessions of 20-level depth were captured (2026-08-24 → 09-04,
64 GB). A first single-strike quoter lost money and its one profit was a directional bet (§17).
A rebuilt **portfolio** market maker (§20–§21) captures spread, is market-neutral (R² of PnL on
the market ≈ 0), and was profitable on five held-out sessions **at zero latency** — but loses at a
realistic 500 ms latency (§22). A fork then tested two latency-tolerant strategies under
pre-registered criteria; both failed (§23). **The result:** on this market, edges large enough to
pay the ~24 bp round-trip statutory cost on option premium decay in under ~0.5 s, and edges slow
enough to survive that latency are smaller than the cost. Speed squeezes from one side, STT from
the other. No order was ever placed; no trading endpoint exists in the code.

### 0.2 Branches, tags and what lives where

| Ref | What it is | Head |
|---|---|---|
| `main` | The complete **classic** study: capture layer, data QA, Phase 1 and Phase 2 market makers, latency robustness, README written around the classic result | `1f15f8d` (+ doc-only commits after it) |
| tag `classic-mm-v1` | Pins the classic study exactly as concluded | `1f15f8d` |
| `rv-market-maker` | Fork from `main` at `1f15f8d`. Adds the smile module, two pre-registered go/no-go studies, their reports, and `DECISIONS.md` #24. **Nothing on this branch modifies the classic code.** | `64aaf76` (+ doc-only commits after it) |

Remote: `https://github.com/sattu-dealer/Market-Making-Banknifty-Options` (both branches and
the tag are pushed). The user wanted the classic study kept intact "so the classic market maker
has its own place and importance in quant interviews" — **never merge `rv-market-maker` into
`main`**. Documentation shared by both (`BRIEFING.md`, `DECISIONS.md`) is kept identical on both
branches; `README.md` differs (each branch's README describes its own line of work).

Files that exist **only** on `rv-market-maker`: `src/bnfmm/fairvalue/smile.py`,
`tests/test_smile.py`, `scripts/rv_persistence.py`, `scripts/rv_patient.py`,
`config/frozen/protocol_rv.yaml`, `config/frozen/protocol_rv_patient.yaml`,
`reports/rv_strategy1_smile_go_no_go.md`, `reports/rv_strategy2_patient_go_no_go.md`.

### 0.3 State at a glance

| | |
|---|---|
| **Capture** | **Closed** by user decision (2026-09-30: "we are not collecting anymore data"). Dhan Data API entitlement lapsed 2026-09-22 19:28:31 IST. Last session captured: 2026-09-04 |
| **Data** | 10 sessions, `data/` (gitignored, local only, ~64 GB). 8 usable for quoting studies (Aug 26 excluded by a coverage rule; Aug 24/25 excluded by a pre-registered expiry rule). Integrity verdict: fit for simulation — §21.1, `reports/data_integrity.md` |
| **Tests** | `main`: 583 collected, 582 pass. `rv-market-maker`: 587 collected, 586 pass. The one failure on both is the known stale-master test (§13.9) |
| **Code size** | ~13.7k lines in `src/` + `scripts/` + `main.py`; tests in `tests/` |
| **Configurations ever run** | 41 lines in `reports/config_log.jsonl` (27 before the mm_v1 freeze = 26 distinct; 12 post-freeze robustness runs of the frozen strategy; 2 RV measurements) |
| **Holdout reads** | 2, both of the frozen mm_v1, both logged in `reports/holdout_log.md` (§22.4) |
| **Git** | Everything committed and pushed. Author `sattu-dealer <just4pc1117@gmail.com>` set in the repo-local config only |
| **Broker / instrument** | Dhan DhanHQ v2; BANKNIFTY monthly options (no weeklies exist), front series, ATM ±11 strikes on the depth feed |
| **Environment** | Fedora, 24 cores, 15 GB RAM; Python 3.14.7 in `.venv/` (`.venv/bin/python`). Memory is the binding resource — §25 |

### 0.4 Results ledger — every result that matters, with where it comes from

**Classic line (`main`)**

| Result | Numbers | Source |
|---|---|---|
| Phase 0: options, not futures | Options round-trip floor 47 ticks member vs futures 163; options STT on premium (58× smaller base) | `reports/phase0_instrument_selection.md`, §3.1 |
| Phase 1: single-strike quoter, Aug 24/25 | Loses in every configuration; only profit (+₹92,433) was 58% inventory PnL — a bet | §17, `reports/phase1_market_maker_results.md` |
| Data integrity | 34.3M option snapshots: 0 off-tick, 0 non-positive, 0.004% crossed/locked; tape bug fixed (stale-packet replays, 5.8% phantom volume on Sep 4); depth feed lags quote feed 225 ms | §21.1, `reports/data_integrity.md`, `reports/data_quality.md` |
| Phase 2 develop (Aug 27+28), frozen mm_v1, 0 ms | +₹127,594 member; +₹105,441 liquidated; R² ≤ 0.017 | §20, `reports/phase2_portfolio_market_maker.md` |
| **Phase 2 holdout (Aug 31–Sep 4), 0 ms** | **+₹199,537** member, **+₹155,159** liquidated, 5/5 days; capture ₹735,694 vs inventory −₹222,432; R² 0.002–0.18; per-fill 95% CI [₹6.34, ₹20.40] | §20.1 |
| Retail cost (Dhan ₹20/order, 1 lot), holdout 0 ms | −₹121,494 | §20.1 |
| **Latency, holdout** | **500 ms: −₹40,500** (liquidated −₹84,754), 1/5 days; 1000 ms: −₹96,808 | §22.2, `DECISIONS.md` #23 |
| Latency, develop | 0 / 500 / 1000 ms: +₹127,594 / +₹13,323 / −₹33,823 | §22.2 |
| Book source, develop (0 ms) | depth +₹127,594; depth merged with fresher quote feed +₹69,666; quote feed alone −₹39,520 | §22.3 |
| Longer-dated series (Oct; Sep on Aug 24/25) | **Not run** — the quote-feed adapter failed validation | §22.3 |

**Fork (`rv-market-maker`)** — both go/no-go criteria committed and pushed before measuring.

| Strategy | Result | Source |
|---|---|---|
| #1 Smile relative value | Residual autocorrelation 0.587 at 5 s (PASS ≥0.5); correction toward smile after 1 s delay +4.46 bp vs 11.85 bp per-side cost (FAIL) → **NO-GO** | §23.3, `reports/rv_strategy1_smile_go_no_go.md` |
| #2 Patient liquidity | Best cell δ=100 bp, 5 min: +21.63 bp vs 23.71 bp round trip, t −0.07; δ=50 bp picked off (−3.75 bp at 30 s) → **NO-GO** | §23.4, `reports/rv_strategy2_patient_go_no_go.md` |

### 0.5 The conclusion, stated so it survives an interview

1. **A correctly built market maker on this data is genuine market making.** After four
   structural changes (§20.2) its profit comes from spread capture, its inventory term is
   negative, and its PnL is uncorrelated with the market. Phase 1's "profit" was not.
2. **That edge is shorter-lived than the latency of anyone receiving these feeds.** Trades
   arrive ~0.3–0.5 s after they execute; at 500 ms of order latency the held-out profit becomes a
   loss. In the terms of §11.6: the latency slope answers "strategy or co-location bet", and it
   answers co-location.
3. **Latency-tolerant edges exist but cannot pay statutory costs.** Smile mispricings persist
   for tens of seconds but are ~4.5 bp against ~11.9 bp per side; patient deep quotes see
   reversion of ~17–22 bp against a ~23.7 bp round trip.
4. **Therefore** passive market making in BANKNIFTY options from a non-co-located seat is
   squeezed out by speed on one side and STT on the other. Every step of that conclusion is a
   measurement with its configuration logged, not an assertion.

What it is **not** safe to claim: that longer-dated options are profitable (never validly
tested, §22.3); that the zero-latency figure is achievable money (it is an upper bound on what
the quoting logic captures); that any result generalises beyond one monthly cycle at 25–33 DTE.

### 0.6 Rules for whoever continues — these protect the credibility of everything above

* **No order placement, ever** (§2, enforced by `tests/test_no_order_placement.py`).
* **The classic holdout (Aug 31 – Sep 4) is spent for classic tuning.** Any change to mm_v1 is
  mm_v2 and has no untouched data. Say so in any write-up.
* **The RV validation days (same dates) are untouched by any RV analysis.** Both RV strategies
  were NO-GO and never built, so those days remain clean *for the RV family only* — but they were
  read by the classic study, so results on them are weaker evidence (stated in
  `config/frozen/protocol_rv.yaml`).
* **Pre-register before measuring.** Commit and push the go/no-go criteria, then run. Every run
  of `scripts/mm.py`, `rv_persistence.py`, `rv_patient.py` and `backtest.py` is appended to
  `reports/config_log.jsonl` automatically; quote "from N configurations".
* **Holdout days go through the loader.** `bnfmm.analysis.holdout.require_access` refuses them
  without `--unlock-holdout "REASON"` and logs every unlock to `reports/holdout_log.md`. Never
  edit either log by hand. Never edit a frozen file after it has been used; copy it to a new name.
* **Do not reuse any trade tape computed before 2026-09-30** (tape bug, `DECISIONS.md` #20).
* **Report both cost profiles** (member and Dhan) and the capture/inventory split on every result.
* **Do not merge `rv-market-maker` into `main`.**

### 0.7 What could be done next (none of it is in progress)

In the order I would recommend:

1. **Cost-sensitivity / breakeven-STT analysis** (no new data needed). For the frozen mm_v1 at
   500 ms and for both RV signals, compute the STT rate (or total bp cost) at which each breaks
   even. Turns the conclusion into a number: "viable if round-trip cost were X bp". Cheap,
   uses existing scripts with a modified `config/costs.yaml` copy. Pre-register the cost grid.
2. ~~**README for the `rv-market-maker` branch.**~~ Done 2026-10-02: the branch README now opens
   with a fork summary (§23) and links back to `main`.
3. **Smile signal as an ingredient** (new hypothesis, needs its own pre-registration): use the
   leave-one-out smile value as fair value inside the passive quoter. Still latency-exposed, so
   expect a modest effect; the RV validation days are the only clean data left for it.
4. **Housekeeping**: refresh the instrument master and fix §13.9 (a 36 MB public download from
   Dhan — ask the user first; the 2026-08-23 master is archived so nothing is lost); run
   `ruff check` (§13.4); remove the two dangling `sim.fills.sensitivity` references (§13.13);
   tests for the remaining untested modules (§13.2).
5. **More data** would change everything above — a longer window, other expiry cycles, a spot
   index feed — but the user has decided not to collect more.

### 0.8 Project timeline

| Date | Event |
|---|---|
| 2026-08-23 | Kickoff; Phase 0 (instrument choice: options); capture layer built |
| 2026-08-24 → 09-04 | 10 sessions captured (Aug 26 lost to an expired token) |
| 2026-08-26 → 09-03 | Phase 1 single-strike market maker built and measured on Aug 24/25 — negative |
| 2026-09-04 | §18/§19 plans written; capture stopped after this session |
| 2026-09-22 | Dhan entitlement lapsed |
| 2026-09-30 | STT verified; contract sidecars fixed; holdout frozen; data integrity pass; portfolio market maker built; mm_v1 frozen and evaluated on the holdout; latency and book-source robustness; first commit and push |
| 2026-10-02 | `main` tagged `classic-mm-v1`; README rewritten; `rv-market-maker` forked; RV #1 and #2 pre-registered and measured — both NO-GO |

## 1. Origin, framing, and how the framing changed

### 1.1 Where the project came from

The repo began as a single file: a "Claude Code Kickoff Brief: Market Making Simulation on
BANKNIFTY", a placements-focused portfolio project for a student targeting quant/finance
and data roles. The original ambition was market making across BANKNIFTY "options,
futures, stocks, and potentially other related instruments."

That ambition was scoped down deliberately, with the reasoning recorded rather than
assumed. Market making simultaneously across options, futures and cash equity is close to
a full desk's mandate; options market making alone needs a live consistent implied
volatility surface plus delta/vega management. The brief itself invited divergence — its
§4 asked for "a concrete technical plan and repo structure — matching the direction above,
improving on it, or diverging from it with justification" and for an early, explicit flag
if the idea itself was not the strongest use of the time available.

### 1.2 The framing that was chosen, and why it is defensible

**This is an execution-realism study, not a strategy.** The deliverable is not "a market
maker that makes money." It is a *measurement*: **how far does a naive instant-fill
backtest overstate BANKNIFTY options market-making PnL?**

Why that framing is stronger for the stated purpose:

- **It cannot fail.** A strategy project fails if the strategy loses money. A measurement
  project succeeds either way — "quoting at the touch is structurally unprofitable at
  retail cost levels, and here is the decomposition proving it" is a *result*, and a more
  interesting one than a positive Sharpe on 21 days of data (which, as §11.5 shows, would
  not be statistically distinguishable from zero anyway).
- **It targets the exact thing a market-making-literate interviewer probes first.** The
  original brief named it: "the single biggest risk to the project's credibility [is] a
  backtest that assumes instant fills at the quoted price." Making that flaw the *subject*
  rather than the hazard inverts the risk.
- **It is honest about what a laptop-based retail study can and cannot show.** No latency
  claims, no co-location claims, no live PnL claims.

The headline artifact is a **2×2 grid** (§5.1), not a number.

### 1.3 The redirection to harvesting-first (this is the most important thing to understand)

> ⚙ **Status 2026-10-02.** Harvesting is over: capture stopped after 2026-09-04 and the user
> decided on 2026-09-30 that no more data will be collected. The principle below explains why the
> capture layer is so heavily tested; it no longer drives scheduling.

The original plan built the simulator first and captured data alongside. That was inverted
on explicit instruction, and the inversion is the reason the repo looks the way it does:

> "Remember that now the objective is to record and log all live market data we can get
> from market open to close, store it and then we can use it later to test the market
> maker on. Live Testing is not the current objective, systematic data harvesting is […]
> All we need to do via the API right now is log data which we can use to test our market
> maker on anytime later." — user, 2026-08-23

**The asymmetry that justifies it:** a trading session happens once and cannot be re-run.
The simulator can be written at any time. Therefore anything on the critical path to
recording bytes is urgent, and *everything else is not*. This is why:

- `scripts/capture.py`, `data/rawlog.py`, `data/reconnect.py`, `data/channels.py`,
  `data/chain.py`, `data/universe.py`, `config/capture.yaml` and `main.py` were built and
  tested **first**, while
- `sim/fills/`, `fairvalue/`, `book/` and `strategy/` were left empty, and
- `data/qa.py`, the Tier C synthetic generator and the ruff pass were all deferred out of
  Phase 1a into 1c, purely because they read stored files rather than a socket.

**This is now history rather than current state.** All four of those packages were built
between 2026-08-26 and 2026-09-03 (§6, §17), out of the phase order in §14, because the
user asked for a working market maker on the first two captured days. What remains true is
the ordering *principle*: capture is still the only thing that cannot be recovered later,
and it still runs every morning regardless of what else is in flight.

The visible consequence of building analysis in one burst is that **the new modules are the
least tested code in the repo** (§13.2) — the inverse of the capture layer, which has 400+
tests. A colleague should read that asymmetry as the same scheduling decision seen from the
other end, and should close it before quoting any number from §17 as settled.

### 1.4 Why Aug 25 was captured rather than skipped

The August contract expired **Tue 2026-08-25**. An earlier draft proposed skipping it as
unreachable and starting clean with September. That was rejected on instruction, correctly,
for two independent reasons:

1. **Infrastructure shakedown on a live open.** The wire decoders were built entirely
   against `tests/fixtures/packets.py` — a faithful reading of the vendor SDK's byte
   layout. A faithful reading is still a reading. Two sessions of real bytes before the
   September cycle begins is a rehearsal that would otherwise have to be taken on the
   cycle that matters.
2. **A 0-DTE expiry-day regime sample.** BANKNIFTY on expiry day is a different market:
   gamma dominates, ATM time value collapses toward intrinsic, spreads and depth behave
   unlike any other day, and pin risk is real. It is the most demanding regime for a market
   maker and the one where inventory limits actually bind. The September cycle supplies one
   expiry; capturing Aug 25 supplies **two**, reached by different DTE paths.

This also makes the subscription-lapse problem cheap — see §4.7.

**Both reasons were vindicated.** The decoder handled real bytes on Aug 24 with no
anomalies, and the two days became the *entire* dataset the market maker was tested on
(§17) — chosen precisely because 1 DTE and 0 DTE are the two highest-information sessions
available. The expiry-day session also produced the NaN-closing-mark bug (`DECISIONS.md`
#19), which only a dying 0 DTE book exposes and which would otherwise have sat undetected
until the September expiry.

---

## 2. Binding constraints — these are not negotiable

Quoted verbatim from the original brief. They survive the rewrite unchanged and were
reinforced directly by the user.

> **No live capital, no real order placement** — this is a research/simulation project
> only. Do not wire up trading/order-placement endpoints even where the SDK makes it
> convenient. *(§5 of the kickoff brief)*

> **Non-negotiable constraint:** whatever data strategy is chosen, the final writeup must
> clearly and honestly label what is real captured market data versus synthetic,
> simulated, or proxy data. Overstating this is exactly the kind of thing that damages
> credibility badly if probed in an interview. *(§2 of the kickoff brief)*

> This refresh should not be automated around the OTP step — assume a human supplies fresh
> tokens each morning. *(§2, written about Paytm Money)*

Reinforced by the user in their own words: **"No Live trading at all, rather what I meant
was testing the market making during live market with live market data on the go instead
of harvested data of some other previous day."**

Both of the first two are **enforced structurally**, not by convention — see §5.2 and
§5.3. The third is discussed at §5.4 and `DECISIONS.md` #14: the letter of it (no local
TOTP) was diverged from, with the intent (human-supplied credentials, never committed)
preserved.

Three standing directives from the user:

- **Do not commit yet.** "No need to commit anything right now, we'll do it much later in
  the project."
- **Ruff is the implementer's call.** Resolved as `ruff check` only, no `ruff format` — see
  §13.4.
- **Assume the Dhan monthly subscription is taken.** Now moot: it *is* taken and active.

---

## 3. Locked decisions

Eight decisions were locked through two rounds of explicit questions. `DECISIONS.md` (741
lines, 19 numbered entries plus sub-entries) is the running log the kickoff brief §4.5
asked for; it holds the long-form reasoning. This is the index.

| # | Decision | Rationale in one line |
|---|---|---|
| 1 | **Timeline 8–12 weeks** | Enough for a real capture window plus a validated simulator; not so long the project rots |
| 2 | **Broker: Dhan, not Paytm Money** | Documented v2 API, 20-level depth, public instrument master, and a Data API subscription that can actually be bought |
| 3 | **Framing: execution-realism study** | See §1.2 |
| 4 | **Instrument: decided on Phase 0 evidence, not upfront** | Became options, not futures — see §3.1 |
| 5 | **Cost case: both, paired everywhere** | Member (₹0 brokerage) and retail (Dhan ₹20/order) reported side by side, always |
| 6 | **No live trading at all** | See §2 |
| 7 | **Dhan account with Data API** | Was "account exists, no Data API"; superseded — subscription is live |
| 8 | **Capture host: laptop, expect gaps** | Drives segment-level rather than session-level analysis — see §5.5 |

### 3.1 The instrument decision, because it inverted the original plan

The kickoff brief proposed quoting **futures** and using the option chain only as a
fair-value signal. Phase 0 measured the cost floor and reversed that. `DECISIONS.md` #9,
`reports/phase0_instrument_selection.md`:

| Instrument | STT base | Round trip / lot (member) | Breakeven (member) | Breakeven (retail) |
|---|---|---|---|---|
| Futures | **notional** (₹1.73 crore/lot) | ₹867+ | **163 ticks = ₹32.68** | 171 ticks = ₹34.25 |
| Options | **premium** (~₹30k/lot) | ₹71.13 | **47 ticks = ₹2.37** | 79 ticks = ₹3.94 |

Options STT is levied on premium; futures STT on notional — a **58× smaller base**. A
futures market maker at retail rates would need to capture 163 ticks per round trip on an
instrument whose spread is typically 1–5 ticks. That is not a marginal disadvantage, it is
a different sign. Options are **14× more favourable** at member rates, 9× at retail.

Consequence: Greeks, an IV smile and put–call parity moved from "nice to have" into the
**core** of the project, because you cannot fairly price an option without them. That is
§10.

---

## 4. The external interface: DhanHQ v2

Everything in this section was verified against the live API or read from the installed
SDK at `.venv/lib64/python3.14/site-packages/dhanhq/`, not inferred from documentation
prose. Wrong assumptions here cost trading days.

### 4.1 Authentication

Two entirely separate flows exist. **We use the first and must never touch the second.**

**Path A — programmatic TOTP login** (`src/bnfmm/data/auth.py`):

```
DhanLogin(client_id).generate_token(pin, totp)
  → POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&pin=…&totp=…
```

Note there is **no `app_id`/`app_secret`** in this flow — a natural assumption that is
wrong. The TOTP is computed locally from a shared secret (`DECISIONS.md` #14).

**Path B — paste a token** (what is in use today). Mint a 24-hour access token in the
DhanHQ developer portal and paste it into `.env`. `auth.login(allow_env_token=True)`
takes it directly. This is the path currently populated.

**The OAuth consent flow** (`generate_login_session` / `consume_token_id`) is a third,
separate path. We do not use it. The portal's "API Key" and "API Secret" fields belong to
it and are irrelevant here — a real source of confusion, documented in `.env.example`.

Canonical request headers, matching `Session.headers()` exactly:
`{'access-token', 'client-id', 'Content-type', 'Accept'}`.

**The access token rides in the WebSocket connect URL query string.** The URL is therefore
a credential. `auth.redact_url` exists for this and is used on every log line and in every
session manifest. A test asserts no token reaches `data/`.

`set_ip` / `modify_ip` / `get_ip` exist in the SDK. They are **prerequisites for order
placement only**. A guard test asserts they are unreachable from `src/`. The portal also
only permits a static IP to be re-set once every 7 days, so an accidental call is not
cheaply undone.

### 4.2 Wire protocol — 20-level depth

Implemented in `src/bnfmm/data/protocol.py`, `depth_decoder()`.

| | |
|---|---|
| Header | `<hBBiI`, **12 bytes** (`DEPTH_HEADER_SIZE`) |
| Payload | `<dII` × 20 levels = 320 bytes |
| **Total frame** | **332 bytes** |
| Message codes | **41** = bid side, **51** = ask side, **50** = disconnect |
| Subscribe | `RequestCode: 23` |

Each side arrives as a **separate frame**. There is no combined book message: a full
20-level book at one instant is two frames that must be paired by the reader.

### 4.3 Wire protocol — general feed

Implemented in `protocol.py`, `market_feed_decoder()`.

| | |
|---|---|
| Header | `<BHBI`, **8 bytes** (`FEED_HEADER_SIZE`) |
| Subscribe | `CAPTURE_REQUEST_CODE = REQUEST_FULL = 21` |

Packet sizes by message code, all eight implemented:

| Code | Type | Bytes |
|---|---|---|
| 2 | Ticker | 16 |
| 3 | Quote | 50 |
| 4 | OI | 12 |
| 5 | PrevClose | 16 |
| 6 | Status | 8 |
| 7 | Depth5 | 112 |
| **8** | **Full** | **162** |
| 50 | Disconnect | 10 |

`REQUEST_FULL` yields code 8, which carries the 5-level book, cumulative volume, and open
interest in a single packet.

### 4.4 Both protocols: no sequence number

**Neither wire protocol carries a sequence number.** `DECISIONS.md` #15.2 records this as a
correction to an earlier assumption. Consequences that propagate through the whole design:

- **Packet loss is undetectable from the wire.** A dropped frame leaves no gap to find.
- Local arrival time is the *only* clock on the depth feed — it carries no exchange
  timestamp either (`DECISIONS.md` #7). This is why `store.py` records **two** clocks on
  every row (§7.3) and why laptop suspend is a correctness problem, not an inconvenience.
- `recv_seq` in the Parquet is **capture-assigned**, not from the exchange. It breaks
  timestamp ties and validates the read path. It cannot detect exchange-side loss, and QA
  must not claim otherwise.

### 4.5 Disconnect codes, and a deliberately inverted retry rule

```python
DISCONNECT_REASONS = {
    805: "no. of active websocket connections exceeded",
    806: "subscribe to Data APIs to continue",
    807: "access token expired",
    808: "invalid client id",
    809: "authentication failed",
}
FATAL_DISCONNECTS = frozenset(DISCONNECT_REASONS)
```

**Documented codes are fatal. Undocumented codes and bare socket closes are retryable.**

That is backwards from the usual instinct and it is deliberate. Every documented reason
describes a condition a reconnect cannot fix — the subscription is absent, the token is
dead, the client id is wrong, or there are already too many connections and retrying makes
it *worse*. An unknown code or a silent close is far more likely to be a Wi-Fi blip, which
is exactly what backoff is for. `data/reconnect.py`, 43 tests.

### 4.6 Rate and connection limits

| Limit | Value | What it actually binds |
|---|---|---|
| WebSocket connections per user | **5**; a 6th disconnects the **first** | We use 3, hold 2 in reserve |
| Instruments per 20-level depth connection | **50** | The real constraint on the study |
| Instruments per general-feed connection | **5000** (100 per subscribe message) | Effectively unbounded here |
| Server silence timeout | **40 s** (`SERVER_SILENCE_TIMEOUT_S`) | Requires client-side ping |
| Data API REST | 5/sec, 100,000/day | Generous |
| **Option Chain REST** | **1 unique request / 3 s** (`OPTION_CHAIN_MIN_INTERVAL_S`) | The binding rate limit |

Two consequences worth internalising:

**The rate limits do not constrain the WebSocket universe at all.** The constraint is the
50-per-depth-connection *connection* cap and the 5-connection ceiling. More depth means
more sockets, not more requests.

**A 6th connection kills the first, not the sixth.** So exceeding the cap destroys the
connection that is working. This is why `reserve_connections: 2` and why every reconnect
sends an explicit `DISCONNECT_MESSAGE` before closing — a half-open socket left by a
suspended laptop may still be counted server-side.

### 4.7 Entitlement, and the date that constrains the whole capture window

Verified live against `/v2/profile`:

```
Data APIs:    Active
dataValidity: 2026-09-22 19:28:31 IST
```

That timestamp is a **fact**, replacing the plan's estimate of "around Sep 23". It lapses
*after* that day's close, so **Sep 22 is fully capturable**. The consequences:

- The September cycle is capturable **Aug 26 → Sep 22**, i.e. from ~34 DTE down to
  **7 DTE**.
- **The Sep 29 expiry is out of reach.** There is no 0-DTE September sample.
- **The renew-or-not decision point is ~Sep 20.** By then it will be known whether the
  captured data is actually being used, which is the right time to spend more money.

**This is affordable only because Aug 25 is being captured.** With a 0-DTE sample already
in the dataset from August, what the lapse costs is a *second* expiry-day sample, not the
only one. That is the payoff of the §1.4 decision.

### 4.8 API gotchas that cost real time — read this before touching an endpoint

- **The option chain endpoint needs the INDEX security id, not the futures id.** Pass
  `UnderlyingScrip: 25` with `UnderlyingSeg: "IDX_I"`. Passing the futures security id
  returns **HTTP 200 with an empty body** — it looks like success. This is invisible in the
  reply and is exactly the kind of error that produces an empty dataset at 15:40.
- **`IDX_SEGMENT_CODE = 0`** — deliberately falsy. Any `if segment:` test silently drops
  the indices, and the indices are the anchor of the entire fair-value estimator (§10.3).
  Compare against `None` explicitly.
- **`/v2/charts/rollingoption` returns `DH-905 "securityId is required"`.** An
  SDK-only, undocumented endpoint whose request body we guessed wrong. Note it is **not**
  `DH-902`, so this is a malformed request, not an entitlement gate — it is fixable.
  Non-blocking for capture; relevant to the Tier B backfill (§13.2).
- **`TICK_SIZE` in the instrument master is denominated in paise**, not rupees
  (`DECISIONS.md` #6). Confirmed by cross-checking index options at `5.0` against the
  published ₹0.05.
- **`_expiry` must be accessed by bracket, not attribute** (`DECISIONS.md` #11).
- **`security_id` is both a real Parquet column and a hive partition key.** Reading the
  dataset directly raises `ArrowTypeError: int32 vs dictionary<values=int32…>`. Use
  `store.read_capture` — it is the intended reader and handles this. Not a bug; a trap.

### 4.9 Pinned contract facts

Read from the live instrument master, not from documentation. `tests/test_master_facts.py`
(9 tests) asserts these against the real CSV so a silent exchange change fails the suite.

| Fact | Value |
|---|---|
| BANKNIFTY weeklies | **None.** One monthly expiry only |
| Expiry day | **Tuesday** |
| Expiries live now | **2026-08-25**, 2026-09-29, 2026-10-27 (all Tuesdays) |
| Lot size | **30** |
| Futures tick | **₹0.20** |
| Options tick | **₹0.05** |
| Freeze quantity | **601 units = 20 lots** (601/30 = 20.03) |
| Near-money strike spacing | **100 points** |
| NIFTY BANK index security id | **25** |
| NIFTY 50 index security id | **13** |
| BANKNIFTY AUG FUT security id | 58067 |

The "no weeklies" fact matters: NSE has listed BANKNIFTY weeklies in the past and may
again, so every contract fact is **resolved at runtime and only constrained in config**
(`DECISIONS.md` #5). Nothing above is hardcoded in Python.

---

## 5. Decisions enforced by code rather than by discipline

> ⚙ **Status 2026-10-02 for the three gaps in the table below.** (1) `PnlGrid` is still bypassed:
> the Phase 2 driver `scripts/mm.py` reports both brokerage profiles on every run but emits plain
> floats, and the queue convention is a flag (`--optimistic-queue`), run as a robustness check
> (§20). (2) Segments are measured by `data/qa.py` and the inclusion rule uses them, but the
> simulators still run a whole day with a staleness rule rather than per-segment. (3) The cost
> floor **is now in the quoting decision**: `sim/portfolio.py` requires each side to clear its
> share of the round-trip statutory cost and stands a leg down when its spread is below 37.8 bp of
> fair value (§20.2).

Reporting discipline that depends on remembering to be disciplined fails. Each of these
has a structural guarantee.

⚠ **Three of those guarantees are not actually in the code, and the analysis layer built in
Phase 1 is where all three failed.** Recorded here rather than only in the subsections, because
the pattern is the finding:

| Guarantee | Stated in | Reality |
|---|---|---|
| Every PnL figure is a 2×2 | §5.1 | `scripts/backtest.py` bypasses `PnlGrid`; the convention is a CLI flag |
| Segment, not session, is the unit of analysis | §5.5 | `max_gap_s` exists nowhere; `run_day` pools a whole day (§19) |
| The quoted edge must clear the cost floor | `QuoteParams` docstring | the gate is a hardcoded `1.0` placeholder (§18.2) |

In each case a *plausible-looking default* stood in for the guarantee — a flag defaulting to the
right convention, a per-day loop, a constant named as though it were derived. **None of them is
wrong on its face, which is why all three survived.** A structural guarantee that can be
satisfied by a default is not structural; the ones in this section that held (`Session.__repr__`,
`redact_url`, the no-order-placement test) all fail *loudly* when violated. Prefer that shape.

### 5.1 Every PnL figure is a 2×2 grid

There is no headline cost number. Every PnL result is reported across
**brokerage regime × cancellation convention**:

| | pessimistic queue | proportional queue |
|---|---|---|
| **member** (₹0 brokerage) | … | … |
| **retail** (Dhan ₹20/order) | … | … |

*Enforcement:* `analysis/results.py` defines `PnlGrid` with **all four corners required at
construction**. There is no `float` PnL anywhere downstream — a single number is
*unrepresentable*. `analysis/report.py` renders only `PnlGrid`. Tests assert `PnlGrid`
rejects partial construction and that the recon report and README headline each contain
both regime labels. `DECISIONS.md` #13.

*Why this is stronger than a point estimate:* if the sign of PnL flips inside the grid,
**naming the corner where it flips is the finding.** "Profitable only in the
member/proportional corner" is a far more credible sentence than any single number.

The two cancellation conventions exist because of an identity that cannot be inverted.
From snapshot deltas, `Δqty = adds − cancels − trades`. Trades are observable from the
volume tape; adds and cancels are **not separable**. So the fill simulator cannot know how
much of a queue ahead of it was cancelled versus traded. Pessimistic (assume nothing ahead
cancels) and proportional (assume cancels are spread evenly) give a **lower and upper
bound**. Hence every figure is an interval, and `pessimistic ≤ proportional` is a property
test. `DECISIONS.md` #15.3.

#### ⚠ This guarantee is currently broken — read before quoting any §17 number

`scripts/backtest.py` **does not route through `PnlGrid`.** It reports the brokerage axis
correctly (member and Dhan are printed for every run, always paired) but it emits plain
floats, so the structural guarantee above is bypassed rather than satisfied. Two specific
defects:

1. **The cancellation axis is a CLI flag, not a grid column.** The optimistic queue
   convention is reachable via `--optimistic-queue`, which sets
   `DepletionSimulator(queue_at_price_ahead=False)`. That is one run against the other, not
   two corners reported together, so nothing forces both to appear side by side and nothing
   asserts `pessimistic ≤ proportional`.
2. **`PnlGrid` is therefore unexercised on real data.** `analysis/results.py` and its 28
   tests still pass, but no result from the actual backtest has ever been constructed
   through them.

This matters more than a tidiness complaint. §5's entire premise is that reporting
discipline which depends on remembering to be disciplined *fails*, and the first time real
results were produced under time pressure, it did exactly that. **The honest reading is
that the §17 numbers are not yet reported to the standard this document sets**, and closing
the gap is item 3 on the §0 next-action list.

The repair is small: have `run_day` return both queue conventions (the simulator already
takes the flag), build a `PnlGrid` from the four corners in `scripts/backtest.py`, and add
the `pessimistic ≤ proportional` property test that §14 Phase 3 promised.

### 5.2 No order placement, enforced two ways

`tests/test_no_order_placement.py`, 23 tests:

- **Import allowlist.** The set of `dhanhq` submodules reachable from `src/` must be a
  subset of `{auth, dhan_context, dhan_http, marketfeed, fulldepth, _historical_data,
  _option_chain}`. This excludes `_order`, `_super_order` and `_forever_order` *by
  construction*, including aliased and transitive imports.
- **Symbol grep.** `src/` must not contain `place_order`, `modify_order`, `cancel_order`,
  `/orders`, or `super_order`.

No `broker/` or `execution/` package exists. `data/auth.py` never calls `set_ip`.

### 5.3 Data-tier labelling, enforced in the renderer

Every result object carries a **required** `tier` field:

| Tier | Meaning |
|---|---|
| `A_REAL_L2` | Real captured 20-level depth — what this project is for |
| `B_REAL_1MIN` | Real but coarse: historical 1-minute bars via REST |
| `C_SYNTHETIC` | Simulated order book from assumed parameters |

`report.py` prints the tier in every caption and **raises** if a `C_SYNTHETIC` result
reaches the README headline slot. This makes the kickoff brief's non-negotiable labelling
constraint (§2) a checkable property rather than an intention.

### 5.4 TOTP computed locally — a reasoned divergence

The brief said the refresh "should not be automated around the OTP step — assume a human
supplies fresh tokens each morning." `auth.py` can compute a TOTP locally, which is a
partial divergence from the letter of that line. `DECISIONS.md` #14 records the reasoning:
the *intent* — credentials are human-supplied and never committed — is preserved
absolutely (secrets come from a gitignored `.env`, and there is a test that no token
reaches `data/`), while a human retyping a 6-digit code at 08:50 every morning is a
single point of failure on the one deadline that cannot slip. **Today's run uses Path B
(a pasted token) anyway**, so the divergence is currently unexercised.

### 5.5 Segment, not session, is the unit of analysis

⚠ **Unimplemented as of 2026-09-04.** `max_gap_s` appears nowhere in `src/`, `scripts/`,
`config/` or `tests/`, and no module under `sim/`, `book/` or `analysis/` mentions a segment.
`sim/backtest.py:run_day` treats a calendar day as one continuous series, carrying a staleness
flag but never a segment boundary — so the §17 numbers pool gapped and clean periods into single
per-day statistics, which is exactly what this section says not to do. See §19 for the
consequences, which are larger than a QA inconvenience.

Follows from "laptop, expect gaps." Session-level QA would discard an entire trading day
for one 30-second dropout. So a gap exceeding `max_gap_s` **ends a segment**, and each
segment gets independent QA. Walk-forward folds still split by **calendar day, never by
segment** — adjacent segments from one day landing on both sides of a split would leak.

The two clocks make the *cause* of a gap classifiable, which is the real payoff:

| Gap visible in | Diagnosis |
|---|---|
| both clocks | genuine feed dropout |
| wall clock only | laptop suspend, or a forward NTP step |
| monotonic only | backward NTP step |

Without this, resume-from-sleep produces timestamps indistinguishable from a real
dropout — and since there is no sequence number (§4.4), there is no other way to tell.

---

## 6. Repo map

⚙ **Rewritten 2026-10-02.** Line counts are current. "(rv)" marks files that exist only on the
`rv-market-maker` branch. Tests per file in parentheses.

```
BNFMM/
├── main.py                      935  Single entry point: record (capture) / analyse modes. §8
├── BRIEFING.md                       This document (identical on both branches)
├── DECISIONS.md                      26 numbered decisions with reasoning (identical on both branches)
├── README.md                         Public summary. main: classic result. rv branch: same file (§0.7 item 2)
├── pyproject.toml                    Py >=3.11 (venv is 3.14.7); ruff configured, never run (§13.4)
├── .env.example                      Which Dhan portal fields matter. .env is gitignored
│
├── config/
│   ├── capture.yaml                  Harvesting universe (48 depth / 328 feed ids). §7.4 — capture is closed
│   ├── instruments.yaml              Quoting-study scope; min_days_to_expiry: 3 (pre-registered 2026-08-23)
│   ├── costs.yaml                    Statutory stack + broker profiles; STT verified 2026-09-30. §12
│   ├── market_calendar.yaml          Session windows; empty holiday list (moot now). §8.3
│   └── frozen/
│       ├── protocol.yaml             Classic develop/holdout split + inclusion rule (sha256 0e865025…). §21.3
│       ├── mm_v1.yaml                The frozen classic strategy (sha256 4f144314…). §20
│       ├── protocol_rv.yaml     (rv) RV develop/validation split + strategy #1 go/no-go (sha256 9b070f78…). §23.2
│       └── protocol_rv_patient.yaml (rv) Strategy #2 go/no-go. §23.4
│
├── src/bnfmm/
│   ├── data/                         CAPTURE + QA
│   │   ├── protocol.py          849  Both wire decoders (75)
│   │   ├── universe.py          797  Capture universe vs connection caps (60)
│   │   ├── store.py             717  Parquet store, two clocks, read_capture (44)
│   │   ├── rawlog.py            700  Append-only raw byte log, read_preamble (52)
│   │   ├── channels.py          668  Async capture channels (0 — §13.2)
│   │   ├── instruments.py       399  Instrument master parsing (37)
│   │   ├── qa.py                290  Data-quality checks: coverage, gaps, book/tape sanity, presence (16). §21.1
│   │   ├── chain.py             272  Option-chain REST parsing (0 — §13.2)
│   │   ├── reconnect.py         271  Reconnect policy (43)
│   │   └── auth.py              206  Credentials, TOTP, URL redaction (31)
│   ├── book/
│   │   ├── reconstruct.py       281  Depth rows → BookSeries; align() LOCF (0 direct — §13.2)
│   │   ├── tape.py              247  Trade tape from cumulative volume; running-max rule (8). §21.1
│   │   └── merge.py             130  Causal depth + quote-feed merge; load_quote_ladders (5). §22.3
│   ├── fairvalue/
│   │   ├── parity.py            308  Cross-strike parity forward (0 direct — §13.2)
│   │   ├── microprice.py        105  Microprice, imbalance, depth-weighted micro (0 direct)
│   │   ├── black76.py            95  Price, delta, vega, implied vol — risk only (11). §21.2
│   │   └── smile.py         (rv)  78  Leave-one-out weighted IV smile (4). §23.2
│   ├── strategy/
│   │   └── quoter.py            199  Phase 1 single-leg quoter (0 — §13.2). Superseded by sim/portfolio.py
│   ├── sim/
│   │   ├── portfolio.py         630  ★ Phase 2 portfolio market maker + summarise() (10). §21
│   │   ├── backtest.py          454  Phase 1 run_day, huang_stoll, decompose_pnl, Position (9 in test_pnl_decomposition)
│   │   ├── costs.py             418  CostModel, OrderCost, breakeven_ticks (48)
│   │   └── fills/depletion.py   475  Fill model; activation latency; cancel_at (24 in test_fills). §21.2, §22.1
│   ├── analysis/
│   │   ├── holdout.py           195  Frozen-protocol loader, require_access, config log (13). §21.3
│   │   ├── results.py           167  PnlGrid, Tier (28) — not used by Phase 2 (§5.1)
│   │   ├── quote_size.py        110  Brokerage-vs-size arithmetic
│   │   └── report.py             97  Renderer that refuses synthetic headlines
│   └── synthetic/                    Empty — Tier C generator never built
│
├── scripts/
│   ├── mm.py                    472  ★ Phase 2 driver: build day, run portfolio, report, log. §21.4
│   ├── qa_report.py             338  Corpus-wide data QA → reports/data_quality.md. §21.1
│   ├── snapshot_contracts.py    235  Contract sidecars per capture run (manifest or raw-log preamble). §13.11
│   ├── rv_persistence.py   (rv) 188  Strategy #1 go/no-go measurement. §23.3
│   ├── rv_patient.py       (rv) 147  Strategy #2 go/no-go measurement. §23.4
│   ├── backtest.py              448  Phase 1 driver (holdout-guarded since 2026-09-30). §17.10
│   ├── strike_economics.py      143  Per-strike spread vs cost-floor screen. §17.2
│   ├── capture.py               513  Capture driver (capture closed)
│   ├── phase0_recon.py          461  Reproduces the Phase 0 report
│   ├── check_entitlement.py     391  Read-only API probe
│   ├── chain_poll.py            252  Standalone option-chain poller
│   └── refresh_master.py         40  Download the instrument master (do not run without §0.7 item 4)
│
├── tests/                            21 files (22 on rv). Counts per file shown above; total §0.3
├── reports/
│   ├── phase0_instrument_selection.md   Why options, not futures
│   ├── phase1_market_maker_results.md   Phase 1 (negative) result
│   ├── data_integrity.md                Verdict on the corpus (hand-written)
│   ├── data_quality.md                  QA tables (generated by scripts/qa_report.py)
│   ├── phase2_portfolio_market_maker.md Phase 2 result, latency caveat first
│   ├── rv_strategy1_smile_go_no_go.md   (rv) NO-GO write-up
│   ├── rv_strategy2_patient_go_no_go.md (rv) NO-GO write-up
│   ├── config_log.jsonl                 Every configuration ever run (append-only, committed)
│   ├── holdout_log.md                   Every holdout unlock (append-only, committed)
│   ├── mm/   (gitignored, local only)   Per-run JSON summaries and fill dumps (*.npz). §21.5
│   ├── qa/   (gitignored, local only)   Per-day per-instrument QA JSON
│   └── rv/   (gitignored, local only)   go_no_go.json, patient_go_no_go.json
├── docs/kickoff_brief_original.md       The original brief, verbatim
└── data/  (gitignored, local only)      Captured corpus, ~64 GB. §9.5
    ├── reference/api-scrip-master-detailed.csv   Instrument master as of 2026-08-23 (stale on purpose)
    ├── reference/archive/                         Full master copy + BANKNIFTY slice, 2026-08-23
    ├── tier_a/raw/{depth,feed,chain}/date=…/      .bnrl raw byte logs — the primary artifact
    └── tier_a/parquet/{depth,quotes,sessions}/    Derived read path; sessions/ holds manifests + contract sidecars
```

**What a fresh clone does not have:** `data/` and the three gitignored report folders. Without
`data/` no script that reads market data can run; the tests (which use fixtures) and the
committed reports and logs are what a clone can check. The capture corpus is on the original
machine only (`/home/sattu-dealer/Desktop/BNFMM/data`).

### 6.1 Why `config/capture.yaml` and `config/instruments.yaml` both exist

They look redundant and are not. `instruments.yaml` configures the **quoting study** and
deliberately *excludes* the near-expiry regime (`min_days_to_expiry: 3`,
`roll_days_before_expiry: 3`) because the final sessions of a cycle are a pin-risk regime
unrepresentative of normal quoting.

That is exactly wrong for harvesting. On 2026-08-24 the August contract was **1 DTE and was
the thing being recorded.** So `capture.yaml` resolves by *index* into the live expiry list
with no roll filter: `front` follows the calendar and rolls by itself the morning after an
expiry. Two configs because they encode opposite requirements.

The roll has since been observed working unattended: `front` moved from 2026-08-25 to
2026-09-29 on the morning of Wed 2026-08-26 with no config edit.

### 6.2 Why `universe.py` exists separately from `instruments.resolve()`

Same reason at the code level. `instruments.resolve()` applies the study's filters, which
invert the harvesting objective. `universe.py` (797 lines, 60 tests) resolves the capture
set against the documented caps, enforces `required` groups, deduplicates overlapping
strike bands by `InstrumentKey`, applies the drop order, and **fails at resolve time** if a
band does not fit. A band that does not fit must fail at 08:50, not truncate silently at
09:15.

Its **pair-preserving truncation rule** is load-bearing for §10.2: truncation drops
CE and PE *together*, never one leg. A lone CE is useless for put–call parity, and the
cross-strike parity bound is the project's stale-quote detector.

**This rule paid for itself.** With no index spot ever captured (§9.4), put–call parity
across paired strikes became the *only* available source of a forward price, so every quote
in §17 depends on the pairing having survived truncation. Had truncation been allowed to
drop a lone leg, the fair value would have had no anchor at all.

---

## 7. Capture architecture

### 7.1 Three channels, and why each exists

| Channel | Coverage | Resolution | Why it exists |
|---|---|---|---|
| **20-level depth** WS | 48 instruments: near-ATM CE+PE band + 2 futures | every update | **The only source of queue-position evidence.** What the fill simulator consumes. The reason the project exists. |
| **General feed** WS (`REQUEST_FULL`) | 328 instruments: wide CE+PE band, both futures, **both indices** | every update | 5-level book, cumulative volume, OI. The **volume tape bounds fills**; the only channel that can carry indices. |
| **Option chain** REST | **every strike, both expiries** (762 legs / 381 strikes observed) | 0.33 Hz | Coverage the 50-slot depth budget can never reach, **plus an independent cross-check**. |

The third channel is a deliberate redundancy with two payoffs. It is **plain JSON over
REST, so it cannot fail for any reason the binary decoders can** — a total WebSocket
failure still leaves whole-chain top-of-book at 3-second resolution. And when the two
sources disagree, one of them is wrong, which is free QA (§13.3).

**Indices are mandatory, not optional.** The BANKNIFTY spot index has no bid-ask, therefore
no bid-ask bounce, and it is the only high-frequency reference not contaminated by the
wide futures spread. It anchors the entire fair-value estimator (§10.3). It costs 1 slot
out of 5000 and is **unrecoverable if missed today.**

### 7.2 The raw byte log is the primary output; Parquet is secondary

`data/rawlog.py`, 700 lines, 52 tests. Append-only, length-prefixed frames with arrival
wall **and** monotonic timestamps, written to `data/tier_a/raw/` **unconditionally, before
any decoding is attempted.**

This is what made a 15-hour deadline acceptable. The one risk that could not be retired
offline was that the decoders — built against a reading of the SDK — would be wrong on
real bytes. With the raw log, **a decoder bug costs an offline re-run of a script. Without
it, it costs a trading day that does not come back.**

The log is written even when the decoder raises `ProtocolError`. Rotation at 256 MiB
bounds the loss from one corrupt file. `fsync` is **off** deliberately: the threat model is
suspend and `SIGKILL`, both of which a plain flush survives, not power loss. Flushing every
64 records or 1 MiB closes that window at no cost.

Stdlib-only by constraint — the safety net must not depend on `pyarrow` being importable.

### 7.3 The store: one row per frame, two clocks

`data/store.py`, 717 lines, 44 tests. `DECISIONS.md` #16.

- **One row per frame, never one row per book.** Reconstructing a book is a *reader's* job;
  a writer that reconstructs is a writer that can be wrong in a way you cannot undo.
- **`recv_seq`** breaks timestamp ties (#16.2) and is capture-assigned — see §4.4.
- **Both clocks on every row** (#16.3), with `ClockWitness` tracking max skew and span
  disagreement per session. This is what makes §5.5's gap classification possible.
- **A rename per flush** (#16.4): write to a temp file nothing will read, then rename
  atomically. A `SIGKILL` mid-flush leaves the dataset readable.

### 7.4 The universe, with the arithmetic written out

Strike spacing near the money is 100 points, verified against the 2026-08-23 master.

**Depth channel — 48 of 50 slots:**

| Group | Contents | Legs | `required` |
|---|---|---|---|
| `front_future` | August future | 1 | **yes** |
| `next_future` | September future | 1 | **yes** |
| `front_atm_core` | ATM ±4, CE+PE = 9 strikes | 18 | **yes** |
| `front_band` | ATM ±11, CE+PE = 23 strikes, minus the 9 above | 28 | no |
| | **Total** | **48** | 2 spare |

**ATM ±12 would be 25 strikes = 50 legs, which with 2 futures is 52 — over the cap.** That
is the exact slip `universe.py`'s enforcement is designed to catch. At spot ~57,800, ±11
strikes is ±1,100 points = **±1.9%**.

Futures are listed first and marked `required` so a cap can never evict them. `front_band`
is the group the drop order is designed to eat, furthest strike first.

**Feed channel — 328 of 5000:** 2 indices + 2 futures + front ±50 (101 strikes × 2 = 202)
+ next ±30 (61 × 2 = 122).

**Chain poller:** `min_interval_s: 3.5` against a documented 3.0 floor, alternating
front/next. ~7,100 requests over a 6.25 h session = **7% of the 100,000/day quota.**

**3 of 5 WebSocket connections used**, `reserve_connections: 2` — see §4.6 for why the
reserve is not waste.

### 7.5 Keepalive, staleness, and reconnect

```yaml
ping_interval_s: 15.0     # asserted < 40 s in channels.py, not assumed
ping_timeout_s: 20.0
stale_after_s: 45.0       # measured on BYTES RECEIVED, not on pong replies
max_attempts: 40
base_delay_s: 1.0  max_delay_s: 60.0  multiplier: 2.0  jitter: 0.25
```

**Staleness is measured on bytes received, not on pongs.** A library-level ping can keep
answering across a laptop suspend while no market data flows at all — which is
indistinguishable from a dead feed in the *data*, and the data is the deliverable.

Backoff jitter is **multiplicative** and drawn from a `random.Random` seeded at build time,
never the module-level generator, so tests assert exact delays and two channels in one
process do not perturb each other's sequence.

`max_attempts` counts **consecutive attempts without data**, and resets on data, not on
connect — resetting on connect would turn a server that accepts and immediately closes
into a tight loop at the base delay.

**Why `max_attempts` is 40 and not 20.** The 20-level depth feed is a pure delta feed with
no snapshot on subscribe, so it legitimately receives nothing from the 08:58 capture start
until the book starts moving. Each cold cycle costs `stale_after_s` + backoff = 105 s once
the delay saturates, so **20 attempts expire at 09:28** — only twelve minutes after
continuous trading begins. A late open, or a depth feed quiet through the pre-open, would
have killed the depth channel for the entire session while feed and chain kept running to
15:35, and the loss would have surfaced only in the closing summary. 40 attempts pushes the
give-up to **10:03**: about an hour of continuous silence tolerated. Documented disconnect
codes are fatal immediately and are not governed by this count, and the session is bounded
by `--duration` regardless, so a larger budget cannot become an unbounded loop.

The better fix — not counting cold-start staleness against the budget at all — is
deferred to Phase 1c where `tests/test_channels.py` can cover it. It was not worth a
behaviour change to the reconnect path on the morning of the first capture.

### 7.6 A bug worth knowing about, because it was an unconditional hang

`channels.py:_receive` originally did a bare `await ws.recv()` whenever `stale_after_s`
was `None`, which made the deadline check at the top of the loop **unreachable**. On a
market that has not opened, `recv()` never returns, so the loop could never notice the
deadline it was given. `--connect-test` (which disables the watchdog) walked straight into
it and hung indefinitely.

The fix bounds every `recv` by `min(stale_after_s, time_remaining_to_deadline)`:

```python
budget = self.stale_after_s
if deadline is not None:
    remaining_s = (deadline - self.mono_ns()) / 1e9
    if remaining_s <= 0:
        raise _Stop
    budget = remaining_s if budget is None else min(budget, remaining_s)
```

and disambiguates the two unrelated events that raise the same `asyncio.TimeoutError` — a
stale feed and a finished session — by asking the clock which it was. They want opposite
handling: reconnect versus stop. This also removed a latent flaw on the real session path,
not just the test path.

### 7.7 Per-day config edits, written down before they are needed

**Tue 2026-08-25 (expiry day, 0 DTE).** Split the depth budget across the roll instead of
spending it all on the expiring contract: September becomes front month at the close, and
how liquidity migrates across that boundary is only observable with depth on **both sides
of it.** Set `front_band.strike_band: 7` (15 strikes × 2 = 30) and add a `next_band` group
with `strike_band: 4` (9 × 2 = 18) — 48 legs plus 2 futures is exactly 50. `required` on
the two futures and the ATM core means a miscount raises rather than truncates.

**Wed 2026-08-26 onward.** **No edit needed.** The August rows drop out of the live expiry
list on their own, `front` becomes September and `next` becomes October. Optionally scale
depth to two connections (100 instruments, ±23 strikes = 94 legs + 2 futures = 96) once
day one has proved the pipeline; that needs a second `depth_20` channel entry and
`reserve_connections: 1`, which the resolver will check.

---

## 8. `main.py` — the single entry point

915 lines, 33 tests. Two modes, chosen interactively or by `--mode`.

```bash
.venv/bin/python main.py                          # interactive: asks which mode
.venv/bin/python main.py --mode record            # capture a live session
.venv/bin/python main.py --mode analyse           # work on recorded data
```

With no TTY **and** no `--mode`, it exits `EXIT_FAILED` rather than guessing. Exit codes:
`0` OK, `1` DEGRADED, `2` FAILED.

### 8.1 Record mode, step by step

1. **Plan the window** (`plan_window`) — §8.2.
2. **Warn** about anything the operator must know *before* the wait begins: a partial
   window, an unverified holiday list, and — if the wait exceeds 30 minutes and the process
   does not appear to be under `systemd-inhibit` — that the laptop may suspend.
3. **Sleep** to `start − preflight_lead_s` (600 s).
4. **Preflight**, cheapest check first: load `.env` and authenticate (warn if the token
   looks expired) → instrument master, **auto-refreshing above 12 h** because `capture.py`
   *raises* above 24 h rather than warning → disk free, WARN at ≤20 GB →
   `capture.main(["--dry-run"])`, which echoes the fully resolved universe before anything
   connects. Any failure stops the run unless `--force`.
5. **Sleep** to `start`.
6. **Capture** — calls `capture.main()` **in-process**, not via a subshell, so a traceback
   is a traceback and not an exit code.
7. **Reconcile** (§8.4), inside a `try/except` so that a reconciliation failure can never
   mask a good capture.

### 8.2 The window planner, and why waiting is a feature

Three cases, in test order:

| Now | Behaviour |
|---|---|
| inside today's window | start **immediately**, flag `partial=True` |
| before today's window | **wait** for today's open |
| after it, or a non-trading day | forward-scan up to 21 days for the next trading day |

If no trading day is found in 21 days it **raises** rather than looping.

`Window.duration_s(now)` runs from **now**, not from the nominal start, so a mid-session
launch is never handed an already-expired deadline. A `partial` window produces an explicit
warning that "the pre-open call and the opening print are already gone and cannot be
recovered."

Waiting is the point, not a convenience: *a trading session happens once and cannot be
re-run.* Launch this the night before, or at 07:00, and the open is covered without anyone
being present.

**`sleep_until` is deliberately not one long `time.sleep`.** On Linux `time.sleep` is
implemented on `CLOCK_MONOTONIC`, which **does not advance while the machine is
suspended** — so a single long sleep wakes late by however long the lid was shut.
`sleep_until` re-derives the remaining time from the **wall clock** every 30 seconds, so
suspend costs nothing on the waiting side. The countdown announcement cadence tightens as
the target nears: 15 min out at >1 h, 5 min at >10 min, 1 min at >2 min, then 15 s.

**The converse is a known, bounded exposure.** `--duration` becomes a `CLOCK_MONOTONIC`
deadline inside `channels.py` (correctly — an NTP step must not end a session early), so a
mid-session suspend pushes the stop past 15:35 in wall-clock terms. This self-limits: with
no data flowing, staleness → reconnect → 40 attempts with backoff capped at 60 s means the
channels give up roughly 70 minutes after the market goes silent. Documented in §13.6.

### 8.3 `config/market_calendar.yaml`

```yaml
holidays: []                 # EMPTY ON PURPOSE — see below
sessions:
  preopen_start:     "09:00"   # pre-open order entry begins
  continuous_start:  "09:15"
  continuous_end:    "15:30"
  capture_start:     "08:58"   # capture starts BEFORE the pre-open call
  capture_stop:      "15:35"
preflight_lead_s: 600
```

**Capture starts at 08:58, before the pre-open call, deliberately.** Order entry from 09:00
and the 09:08–09:12 randomised match produce the opening print, and the equilibrium-price
discovery in that window is a **distinct microstructure regime that cannot be recovered
afterwards.** It stops at 15:35 so the closing print and post-close settlement traffic land
inside the recording rather than outside it.

**The holiday list is empty and this is a known gap, not an oversight.** The 2026 NSE
holiday list could not be fetched from this machine — `WebSearch` is unavailable for this
model and nseindia.com refuses automated requests. A *guessed* list would be worse than
none, because a wrong list is trusted. So it ships empty, with a `holidays_verified` flag
that is `False`, and `main.py` **says so out loud every time it computes a wait.** A test
asserts `holidays_verified == bool(holidays)` so the flag can never become `True` by
accident.

Cost of the gap: a public holiday looks like a trading day, so a scheduled run wakes,
connects, and receives nothing. **That is a wasted morning, not corrupted data** — the
manifest records zero rows and reconciliation reports an empty capture, which is
unambiguous. Weekends are handled by rule and need no list.

**To close it:** paste dates from
`https://www.nseindia.com/resources/exchange-communication-holidays` as plain
`YYYY-MM-DD` entries. Only full closures belong there; a Muhurat session is not a closure.

### 8.4 Reconciliation

After every capture, each channel's raw parts are replayed and **re-decoded from scratch**,
reporting `unknown_codes`, `length_mismatches`, `padding_anomalies` and `unsorted_sides`,
then compared against the Parquet row counts and `skipped_frames` from the manifests.

It deliberately does **not** report `partial_carries` — a mid-frame chunk boundary is
normal on a byte stream, and flagging it would train the operator to ignore the report.

### 8.5 Analyse mode

1. `discover()` lists recorded sessions; pick one interactively or with `--session` /
   `--date`.
2. Reconcile it (§8.4).
3. `describe_contents` — row counts by channel and dataset.
4. Print the **pipeline status table**.

`discover` groups by the **bare 15-char session id, not by date directory**, because a
writer that captured no rows has no clock to date itself by and falls back to
`1970-01-01`. Grouping by directory would split one real session across two dates *and*
invent a 1970 session that never happened.

Session ids are `YYYYMMDD-HHMMSS` when auto-generated, but `--session-id` accepts any
string, so nothing asserts that format.

The pipeline table's `available` column is **probed by importing, never declared in a
table**, so the listing cannot go stale:

```python
@property
def available(self) -> bool:
    try:
        mod = importlib.import_module(self.module)
    except Exception:
        return False
    return self.attr is None or hasattr(mod, self.attr)
```

| Stage | Module | Phase | Status today |
|---|---|---|---|
| `read` | `bnfmm.data.store.read_capture` | 1a | **available** |
| `qa` | `bnfmm.data.qa` | 1c | missing |
| `book` | `bnfmm.book` | 2 | missing |
| `fairvalue` | `bnfmm.fairvalue` | 2 | missing |
| `fills` | `bnfmm.sim.fills.QueueFill` | 3 | missing |
| `quoter` | `bnfmm.strategy` | 4 | missing |
| `metrics` | `bnfmm.analysis.metrics` | 5 | missing |

When Phase 3 lands `QueueFill`, the row flips to available with **no edit to `main.py`**.
Until then analyse mode returns `EXIT_DEGRADED` and says so plainly:

> This is the honest answer rather than a stub that returns numbers. The data captured
> today is not blocked by it — capture and analysis were deliberately decoupled precisely
> because a trading session happens once and the simulator can be written any time.

---

## 9. What has been verified live, and the gap that was missed

§9.1 and §9.2 were run against the live API on the night of 2026-08-23/24, before any
market data existed. §9.3–§9.5 record what nine real sessions then showed. **These are
measurements, not expectations.**

### 9.1 Verified working (pre-capture, 2026-08-23/24)

| Check | Result |
|---|---|
| Auth from `.env` (Path B) | works; `Session.__repr__` redacts the token; no token anywhere under `data/` |
| `/v2/profile` | `Data APIs: Active`, `dataValidity 2026-09-22 19:28:31 IST` |
| `/optionchain` | **762 legs across 381 strikes**, underlying last **57,761.95** |
| `/optionchain/expirylist` | 6 expiries, vendor front = **2026-08-25**, matching the local master |
| `/charts/intraday` | answers |
| **Decoder on real wire bytes** | **652 frames decoded live with zero errors** |
| **Offline re-decode from the raw log** | **exactly 652 frames**, `{'Snapshot': 326, 'OpenInterest': 326}`, `unknown_codes={}`, `length_mismatches={}`, `padding_anomalies=0`, `unsorted_sides=0` |
| Raw log replay | 652 records / 56,724 bytes, byte-for-byte, matching the live summary |
| Read path | `read_capture` → `snapshots()` → typed `Snapshot`: security 58067 (BANKNIFTY AUG FUT), ltp 57,705.0, OI 1,864,920, volume 791,460 |
| Staleness watchdog | fired and recovered correctly: `[feed] STALE: data stopped for 45s` → `reconnecting in 0.8s (attempt 1)` → resubscribed → data resumed |
| Full record-mode rehearsal | countdown → preflight → capture → 1,304 chunks / 113,448 bytes / 1,304 frames / 652 rows across 2 connects; chain 18 polls / 4,393,956 bytes; correct `DEGRADED` for `depth: received no data at all`; reconciliation reported a clean re-decode |

**`skipped_frames.OpenInterest = 326` is benign and understood.** All 326 rows are
`msg_code=8` (`FEED_FULL`) with `open_interest` non-null 326/326 — the standalone 12-byte
OI frames are redundant with the OI already embedded in every `Full` packet.

### 9.2 Master pre-staged

Forced fresh on 2026-08-24: **36.1 MB, 212,736 rows**, so the first capture morning did not
depend on a download. Preflight auto-refreshes above 12 h. **The master is now stale** and
that is what §13.9's failing test is telling you.

### 9.3 The depth decoder — risk retired on real bytes

The previous revision of this document named this "the one honest caveat": the 20-level
depth feed had produced zero bytes, its decoder was verified only against
`tests/fixtures/packets.py`, and the raw byte log existed precisely so a decoder bug would
cost an offline re-run rather than a trading day.

**It decoded correctly.** Nine sessions later the depth channel has produced ~19 GB of raw
bytes and millions of frames. A representative session (2026-09-01) reconciles as:

```
3,055,062 frames from 1,014,280,584 bytes in 32,976 chunks;
30 padding anomalies; 14 unsorted sides
```

30 padding anomalies and 14 unsorted sides in 3.06 million frames is **1 per 100,000** —
real exchange quirks surfacing at a trivial rate, not a decoder defect. They are counted
and reported rather than suppressed (`decode_clean: false` in the manifest), which is the
intended behaviour: the flag says "look at this", not "the data is bad".

What the bytes also revealed, and what nothing offline could have predicted, is the feed's
**~200 ms fixed snapshot cadence** (median 201 ms, p05 157 ms, p95 442 ms; payload sizes
all multiples of 332 bytes). It is not an event-driven delta stream. That single measured
fact is what makes true queue-position modelling unsupportable and constrains the entire
fill simulator — see `sim/fills/depletion.py`'s module docstring, which is the best single
statement of it in the repo.

### 9.4 ⚠ The gap that was missed: no index spot was ever captured

**This is the one thing §9 got wrong, and it was not found until Phase 2.**

`config/capture.yaml` subscribes security ids **25 (NIFTY BANK)** and **13 (NIFTY 50)** on
the general feed, and §10.2 layer 3 declares them "mandatory". They were subscribed with
`CAPTURE_REQUEST_CODE = REQUEST_FULL` (21) at `src/bnfmm/data/protocol.py:204`. **Dhan does
not serve Full packets for the `IDX_I` segment** — an index has no book and no open
interest — so the subscriptions were **silently ignored**. No error, no warning, no
disconnect.

Measured consequence: all 7,356,944 quote rows on 2026-08-24 are msg code 8 on segment 2
(`NSE_FNO`), and **zero rows exist for either index on any captured day**. Confirmed across
all nine sessions.

Three things follow, and a colleague must understand all three:

1. **§10.2 layer 3 (`basis.py`) is unbuildable on this data.** `F = S + basis` has no `S`.
   That layer was never written and cannot be, on the current corpus.
2. **Put–call parity became the only forward.** Not one estimator among four, as §10
   planned — the *sole* source. Everything in §17 rests on it. That is why `parity.py`'s
   docstring opens by enumerating why the three obvious alternatives are unavailable.
3. **This is a capture bug, not a design choice**, and the report says so in those words
   (`reports/phase1_market_maker_results.md` §7.4). Do not let it be described as a
   methodological preference.

**Whether to fix it is an open decision, not a clear yes.** Fixing means either sending
request code 15 (Ticker) or 17 (Quote) for `IDX_I` ids while keeping Full for FNO ids —
i.e. per-segment request codes, which `subscribe_message` does not currently support — or
dropping the index subscriptions and deleting the §10.2 layer-3 claim. The cost of the fix
is a change to the capture path that is running successfully every morning, three weeks
into a 4½-week entitlement window. **The risk of touching working capture may exceed the
value of the spot series**, particularly since parity is measurably adequate. Decide
deliberately; do not fix it reflexively.

**How this got missed** is worth recording, because the same shape of bug can recur: every
channel reported healthy, the row counts were large, and reconciliation was clean. Nothing
was broken — a *subset* of the subscription was ignored, and no code asserted that every
subscribed id produced rows. A `required`-ids post-capture assertion would have caught it on
day one. That check still does not exist.

### 9.5 The captured corpus

⚙ **Final, 2026-10-02. Capture is closed; this table will not change.** Coverage is of the
09:15–15:30 continuous session, from `reports/data_quality.md` (gaps > 30 s end a segment).

| Date | Series quoted (DTE) | Runs | Depth coverage | Status for quoting studies | Notes |
|---|---|---|---|---|---|
| Mon 2026-08-24 | Aug (1) | 3 | 96.8% | excluded by `min_days_to_expiry: 3` | Open lost: capture began 09:27:01. Used in Phase 1 (§17) |
| Tue 2026-08-25 | Aug (0, expiry) | 2 | 99.1% | excluded by `min_days_to_expiry` | 4 gaps 35–88 s in first 25 min. Used in Phase 1 |
| Wed 2026-08-26 | Sep (34) | 3 | 19.1% | **excluded** (coverage rule) | Token expired; feed dead from 10:44 (§13.12) |
| Thu 2026-08-27 | Sep (33) | 2 | 98.8% | classic develop; RV develop | One 271 s gap at 13:20 |
| Fri 2026-08-28 | Sep (32) | 1 | 100.0% | classic develop; RV develop | Cleanest day |
| Mon 2026-08-31 | Sep (29) | 1 | 99.8% | classic holdout; RV validation | One 34 s gap |
| Tue 2026-09-01 | Sep (28) | 2 | 86.4% | classic holdout; RV validation | 51-min gap 12:32–13:23 (restart) |
| Wed 2026-09-02 | Sep (27) | 1 | 48.3% | classic holdout; RV validation | Capture ended 12:16 (half day) |
| Thu 2026-09-03 | Sep (26) | 2 | 94.9% | classic holdout; RV validation | 16-min gap 10:06–10:22 (restart) |
| Fri 2026-09-04 | Sep (25) | 2 | 77.5% | classic holdout; RV validation | Capture ended 14:07; duplicate-packet storm 09:30–11:30 and a stale-replay burst ~12:23 on the quote feed (both handled, §21.1) |

Every session has **48 depth instruments** (46 front-series options = ATM ±11 strikes, plus the
two futures) and **326 quote-feed instruments** (front ±50 and next ±30 strikes, both futures;
the 2 index ids never produced rows, §9.4). Depth Parquet is ~320 MB on a full day; raw byte logs
are the bulk of the 64 GB.

**What exists beyond the front-month depth band:** the next-month series (Sep on Aug 24/25 at
35–36 DTE; Oct on Aug 27 → Sep 4 at 53–61 DTE) and front-month strikes beyond ±11 exist **only
on the quote feed** (5-level book, event-driven, ~1.6 packets/s on liquid legs). The Oct series
is thin (~470k units/day across 61 strikes, ~1% of the front month). See §22.3 for why these were
not used.

Every capture run has an immutable contract sidecar (`*-contracts.json` next to its manifest in
`parquet/sessions/date=…/`), so every security id on disk is labelled with its strike, expiry and
type even after the vendor prunes the master (§13.11).

## 10. Fair value — the mathematics, and why not LTP

**This is a headline deliverable, not an implementation detail.** The fair-value mark
determines the skew; the skew determines adverse selection; and the attribution's split
between spread capture and adverse selection is *entirely* a function of the mark. It gets
its own module tree (`src/bnfmm/fairvalue/`, Phase 2) and its own measured comparison.

This section is the direct answer to a question the user posed explicitly as CV-facing:
*"the bid ask on underlying future is huge, so when a trade happens finally via crossing
the spread, the LTP doesn't give the proper picture."* That is exactly right, and the
answer has four layers.

### 10.1 The problem, quantified in the units that matter

BANKNIFTY futures tick ₹0.20; options tick ₹0.05. So a futures spread must be measured
**in option ticks, scaled by delta**. A 5-tick (₹1.00) futures spread through a
0.5-delta option is ±₹0.25 = **5 option ticks of noise**, against a total edge of 47–79
ticks. Using the futures **mid**, that is 6–10% of the edge as pure noise — and it is
*systematic*, not zero-mean, whenever the futures book is persistently one-sided.

Using **LTP** is strictly worse. Under **Roll's model of bid-ask bounce**:

$$\text{LTP}_t = M_t + \frac{s}{2}\,q_t, \qquad q_t = \pm 1 \text{ by aggressor side}$$

so fair value oscillates ₹0.50 peak-to-peak on alternating prints with *no change in the
underlying market at all*, and the quoter chases that noise and pays the spread to do it.
Worse, LTP carries the **aggressor's information**: it is biased *toward* the direction in
which you are about to be adversely selected.

### 10.2 The four layers — two built, two not

**Status first, because the plan and the code diverged.**

| Layer | Planned | Status |
|---|---|---|
| 1. `microprice.py` | size-weighted mid, depth-weighted variant | ✅ **Built**, 105 lines |
| 2. `parity.py` | cross-strike implied forward + bounds | ✅ **Built**, 308 lines |
| 3. `basis.py` | `F = S + basis`, index-driven | ❌ **Never built. Unbuildable** — no `S` (§9.4) |
| 4. `smile.py` + `greeks.py` | IV-space fit, Black-76 greeks | ❌ **Never built. Deliberately skipped** — see below |

**What the quoter actually uses** is the *parity-anchored microprice*, described in
`strategy/quoter.py:theo`, which is neither layer 1 nor layer 2 alone:

```
value = own_microprice + (parity_value − own_microprice)
```

i.e. the option's own book microprice, corrected by the amount its strike's call/put pair
disagrees with the cross-strike consensus forward. The correction is model-free (it is
parity again) so the quote stays anchored on the option's own liquidity while being pulled
toward the ladder's consensus when its own book drifts.

**Why layer 4 was skipped rather than deferred**, quoting `quoter.py`'s reasoning: deriving
an option value from a forward requires a volatility model, and any such model becomes the
dominant source of error at a 200 ms cadence. A mis-specified smile produces systematic,
one-sided quoting **that looks exactly like alpha until it is not.** Given a two-day
dataset and the §17 result — where the one profitable configuration turned out to be a
directional bet — adding a model capable of manufacturing precisely that failure mode was
the wrong risk to take first. The parity-anchored route needs no volatility at all.

That is a defensible engineering decision, but note what it costs: **§10.3's estimator
comparison is now answerable for only two of the five candidates.**

---

**1. `microprice.py` — quote-derived, never trade-derived.** ✅ Built.

Size-weighted mid, which leans toward the thin side:

$$M^{\text{wtd}} = \frac{P_{\text{bid}} Q_{\text{ask}} + P_{\text{ask}} Q_{\text{bid}}}{Q_{\text{bid}} + Q_{\text{ask}}}$$

Extended to a depth-weighted average over the first *k* of the 20 levels, so a single-lot
touch quote cannot drag the mark. LTP and plain mid are kept as **baselines**, precisely so
the comparison can be published.

*Measured justification, now that real data exists:* on the front-month future on
2026-08-24 the median spread was **41 ticks (₹8.20)** and 2.2% of snapshots were locked,
crossed or one-sided. Half the Roll bounce is over ₹4 on a ₹57,440 underlying. The
theoretical objection to LTP turned out to understate the practical one.

**2. `parity.py` — the forward from put–call parity across every strike.** ✅ Built.

BANKNIFTY options are European and cash-settled, so parity holds exactly:

$$C(K) - P(K) = (F - K)\,D \quad\Longrightarrow\quad \hat F(K) = K + \frac{C(K) - P(K)}{D}$$

where $D = e^{-rT}$. Each strike gives an independent noisy estimate of one forward, and
inverse-variance weighting cuts noise by roughly $\sqrt N$. *The wide-spread future is one
observation; the chain is many.*

Report **bounds**, not just a point estimate:

$$F \in \left[\,K + \frac{C_{\text{bid}} - P_{\text{ask}}}{D},\; K + \frac{C_{\text{ask}} - P_{\text{bid}}}{D}\,\right]$$

intersected across strikes; an empty intersection detects stale or crossed quotes. This is
why truncation must preserve CE/PE pairs (§6.2): a lone leg cannot contribute a bound.

*Three corrections the implementation made to this plan:*

- **"~50 strikes" is wrong; it is 23 per expiry.** The captured depth band is ±11 strikes
  around the ATM plus the ATM itself. $\sqrt{23}$, not $\sqrt{50}$.
- **The point estimate is precision-weighted by $1/(\text{spread}_C + \text{spread}_P)^2$**
  and then *clipped into the intersection*, so the bound constrains the estimate rather than
  merely reporting alongside it.
- **An empty intersection is almost never arbitrage — it is staleness.** One strike's
  snapshot is older than another's at a 200 ms cadence, so they describe different instants.
  Those samples are flagged `arb_violation` and fall back to the unclipped weighted average.
  They are never dropped, because **their rate is a direct measure of cross-strike staleness**
  and that measurement belongs in the writeup.

**3. `basis.py` — separate the fast part from the slow part.** ❌ **Never built. Cannot be,
on this data — see §9.4.** The section below is preserved as the design it would follow if
an index series were ever captured, and as the record of what was lost.

$$F = S + \text{basis}$$

The basis is financing and dividend expectation. It moves on a timescale of **hours**,
while quote noise is per-tick. So:

1. Take the **BANKNIFTY spot index** — an average of 12 constituent LTPs, with **no
   bid-ask and therefore no bounce**.
2. Filter the parity-implied forward with an EWMA, or a 1-D Kalman filter under a slow
   random-walk prior on the basis.
3. Reconstruct at full tick frequency:

$$\hat F_t = S_t + \text{basis}^{\text{filtered}}_t$$

You get the **speed** of the index with the **level** of the options market, and never
touch the future's LTP. The index's own defect — non-synchronous constituent trading,
hence lag — is exactly why it supplies **increments, not the level.**

~~This is why the indices are mandatory in today's universe (§7.1).~~ **They were declared
mandatory and were then silently not captured.** §9.4. This layer is the direct casualty.

**4. `smile.py` — price in IV space off a fitted arbitrage-free curve, not per strike.**
❌ **Never built** — deliberately skipped, see the status table above.

Convert each strike's microprice to Black-76 IV using $\hat F$ and $T$; fit a smooth curve
in log-moneyness $\log(K/\hat F)$ — SVI, or a butterfly-constrained polynomial for a single
expiry; read fair IV at the quoted strike off the **fit**; convert back to price.

Three properties, each independently valuable:

- It pools the strike ladder into every fair value, so one strike's book going momentarily
  one-sided does not move the mark.
- Enforcing butterfly and vertical no-arbitrage means **the mark cannot be picked off by a
  static combination** — a concrete, *testable* correctness property, not a vibe.
- **The residual (market IV − fitted IV) is itself a signal**: local rich/cheapness,
  mean-reverting, and exactly what a market maker should skew against. Fair value stops
  being only an input and becomes alpha.

`greeks.py` would supply Black-76 delta/gamma/theta/vega, tested against published values.

**Expiry-day caveat, which Aug 25 forced.** As $T \to 0$, ATM IV explodes in units of
$1/\sqrt T$ and the fit destabilises. On expiry day, work in **price space** with an
intrinsic + time-value decomposition, or floor $T$. *Note that the parity-anchored route
sidesteps this entirely* — it never enters IV space — which is part of why it was chosen
for a dataset whose second day is 0 DTE.

**If someone builds this later**, the honest framing is that it is a *second* estimator to
compare against the parity anchor under §10.3, not a replacement. The §17 result would have
to be re-run under both and shown not to flip.

### 10.3 Measure the estimator; do not assert it

Score all five candidates — LTP, plain mid, weighted mid, microprice, parity+smile —
**out of sample**, on three axes:

1. One-step-ahead error $\mathbb{E}[(FV_t - M_{t+\tau})^2]$ at $\tau \in \{1\text{s}, 5\text{s}, 30\text{s}\}$
2. Stability of the attribution split
3. Sign of the short-horizon markout

Publish the headline under the **two best marks** and show the conclusion does not flip.

**The invariant to lean on:** total realized PnL over a window that starts and ends flat is
**mark-independent**. Only the *split* is estimator-dependent. That is already a planned
unit test and it is what keeps the headline honest.

#### Status: partially done, and the conclusion did *not* flip

Two of the five candidates exist (§10.2), and `scripts/backtest.py --anchor {micro,parity}`
runs the headline under both. On the two captured days:

| Day | micro anchor, member net | parity anchor, member net |
|---|---|---|
| 2026-08-24 | −₹134,213 | **+₹92,433** |
| 2026-08-25 | −₹382,286 | −₹207,712 |

The *level* is strongly estimator-dependent — parity is better on both days, by a lot — but
**the conclusion is not**: the strategy loses money under both marks once the Aug 24 parity
cell is decomposed and found to be directional (§17.4). That is exactly the robustness
check §10.3 asks for, and it passed in the sense that matters.

**Three of the five candidates remain unscored** (LTP, plain mid, and parity+smile), and
the three-axis scoring above — one-step-ahead error, attribution stability, markout sign —
**has not been run for any candidate.** The comparison is currently "we ran the headline
under both marks we have", which is weaker than what this section promises. The
mark-independence unit test is also still unwritten.

### 10.4 Delta management

Delta is managed by **quoting CE and PE at one strike and skewing inventory**, not by a
hedging leg (`DECISIONS.md` #9, Finding 3). A futures hedge would reintroduce the futures
cost base — 163 ticks of breakeven, or **327 option ticks of edge** to overcome — which is
the entire reason futures were rejected as the quoted instrument.

The measured front-future book independently confirms this: median spread **41 ticks**, 2.2%
of snapshots crossed, one-sided, or carrying outright garbage (a ₹0.60 ask against a ₹57,780
bid). Hedging into that book would cost more than the edge being captured.

**What §17 revealed about this choice.** Inventory skew is a *weaker* risk control than a
hedge, and the failure mode it permits is now measured rather than hypothesised: with skew
at its default 0.5 ticks/lot, the quoter sat pinned at its short-call cap 45% of the day and
its long-put cap 40% — a synthetic short forward — and the resulting directional PnL was 58%
of gross. **When inventory skew is the only control, the position limit becomes the
strategy.** §11.6 anticipated this exactly ("fraction of time pinned at the limit — high ⇒
the limit is the strategy"); §17.4 is that diagnostic firing.

This does not overturn the decision — hedging into a 41-tick book is still worse — but a
colleague should understand the trade-off is now quantified, not assumed.

---

## 11. Evaluation — what "merit" means, and the diagnostic that says what to fix

This section answers the user's second explicit question: *"if the market maker pitfalls,
makes losses or underperforms, is there any scope for improving it? What other quantitative
analysis parameters do we introduce to quantify its merit?"*

A loss is not a dead end — but **the fix depends entirely on which failure it is**, and the
Huang–Stoll decomposition is the diagnostic that tells you which.

### 11.1 The decomposition

For a fill at price $P_{\text{fill}}$ with direction $d = +1$ buy / $-1$ sell:

$$\text{effective spread} = 2d\,(P_{\text{fill}} - M_t)$$
$$\text{realized spread} = 2d\,(P_{\text{fill}} - M_{t+\tau})$$
$$\text{adverse selection} = \text{effective} - \text{realized}$$

All three at $\tau \in \{1\text{s}, 5\text{s}, 30\text{s}\}$. This maps exactly onto the
planned attribution identity, whose sum-to-total-PnL is a unit test.

### 11.2 The three-branch diagnostic tree

| What the decomposition shows | Diagnosis | The fix |
|---|---|---|
| effective spread **< cost floor** | **Structurally dead at the touch.** Quoting cannot pay, at any parameter setting. | Selective quoting, size, fewer round trips. **The finding *is* the result.** |
| effective **> cost** but realized **< 0** | **Adverse selection is eating the edge.** | OFI-driven pull/widen, cancel-reprice policy, better fair value (§10) |
| realized **> 0** but total PnL **< 0** | **Inventory or fees.** | Sizing, risk aversion $\gamma$, netting CE against PE, unwind policy |

#### The measured answer: branches 1 and 2 simultaneously, on both days

This tree was written before any data existed. It has now been run (§17) and the result
landed on **the first two branches at once**, which the tree did not anticipate as a
combination:

**Branch 1 fires.** At the 6-tick half-spread the measured effective half-spread on Aug 24
was **+0.3065 per unit** — about 6.1 ticks of premium — against a **member round-trip floor
of 9.65 ticks** and a **Dhan floor of 41.12 ticks at one lot**. Doubling the effective
half-spread and comparing like for like, the member profile is roughly at breakeven and the
Dhan profile is nowhere near it. The arithmetic screen in §17.2 reaches the same conclusion
without simulating anything: the strikes with headroom have no flow and the strikes with
flow have a headroom-to-floor ratio of only **1.2–1.4×**.

**Branch 2 fires too.** At every half-spread tested, **adverse selection exceeded the
effective spread captured** (at 6 ticks: 0.3349 against 0.3065). The realised half-spread
is therefore negative throughout. The horizon curve says this is *pick-off*, not inventory
risk — see §17.5.

**Branch 3 never gets a chance**, because realised spread is never positive.

The consequence for §11.3's levers is severe and worth stating plainly: **widening does not
escape branch 2.** The sweep shows adverse selection rising in lockstep with the
half-spread (0.2059 → 0.3349 → 0.9536 → 1.4925 as ticks go 2 → 6 → 16 → 24), so quoting
wider trades less at *worse* selection. The lever that the tree implicitly promised — "quote
wider until effective > cost" — is measurably not available on this data.

### 11.3 Improvement levers, ordered by expected impact

**If structurally dead:** quote **selectively** rather than continuously. A market maker is
not obliged to quote; quote only where expected edge > cost + adverse selection. This shows
up as **PnL-per-round-trip rising as fill count falls** — a legible, falsifiable signature.

**Then size, because brokerage is fixed per *order*.** ₹20 over 1 lot is ₹0.67/unit; over
the 601-unit freeze cap it is ₹0.033/unit — a **20× cut** in that component. Per-unit
breakeven falls **₹3.94 → ₹2.45** from 1 to 20 lots. This is *arithmetic, not tuning*,
already quantified in `analysis/quote_size.py`.

Note that **Avellaneda–Stoikov derives an optimal spread and assumes no fixed per-trade
cost**, so it says nothing about this trade-off. Quantifying it is a small original
extension and belongs in the writeup as one.

**Then** fewer round trips (net CE against PE, unwind into incoming flow rather than
crossing), and quoting off the touch where the spread is wide enough to clear cost.

**If viable but underperforming**, in order: adverse-selection reduction → fair-value
quality → queue positioning (which dominates fill quality at the touch, and measuring it
is why this project exists) → inventory policy → regime selection.

### 11.4 The guardrail matters more than any lever

Every one of those is a parameter, and tuning on the evaluation data is overfitting. So:

- Walk-forward by **calendar day** (§5.5).
- **Report how many configurations were tried.**
- Report the in-sample → out-of-sample gap.
- Benchmark against **do-nothing** and **naive-always-quote**, so the claim is the
  *increment*, not the level.
- `data/holdout.py` **refuses** to read holdout dates without `--unlock-holdout`, and
  appends every unlock to `reports/holdout_log.md`. Enforcement in the loader, not in a
  convention.
- `config/frozen/` holds parameter manifests stamped with git SHA and freeze timestamp, so
  any run after that timestamp is out-of-sample **by construction and checkable by a third
  party**.

"Sharpe 2.4, degraded from 3.1 in-sample across 40 configurations tried" is a credible
sentence. "Sharpe 2.4" alone is not.

### 11.5 ⚠ Sharpe on 21 sessions — a correction to the earlier plan

An earlier draft of the plan asserted `SE(SR) ≈ √((1+SR²/2)/N) ≈ 0.22` at N=21, and
concluded "SR 2.0 is really ≈[1.1, 2.9]". **That is wrong, and wrong in the dangerous
direction — it understates the uncertainty by a factor of √252 ≈ 16.**

The formula requires SR and N in the **same period units**. 0.22 is the SE of the *daily*
Sharpe at N=21. Applying it to an *annualised* SR of 2.0 mixes units. Done correctly, with
annualised SR = 2.0 (so daily SR = $2.0/\sqrt{252} = 0.126$) and N = 21 daily observations:

| N sessions | SE(daily) | SE(annualised) | 95% CI on annualised SR = 2.0 |
|---|---|---|---|
| **21** | 0.219 | **3.48** | **[−4.82, +8.82]** |
| 42 | 0.155 | 2.46 | [−2.82, +6.82] |
| 63 | 0.127 | 2.01 | [−1.94, +5.94] |
| 252 | 0.063 | 1.00 | [+0.03, +3.97] |

**Twenty-one sessions cannot distinguish an annualised Sharpe of 2 from zero.** It takes
**~244 sessions (about one year)** for the 95% CI to exclude zero at that effect size. The
September capture window yields ~20 sessions. So:

**A daily-return Sharpe ratio is not a reportable headline for this project, and claiming
one would be exactly the amateur tell the project is built to avoid.** Report it, but
report it *with* this interval and say plainly that it is uninformative.

**What to report instead — and this is a genuine methodological point, not a
consolation.** Move to **per-round-trip** observations, where N is $10^4$–$10^5$ rather
than 21:

| N round trips | SR/trip | SE | t |
|---|---|---|---|
| 5,000 | 0.02 | 0.0141 | 1.41 |
| 20,000 | 0.02 | 0.0071 | **2.83** |
| 50,000 | 0.02 | 0.0045 | **4.47** |

Per-fill PnL is heavy-tailed, so **bootstrap** the CI on PnL-per-round-trip rather than
trusting the closed form. Add a higher-frequency Sharpe with **Newey–West** standard errors
to handle autocorrelation, and *explain the discrepancy* between it and the daily figure —
that explanation is itself a strong signal of competence.

### 11.6 The full metric set

**Decomposition** — effective / realized / adverse selection at three horizons (§11.1).

**Markout curve** — mean PnL per fill against horizon 0 → 5 min. **The shape is the
diagnosis:** monotone decay = pure adverse selection; dip-then-recover = temporary impact
you are being *paid* for. The single most informative exhibit in the writeup.

**Adverse-selection ratio** = adverse selection / gross spread capture. **Above 1 means you
are a liquidity donor.**

**PnL per round trip** and **per unit traded**, stated against the ₹2.37 / ₹3.94 floor —
the most legible number in the project.

**Return on margin, not notional.** Short options are margin-bound (SPAN + exposure, order
₹1–1.5 lakh/lot); PnL/margin is what a desk asks for first. State the caveat that real
market makers get spread and netting benefits a per-leg calculation overstates.

**Inventory diagnostics** — mean $|q|$, inventory half-life, fraction of time pinned at the
limit (high ⇒ **the limit is the strategy**), and **corr(inventory, PnL)**: materially
positive means the "market maker" is a directional bet in costume. Pre-empting that
critique is worth a great deal.

**Participation** — fill ratio, quote-to-trade ratio per side, time quoting both sides, and
the **queue-position-at-fill distribution**, which *validates* the queue model rather than
assuming it.

**Capacity and robustness** — PnL vs quote size (where does it turn over?); latency
sensitivity (the *slope* answers "strategy or colocation bet"); the cancellation-convention
2×2 as the epistemic bound; and a regime breakdown by DTE bucket, realized-vol tercile and
time of day, because **concentration is fragility and should be shown, not hidden**.

#### Which of these actually exist as of 2026-09-03

Built and reported in §17: the **decomposition** at four horizons (1 s / 5 s / 30 s / 300 s
— one more than §11.1 planned, and the fourth is what made the pick-off diagnosis legible);
the **adverse-selection ratio**, which is above 1 at every width tested; **PnL per unit
traded** against the cost floor; **inventory diagnostics** in the specific form that
mattered (fraction of time pinned at the limit — 45% short calls, 40% long puts — and the
spread-capture-vs-inventory split, which is a sharper instrument than `corr(inventory,
PnL)`); and **participation** in part (fill counts, stand-down attribution by named reason).

Not built: the **markout curve** as an exhibit (the four-point horizon table is its
skeleton but it is not plotted, and §11.6 calls the curve "the single most informative
exhibit in the writeup"); **return on margin**; **inventory half-life**;
**quote-to-trade ratio**; the **queue-position-at-fill distribution** (the data to build it
is recorded — `Fill.queue_ahead` and `Fill.cum_volume` are carried on every fill for exactly
this purpose — but nothing consumes it yet); **capacity vs quote size**; **latency
sensitivity**; the **cancellation 2×2** (§5.1 — this is the broken guarantee); and the
**regime breakdown**, which needs the seven unanalysed September sessions (§17.7).

**One metric this section should have specified and did not: the directional-exposure
regression.** §11.6 asks for `corr(inventory, PnL)`, and §17.4 reports something better — the
exact capture-vs-inventory split. But the sharpest form of the question is a **regression of PnL
increments on underlying returns**, reporting **β** (directional exposure in delta units, ≈0 if
market-neutral) and **R²** (the fraction of PnL variance explained by direction). R² is literally
"how much of this is a directional bet", it is computable *within* a single day from per-interval
increments rather than needing n days, and it is the standard way the claim is evidenced. **Not
built — see §19.5, where it also serves as the discriminating test for the gap protocol.**

The gap is not oversight so much as sequencing: the headline result was negative early
enough that building more instrumentation around a strategy that does not work would have
been the wrong order. But **§17 is reported under a thinner metric set than §11.6
specifies**, and that should be closed before anything is published.

---

## 12. Cost model

`src/bnfmm/sim/costs.py` (418 lines, 48 tests), `config/costs.yaml`.

**Why this file is load-bearing:** a market maker's gross edge is a few ticks per round
trip. On BANKNIFTY futures one tick is ₹6.00 per lot while STT alone is ~₹867 per
round-trip lot. **Costs do not trim the PnL here, they dominate it by two orders of
magnitude.** Any market-making result that omits this stack is not wrong at the margin — it
is wrong about the *sign*.

### 12.1 The statutory stack

Rates retrieved 2026-08-23 from `https://zerodha.com/charges/`. Statutory rates are
identical across brokers; only brokerage is broker-specific.

| Component | Options (on **premium**) | Futures (on **turnover**) | Side |
|---|---|---|---|
| STT | 0.15% | 0.05% | sell |
| Exchange txn | 0.03553% | 0.00183% | both |
| SEBI fee | 0.0001% | 0.0001% | both |
| Stamp duty | 0.003% | 0.002% | buy |
| IPFT | 0.0000001% | 0.0000001% | both |
| GST | **18%** on (brokerage + exchange_txn + sebi_fee + ipft) — **not** on STT or stamp duty | | |

**`config/costs.yaml` carries a `VERIFY BEFORE PUBLISHING` block and it means it.** These
rates change by government budget and SEBI circular, and the futures STT rate in
particular has been revised repeatedly (0.01% → 0.0125% → 0.02% → the current 0.05%).
Every headline number in this repo depends on rates retrieved on 2026-08-23. Re-check
against the current circular and update `retrieved:` before publication.

### 12.2 Four corrections made during Phase 0b

1. **Per-broker, per-product brokerage profiles** replaced a uniform
   `min(₹20, 0.03%)`, which was **wrong for Dhan options** — Dhan charges a **flat ₹20 for
   all F&O**. `CostModel.for_profile(name)`; profiles `member` (₹0), `dhan` (₹20 flat both
   products), `zerodha` (comparison only). `DECISIONS.md` #12.
2. **Brokerage is per executed *order*, not per fill.** A quote filling in five partials
   pays ₹20 **once**. `OrderCost` is keyed by order id and charges brokerage on the first
   fill only, statutory components per fill. Test: five partials of one order cost the same
   brokerage as one full fill, and *strictly less* than five separate orders.
3. **Expiry flattening.** Exercised/assigned ITM options attract an additional charge.
   BANKNIFTY is cash-settled so there is no delivery risk, but the charge still applies.
   The rate needs re-verification before publication; **the design consequence is
   rate-independent** — inventory must be flat before expiry, enforced as a hard constraint
   in `strategy/inventory.py` with a forced-unwind schedule, and tested. That lands with
   Phase 4 and is currently **outstanding**.
4. **Quote size became a decision variable** — see §11.3.

---

## 13. Open items, gaps and risks

> **Status banner, 2026-10-02.** This section was written on 2026-09-03/04. Each subsection now
> opens with a ⚙ line giving its current status. Summary: **resolved** — 13.1, 13.10, 13.11,
> 13.12; **partly resolved** — 13.2, 13.3; **still open** — 13.4 (ruff), 13.9 (stale-master test,
> now safe to fix), 13.13 (dangling docstring references); **moot because capture is closed** —
> 13.5, 13.6, 13.7; **superseded** — 13.8.

### 13.1 Not committed to git

⚙ **Status 2026-10-02: RESOLVED.** First commit 2026-09-30; everything committed and pushed; branch `main` (renamed from `master`), fork `rv-market-maker`, tag `classic-mm-v1`. Author identity is set in the repo-local git config only. Generated outputs (`reports/mm/`, `reports/qa/`, `reports/rv/`) and `data/` are gitignored.

**70 files pending — 42 staged, 28 untracked — and zero commits.** `git commit` fails with
"Author identity unknown": `user.name` and `user.email` are unset. This must be resolved by
the user, not guessed, because it stamps every commit of a repo intended for recruiters.

The untracked set has grown from 15 to 28 and is now where most of the *interesting* code
lives — everything under `book/`, `fairvalue/`, `strategy/`, `sim/fills/`, `sim/backtest.py`,
`scripts/backtest.py`, `scripts/strike_economics.py` and
`reports/phase1_market_maker_results.md` is untracked. **A crash or a stray `git clean`
would destroy the entire market maker**, which is a materially worse exposure than the same
sentence described three weeks ago.

Also: the branch is `master` with zero commits while the PR default is `main`. **Renaming
is free while the history is empty** and should happen at the first commit.

Deferred by explicit user instruction ("we'll do it much later in the project"). Phase 6 at
the latest.

### 13.2 Test coverage gaps — the real ones

⚙ **Status 2026-10-02: PARTLY RESOLVED.** Tested now: `data/qa.py` (16), `book/tape.py` differencing and classification (8), `book/merge.py` (5), `fairvalue/black76.py` (11), `sim/portfolio.py` (10), `analysis/holdout.py` (13), `fairvalue/smile.py` (4, rv), plus latency and in-flight-cancel tests in `test_fills.py`. The "every subscribed id produced rows" check now exists (`qa.presence`, run by `scripts/qa_report.py`). **Still untested:** `data/channels.py`, `data/chain.py`, `book/reconstruct.py`, `fairvalue/parity.py`, `fairvalue/microprice.py`, `strategy/quoter.py` (Phase 1 only).

**This section has got worse, not better, since it was written.** Two capture-layer modules
still have zero tests, and six analysis-layer modules totalling 1,569 lines have been added
since with zero direct tests of their own.

#### The six new untested modules

| Module | Lines | What is untested | Why it matters |
|---|---|---|---|
| `book/reconstruct.py` | 281 | LOCF alignment onto a common clock, `usable()`, `best_bid`/`best_ask`, the crossed/one-sided flags | Every §17 number is computed on top of this. A silent misalignment shifts fills by one snapshot, which is exactly the look-ahead the project exists to avoid |
| `book/tape.py` | 225 | cumulative-volume differencing, rewind handling, Lee-Ready classification with the tick-test fallback, the discard rather than guess rule | The tape is *derived*, not captured (§17.1). It is the single most inferential input to the fill model |
| `fairvalue/microprice.py` | 105 | NaN propagation on a one-sided book, the zero-total-size case | Marking depends on it, and a wrong NaN policy is what bug #19 was |
| `fairvalue/parity.py` | 308 | precision weighting, bound clipping, empty-intersection detection, `--exclude-self` reconstruction | The parity anchor is the only configuration that ever showed a profit; §17.4 had to argue it was not circular by algebra and one CLI run rather than by test |
| `strategy/quoter.py` | 199 | every one of the nine named stand-down reasons, `round_to_tick` always rounding away from the market, skew arithmetic, the freeze-quantity cap | A quoter that silently crossed the book would manufacture profit |
| `sim/backtest.py` | 454 | `Position` average-cost flip-through-zero, `run_day`'s settle-before-requote ordering, `huang_stoll`'s prevailing-midpoint choice | Partly covered: `tests/test_pnl_decomposition.py` (9 tests) pins `decompose_pnl` and the NaN-close fallback. The rest is untested |

`sim/fills/depletion.py` (422 lines) is the exception — `tests/test_fills.py` (22 tests)
covers it, including the three sweep conditions that bug #17 turned on. That is the pattern
to copy, not a reason to consider the layer covered.

**The asymmetry is the finding.** The capture layer carries 400+ tests for code that merely
moves bytes; the analysis layer carries 31 for code that produces the conclusions. That is
backwards, and it is the honest reason the §17 numbers should be read as provisional. The
two simulator bugs in `DECISIONS.md` #17 were both caught by *looking at the results and
disbelieving them*, not by a test — which worked twice and is not a method.

#### The two original gaps, unchanged

- **`channels.py` (668 lines)** — needs a scripted fake websocket driving
  subscribe/recv/disconnect/staleness/reconnect. Specifically: the raw log written even
  when the decoder raises `ProtocolError`; a store exception incrementing `store_errors`
  without ending the run; the explicit `DISCONNECT_MESSAGE` on staleness (the 805
  prevention); `websockets_connector` rejecting `ping_interval_s >= 40`; **the new
  `budget = min(stale_after_s, remaining_to_deadline)` logic and the `_Stop`-vs-stale
  disambiguation from §7.6**; duration measured on the monotonic clock;
  `ChainPollChannel` round-robin, envelope, error tolerance and `min_interval_s` floor;
  `depth_channel`/`feed_channel` cap enforcement; `fanout_appender`/`store_appender`; URL
  redaction in `manifest()`.
- **`chain.py` (272 lines)** — `parse_chain` over a fixture reply and over the capture
  envelope.

Both were deliberately deferred: they are the *last* things written before a hard deadline,
and both are exercised end-to-end by the live rehearsal (§9.1). Nine sessions of successful
unattended capture is now much stronger evidence than one rehearsal was, so the *risk* these
carry has fallen even though the line count has not. `channels.py` in particular has been
exercised across 11 capture runs including reconnects, a 401, a mid-session restart and a
stale-master override. **`chain.py` is the weaker case** — chain polling has errored on
almost every day (1, 0, 1, 0, 11, 8, 0, 7, 2, 2, 0 errors per session) and nothing has ever
parsed those replies back.

#### One check that still does not exist and should

**Nothing asserts that every subscribed security id actually produced rows.** This is how
the index-spot gap (§9.4) survived nine sessions unnoticed: ids 25 and 13 are in every
manifest's subscription list, and no code ever compared that list against the ids present in
the Parquet. The check is a dozen lines and would have caught it on day one. It belongs in
`data/qa.py` and is the highest value-per-line test outstanding in the repo.

### 13.3 `DECISIONS.md` entries owed

⚙ **Status 2026-10-02: PARTLY RESOLVED.** Entries #20–#24 were added (tape bug, portfolio market maker, mm_v1 holdout, latency, RV fork). The older backlog listed below is still owed.

**The log now runs to #19** (741 lines). The three added since this section was written are
all Phase 1 findings and all load-bearing for §17:

| # | Title | Why it matters |
|---|---|---|
| **17** | Two simulator bugs that each manufactured profit, and how they were caught | The sweep-rule bug drove the measured effective half-spread *negative* — a simulator paying itself. Read this before trusting any fill model |
| **18** | The parity anchor's edge is real, but it is not market making | The self-reference algebra and the spread-capture/inventory split, long-form |
| **19** | A NaN closing mark silently valued open positions at zero | Expiry-day books go one-sided into the close; `Position.unrealised` returned 0.0 for a NaN mark |

Entries are still owed for: the raw-log format and its stdlib-only
constraint; the reconnect policy's documented-is-fatal rule (§4.5); why `universe.py`
exists separately from `resolve()` (§6.2); the corrected ±11 depth band; the chain poller
writing raw JSON rather than Parquet; `InstrumentKey` and the normalise-before-dedup rule
in `_batched`; the pair-preserving truncation rule; the `UnderlyingScrip` index-id fix
(§4.8); the `channels.py` deadline-bounded-recv fix (§7.6); the `capture.py` dotenv
omission; `main.py`'s existence and the wait-for-open policy (§8.2); the empty holiday list
(§8.3); the confirmed `dataValidity` lapse and its 7-DTE consequence (§4.7); the
`max_attempts` 20→40 change (§7.5); and the Sharpe standard-error correction (§11.5).

Newly owed, from Phase 1: **why the fill model is depletion rather than queue position** (the
200 ms snapshot finding, §17.1 — currently argued only in `depletion.py`'s docstring, and it
deserves a numbered entry because it is the project's central epistemic constraint); **why
marking uses the option's own microprice and never `fair_value`** (`sim/backtest.py`'s
`run_day` docstring); **why the fill mark is `mark[i−1]` and not `mark[i]`** (the Huang-Stoll
prevailing-midpoint choice, which changes the sign of the reported adverse selection); **the
`index_contracts` / `REQUEST_FULL` incompatibility** (§9.4 — an entry is owed whichever way
the decision goes); and **why layer 4 of the fair-value ladder was never built** (§10.2).

### 13.4 Ruff configured but never run

⚙ **Status 2026-10-02: STILL OPEN.** Ruff has still never been run. §0.7 item 4.

`pyproject.toml` has had a `[tool.ruff]` block with `line-length = 100` since day 1 while
ruff has **never been installed or run**. A repo that declares a linter it has never run is
a gap a reviewer finds with one two-second command.

Resolution (the implementer's call, as delegated): add `ruff>=0.6` to the dev extra and run
**`ruff check` only, no `ruff format`.** Reformatting 40 files would bury every real change
in the diff at exactly the point where the diffs are the review. Known long lines:
`scripts/phase0_recon.py` 69/273/274/310/311, `tests/test_instruments.py:161`,
`tests/test_no_order_placement.py:202`. Phase 1c.

### 13.5 The NSE holiday list is still unverified

⚙ **Status 2026-10-02: MOOT.** Capture is closed; no session will be scheduled again.

`config/market_calendar.yaml` still carries an **empty holiday list**, handled structurally
rather than guessed — see §8.3. The original blocker was tooling (`WebSearch` returned
`API Error: 400 tool type 'web_search_20250305' is not supported for this model` and
`WebFetch` of nseindia.com timed out); that may no longer hold, so **retry the fetch rather
than assuming it fails.**

In practice the gap has cost nothing: nine capture attempts on nine weekdays all found a live
market, so no holiday has fallen inside the window yet. **That is luck, not verification.**
The remaining September window (to ~Sep 29) still needs checking against the published list,
and `main.py` will happily wait all day for an open that never comes.

### 13.6 Suspend can overrun the capture stop

⚙ **Status 2026-10-02: MOOT.** Capture is closed.

Understood and bounded — see §8.2. Mitigation is operational: launch under
`systemd-inhibit --what=sleep:idle` and keep the charger connected. `main.py` warns if the
wait exceeds 30 minutes and it does not detect an inhibitor in its parent's cmdline (a
heuristic; a false negative costs a redundant warning, which is the right direction to be
wrong in).

**Nine sessions of evidence: the mitigation works, and the real failure mode was something
else.** No session was lost to suspend. Every session that ended cleanly ended at 15:35:11 or
15:35:12 — five of them — which is the deadline firing to the second. What did happen
repeatedly is **mid-session restarts**, visible as two or three session ids sharing one
trading date: Aug 26 (three), Aug 27 (two), Sep 1 (two), Sep 3 (two). Those are launch-time
and credential problems, not power problems.

### 13.7 Token expiry — the failure mode is confirmed, and it is worse than described

⚙ **Status 2026-10-02: MOOT** (historical: the cause of the lost Aug 26 session).

`Session.likely_expired` is a **property**, and when the token comes from the environment
`issued_at` is set at *load* time because we cannot know when it was actually minted. So
the flag is optimistic. A token pasted the previous evening may not cover 09:00–15:35.
**If in doubt, mint a fresh one before launching.**

**This is no longer hypothetical — it cost most of a session.** On 2026-08-26 the chain
channel failed every single poll with:

```
HTTP 401: {"data":{"808":"Authentication Failed - Client ID or Token invalid"},"status":"failed"}
```

and the feed channel received **no data at all** while depth captured only 61,576 frames
against a normal day's ~8.8 million. Aug 26 is the anomalously short session (§13.12), and an
expired or wrong token is the direct cause. Three separate launches that morning
(`20260826-091421`, `20260826-091800`, `20260826-095342`) all failed to establish a full
session.

Note the asymmetry that made this confusing: **depth connected while the feed and chain did
not.** A partial authentication failure is not a clean signal, so "the token is fine, depth is
flowing" was an available and wrong reading at 09:20.

### 13.8 Test count, for reference

⚙ **Status 2026-10-02: SUPERSEDED.** Current counts are in §0.3 and per file in §6.

**518 collected: 517 passing, 1 failing** (§13.9). Per file:

| File | Tests | File | Tests |
|---|---|---|---|
| `test_protocol.py` | 75 | `test_costs.py` | 48 |
| `test_universe.py` | 60 | `test_reconnect.py` | 43 |
| `test_rawlog.py` | 52 | `test_store.py` | 44 |
| `test_instruments.py` | 37 | `test_main.py` | 37 |
| `test_auth.py` | 31 | `test_results.py` | 28 |
| `test_no_order_placement.py` | 23 | **`test_fills.py`** | **22** |
| `test_master_facts.py` | 9 | **`test_pnl_decomposition.py`** | **9** |

The two bold files are new since the last revision and are the *only* tests of the entire
analysis layer — 31 tests against 1,569 lines that produce every conclusion in §17. Read
§13.2 before drawing comfort from the total.

### 13.9 ⚠ One failing test: the instrument master has gone stale

⚙ **Status 2026-10-02: STILL FAILING, NOW SAFE TO FIX.** Every capture run has a correct contract sidecar (§13.11) and the full 2026-08-23 master is archived at `data/reference/archive/api-scrip-master-detailed-2026-08-23.csv`, so refreshing the master no longer loses anything. The refresh is a 36 MB download from Dhan's public URL; ask the user before running `scripts/refresh_master.py`. After refreshing, expect `test_master_facts.py` assertions about August/September contracts to need updating, because those series have expired.

```
FAILED tests/test_master_facts.py::test_spot_bootstrap_agrees_across_calls_and_puts
AssertionError: 58,117.20 (calls 57,965.60 / puts 58,285.60, disagree 320.00)
              from 2026-09-29 chain, n=10
assert 320.0 < (0.001 * 58117.2)
```

**What it is.** `estimate_spot_detail` infers the underlying level from deep-ITM option
*circuit bands* in the instrument master (`mid(band) ≈ |spot − strike|`, so
`spot ≈ strike + mid` for calls and `strike − mid` for puts). The test asserts the call-side
and put-side estimates agree to within 0.1%. They now disagree by 320 points — 0.55%.

**Why.** `data/reference/api-scrip-master-detailed.csv` was written **2026-08-23** and has not
been refreshed since. Circuit bands are set by the exchange and drift as the underlying moves;
after eleven sessions the September chain's bands no longer track intrinsic value closely
enough for the identity to hold. **The estimator is not broken — its input is 11 days old.**

**Why it has not been fixed, deliberately.** Refreshing the master would fix the test and
**destroy the ability to label the August captures**, because the vendor prunes expired
contracts and the August series expired 2026-08-25. `scripts/snapshot_contracts.py` exists
precisely to break that dependency, and it has been run — all 11 capture manifests now have an
immutable `*-contracts.json` sidecar, and a BANKNIFTY slice is archived at
`data/reference/archive/master-BANKNIFTY-2026-08-23.csv`. **But five capture runs have no
manifest and therefore no sidecar (§13.11), so the dependency is not yet fully broken.**

Capture currently runs with `--allow-stale-master` to bypass the freshness guard. **Close
§13.11 first, then refresh the master, then re-run this test.** Do not refresh it before then.

### 13.10 ⚠ The 0.15% options STT rate is unverified and the whole conclusion rests on it

⚙ **Status 2026-10-02: RESOLVED — the rate is correct.** Union Budget 2026 raised options STT on sale premium 0.10% → 0.15% and futures STT 0.02% → 0.05%, both effective 2026-04-01, so 0.15% applies to every captured session. Recorded in `config/costs.yaml` (`stt_verified: 2026-09-30`, with sources). Exchange-txn, SEBI and stamp rates were not re-verified.

`config/costs.yaml` carries a literal `VERIFY BEFORE PUBLISHING ANY RESULT` block, retrieved
2026-08-23 from `https://zerodha.com/charges/`. **It has not been verified, and §17's central
claim is a direct function of it.**

The dependency is not a rounding matter. Options STT falls on **premium**, sell side, so it is
the dominant term in the round-trip floor and the whole reason the floor scales with premium
rather than staying fixed in ticks. At the Aug 24 ATM call (median mid ₹203.60) STT is **78% of
the ₹10.95 cost of a sell fill**. The member breakeven of 9.65 ticks against an 11-tick market
spread is the sentence the report turns on, and moving the STT rate moves it directly:

| STT on premium | Approx. member breakeven | vs the 11-tick spread |
|---|---|---|
| 0.10% | ~6.7 ticks | clears comfortably |
| **0.15% (assumed)** | **9.65 ticks** | 88% of the spread |
| 0.20% | ~12.6 ticks | does not clear |

That is the range across which the conclusion changes from "thin but viable for a member" to
"structurally dead". **This is the single highest-leverage unverified fact in the repo** and
should be checked against the current Finance Act and SEBI circulars before any number is
shown to anyone. Rates have been revised repeatedly (§12.1 records the futures STT path
0.01% → 0.0125% → 0.02% → 0.05%), so the prior that a rate retrieved once is still correct is
weak. Update `retrieved:` in `costs.yaml` when it is done.

### 13.11 ⚠ Five capture runs have no manifest, and `snapshot_contracts.py` cannot see them

⚙ **Status 2026-10-02: RESOLVED.** `scripts/snapshot_contracts.py` now reads subscribed ids from the raw-log `BNRLOG1` preamble when a run has no manifest, and resolves ids only within NSE segment D (F&O) and I (indices) — which share no ids — with indices built via `universe.index_contracts` (recorded as `IDX_I`, segment code 0). All **19 runs across 10 days** have sidecars, 0 unresolved ids; the 11 pre-existing sidecars were regenerated with the corrected index segment. The archive slice no longer contains ABB / Adani Enterprises.

Two coupled problems, both about **what a captured day knows about itself**.

**(a) A hard-killed run leaves its data undescribed.** `scripts/capture.py` writes the
`*-capture.json` session manifest in `report()`, which runs *after* the `try/finally` — so it
only lands when `run_capture` returns normally. SIGINT and SIGTERM are handled (they set the
stop event and lead to a clean exit with a manifest), but a hard kill, a crash, or a power loss
leaves the Parquet on disk with no manifest at all. Five of the eleven runs across nine days
are in that state:

| Trading date | Run | Manifest? |
|---|---|---|
| 2026-08-26 | `20260826-091800`, `20260826-095342` | ❌ both missing |
| 2026-09-01 | `20260901-091325` (morning, 09:13→12:40) | ❌ missing |
| 2026-09-02 | `20260902-091244` (09:12→12:21) | ❌ missing |
| 2026-09-03 | `20260903-091522` (09:15→10:12) | ❌ missing |

For **2026-09-02 and 2026-09-03 the entire `sessions/date=…` directory does not exist**, so
those days have no subscribed-id list, no decode statistics and no reconciliation record.

`scripts/snapshot_contracts.py` iterates `sessions/date=*/*-capture.json`, so it silently skips
exactly those runs — which is why `--check` reports a reassuring `11 sessions, 0 unresolved
ids` while two whole trading days are absent from its output. **The reassurance is scoped to
what it could see.**

**The mapping is recoverable, and that is the fix.** Every raw-log part file opens with a
`BNRLOG1` preamble carrying a JSON metadata line that includes the full `security_ids` list.
Verified directly:

```
2026-09-02 depth: session=20260902-091244 ids=48
2026-09-02 feed:  session=20260902-091244 ids=328
2026-09-03 depth: session=20260903-091522 ids=48
2026-09-03 feed:  session=20260903-091522 ids=328
```

So `snapshot_contracts.py` should fall back to `rawlog.read_preamble()` when no capture
manifest exists, rather than skipping the day. **This has a deadline**: the September series
expires 2026-09-29, and once the master is refreshed past that date those ids become
unresolvable. Do this before refreshing the master (§13.9).

**(b) Security ids are not globally unique, and the script assumes they are.**
`resolve()` filters on `SECURITY_ID.isin(ids)` with **no exchange/segment filter**. Ids 25 and
13 each match two rows in the master:

| SECURITY_ID | EXCH_ID | SEGMENT | DISPLAY_NAME |
|---|---|---|---|
| 13 | NSE | E | ABB |
| 25 | NSE | E | Adani Enterprises |
| 13 | NSE | **I** | **Nifty 50** |
| 25 | NSE | **I** | **Nifty Bank** |

The loop does `found[id] = …` per matching row, so **last row wins**. It currently resolves to
the indices — but only because the vendor happens to list segment `I` after segment `E`. A
re-ordered master would silently label the BANKNIFTY spot index as "ABB", with no error and no
change in the `48/48; 328/328` success line.

There is a second, already-materialised consequence: the script calls
`instruments._to_contract`, which leaves `exchange_segment` at its `NSE_FNO` default, so the
sidecars record the two indices as **`exchange_segment: "NSE_FNO"`, `segment_code: 2`** when
they are `IDX_I` / `0`. `universe.py` has `_index_contract` and `index_contracts` which do this
correctly — the script reaches past them. Confirmed in
`date=2026-08-24/20260824-092700-contracts.json`:

```json
{"display_name": "Nifty Bank", "exchange_segment": "NSE_FNO", "segment_code": 2,
 "security_id": 25, "instrument": "INDEX", "expiry": "0001-01-01", "tick_size": 0.0005}
```

The same root cause makes the archive block's `master.SECURITY_ID.isin([25, 13])` pull **four**
rows, so `master-BANKNIFTY-2026-08-23.csv` contains ABB and Adani Enterprises alongside the
BANKNIFTY chain. Cosmetic, but it is the same bug.

**Impact today is limited** — there are no index rows in any captured Parquet at all (§9.4), so
nothing downstream reads these two entries. **The sidecar is nevertheless the immutable record
of what was subscribed**, and it currently records the wrong segment for two of 328 ids. Fix:
route index ids through `universe.index_contracts`, and key `resolve()` on
`(EXCH_ID, SEGMENT, SECURITY_ID)`.

### 13.12 ⚠ 2026-08-26 is a broken session and should be excluded, not averaged in

⚙ **Status 2026-10-02: RESOLVED.** Aug 26 is excluded mechanically by the inclusion rule in `config/frozen/protocol.yaml` (depth and feed coverage 19% < 25%), not by judgement.

Aug 26 holds **66 MB of depth and 53 MB of quotes against a normal day's ~320 MB and ~280 MB**
— roughly 20% of a session. It is not a short trading day. Three launches that morning all
failed, and the one that wrote a manifest recorded:

```
degraded = ["feed: received no data at all",
            "chain: every poll failed (ChainError: HTTP 401: 808 Authentication Failed
             - Client ID or Token invalid)"]
depth: frames=61,576   feed: frames=0   chain: errors=8
```

Raw-log write times bracket the damage: `20260826-091421` stops at 09:18, `20260826-091800` at
09:42, `20260826-095342` at 10:52 — **and then nothing for the rest of the day.** Coverage is
roughly 09:14–10:52 with gaps inside it, against a 09:15–15:30 session.

Root cause is §13.7: an expired or invalid access token. The confusing part is that **depth
authenticated while feed and chain did not**, so the 09:20 checklist in §15 would have shown
"depth rows present" and passed.

**Treat Aug 26 as excluded from any cross-day statistic** until `data/qa.py` can segment it
properly. It is also the reason the September corpus is **eight usable sessions, not nine.**
Two consequences worth stating: any per-day average that silently includes it is wrong, and
the §15 morning checklist needs a *per-channel* row-count check rather than a depth-only one.

### 13.13 `depletion.py` promises a module that does not exist

⚙ **Status 2026-10-02: STILL OPEN.** `depletion.py` lines ~89 and ~170 still cite `sim.fills.sensitivity`. The queue-convention comparison is now done by `scripts/mm.py --optimistic-queue` (§20, robustness table), so the fix is to point the docstrings there.

`src/bnfmm/sim/fills/depletion.py` references **`sim.fills.sensitivity`** twice — at line 89
("the sensitivity of the headline number to the queue rule is a first-class output") and line
170 ("`sim.fills.sensitivity` measures what happens under the optimistic one"). **There is no
such module.** `sim/fills/` contains only `__init__.py` and `depletion.py`.

The capability half-exists: `DepletionSimulator` takes `queue_at_price_ahead` and
`allow_sweep_fill`, and `scripts/backtest.py` exposes `--optimistic-queue`. What is missing is
the thing the docstring actually claims — a **first-class output** that reports the headline
number under both conventions side by side. That is the same gap as §5.1's broken `PnlGrid`
guarantee, seen from the other end, and §5 is explicit that the cancellation/queue 2×2 is *the
epistemic bound of the whole project*, not a sensitivity footnote.

Either build it or amend both docstring references. **Do not leave a docstring claiming an
output that a reader cannot find** — it is the kind of thing a reviewer checks in ten seconds,
and in a repo whose comments are otherwise load-bearing (§16) it reads worse than an honest
`TODO`.

---

## 14. Phase plan, with completion criteria

> ⚙ **Status banner, 2026-10-02 — this plan is finished, not in progress.** Final state of each
> phase: 1b capture **closed** (10 sessions); 1c consolidation **done in substance** (`data/qa.py`,
> `scripts/qa_report.py`, contract sidecars; ruff still not run); 2 book/fair value **done** for
> what the data supports (layer 3 unbuildable, layer 4 built only on the rv branch as
> `fairvalue/smile.py`); 3 fill simulator **done** (depletion model + activation latency); 4
> quoting **done** (`sim/portfolio.py`; no Avellaneda–Stoikov baseline was built); 5 attribution
> and validation **done** (decomposition, Huang-Stoll, β/R², frozen holdout, config log); 6
> write-up **done for the classic line** (README on `main`, Phase 2 report). The dated text below
> is the original plan, kept as the record of what was intended.

Phases 0, 0b and 1a are **done**. The market happens once a day and cannot be re-run, so
everything after 1a runs *while capture continues daily in the background*.

### ⚠ How this plan actually went — read before using the dates below

**The dates in the rest of §14 are the plan as written on 2026-08-24 and are now wrong.** What
happened instead:

| Phase | Planned | Actual |
|---|---|---|
| 1b capture | Aug 24 → Sep 22 | **On schedule and still running.** 9 sessions, 41 GB, 8 usable (§9.5, §13.12) |
| 1c consolidation | Aug 25–31 | **Not done.** `data/qa.py` does not exist; `test_channels.py`/`test_chain.py` unwritten; ruff still not installed; the DECISIONS backlog grew |
| 2 clock/sources/book/fair value | Sep 1–12 | **Partially, and out of order.** `book/` and `fairvalue/` layers 1–2 built Aug 26 → Sep 3. **`sim/clock.py` and `sim/sources/` were never built at all** |
| 3 fill simulator | Sep 8–21 | **Done early, in one variant.** `sim/fills/depletion.py` + 22 tests. `naive`/`touch`/`queue` became one honest model — §17.1 |
| 4 quoting | Sep 15–21 | **Done early, minimal.** `strategy/quoter.py`, 199 lines. No Avellaneda–Stoikov. No forced-unwind constraint |
| 5 attribution | Sep 22 → Oct 7 | **Partially, and early.** Huang-Stoll and the PnL split are built and reported (§17); `PnlGrid` is bypassed (§5.1); walk-forward and holdout machinery unused |

**Why the order inverted.** Once the first two sessions were on disk, the instruction was to
build the market maker and measure it. The shortest path to a real number ran straight through
Phases 2–5 for *one strike on two days*, skipping the abstractions (clock, sources) that only
pay off when replay and live share code. That was the right call for getting an answer and it
has left three specific debts:

1. **`sim/clock.py` and `sim/sources/` do not exist.** `sim/backtest.py` reads Parquet directly
   and iterates the depth feed's own snapshot index. The look-ahead discipline it needed was
   achieved by *ordering within the loop* (settle fills before re-quoting) rather than by an
   abstraction. This works and is tested by construction, but the planned test —
   *"no module under `strategy/` or `sim/` references `time.time`, `datetime.now`, or
   `time.monotonic`"* — was never written, and shadow mode (§14, deferred) cannot be built
   without these two modules.
2. **Phase 1c was skipped entirely**, so the data-quality report that was supposed to gate
   analysis does not exist. §17's gap accounting was done ad hoc, per run, rather than by
   `data/qa.py`. That is why §13.11 and §13.12 were found by hand three weeks late.
3. **Phase 5's guardrails were skipped along with its plumbing.** `data/holdout.py` and
   `config/frozen/` — the §11.4 mechanisms that make out-of-sample checkable by a third party
   — are unbuilt, and §17 reports numbers from a hand-driven parameter sweep on the only two
   days analysed. **§17 is therefore in-sample by construction**, which it says.

None of this invalidates §17; all of it bounds how it can be described. The seven unanalysed
September sessions (§17.7) are the natural place to do it properly, in the planned order.

### 1b — Capture (Aug 24 → ~Sep 29) — **on track**

| Date | Contract | What it samples |
|---|---|---|
| Mon **Aug 24** | August, **1 DTE** | Shakedown on a live open + near-expiry regime. **First real bytes.** |
| Tue **Aug 25** | August, **0 DTE** | Expiry day, full session including settlement behaviour |
| Wed Aug 26 → ~Tue Sep 29 | September | Full cycle, ~34 DTE down to 0 DTE, ~24 sessions |

**Actual as of 2026-09-03:** 9 sessions captured, 8 usable, ~13 trading days left in the
window. See §9.5 for the corpus table and §13.12 for the one broken session.

**Done when:** ~22 sessions of Tier A depth + feed + chain are on disk, each with a clean
reconciliation, and `reports/data_quality.md` accounts for every excluded segment with a
reason. **Note both clauses are still open** — the reconciliation exists per run (§9.3) but
`reports/data_quality.md` does not exist at all.

**Evening measurements on day 1** — all three were done, and here is how they landed:
- Volume: **~330 MB depth + ~380 MB quotes per session**, well under the 2 GB/day threshold
  that would have forced narrowing the depth band. **41 GB used, 906 GB free.** The band was
  never narrowed and does not need to be.
- The **golden fixture** to `tests/fixtures/golden/` was **not saved**. Still outstanding, and
  now more valuable than it was: `tests/test_fills.py` and `tests/test_pnl_decomposition.py`
  both construct synthetic books by hand.
- The **spread survey** was done, by a better route than `/optionchain`: `scripts/strike_economics.py`
  measures it from the captured depth books themselves. Answer in §17.2 — **median 11 ticks at
  the ATM against a 9.65-tick member floor.** This was the one question Phase 0 could not
  answer, and it is now answered: *thin, and negative after adverse selection.*

Also in 1b: **Tier B backfill** via `expired_options_data` — **not attempted.** Requires
fixing the `DH-905` request body (§4.8), and it was overtaken by the fact that live capture
was already producing better data than a backfill would.

### 1c — Offline consolidation (planned Aug 25–31) — ❌ **not started**

**This phase was skipped, and §13.11 / §13.12 are the bill for it.** Both were found by hand on
2026-09-03, three weeks after the data-quality report that would have caught them was due.
Nothing here has been built. It is the highest-value unbuilt phase, because the seven
unanalysed sessions (§17.7) should not be analysed without it.

- **`data/qa.py`** — segment splitting on arrival gaps in *either* clock (§5.5).
  Segmentation runs on the **session's** arrival stream, not per instrument, so an illiquid
  strike is not shredded by mere silence. Per segment: crossed book by last-known-partner
  pairing with partner age reported, level-count distribution, zero-padding integrity,
  unsorted sides, `recv_seq` duplicates as a read-path check, message rate vs session
  median, within-segment skew. On quotes: cumulative-volume monotonicity,
  `last_trade_epoch` drift with the static fraction reported, ambiguous-print fraction,
  crossed five-level block. **Cross-feed classifier:** volume advancing across a depth gap
  is evidence of a *dropout* rather than silence.
  **Add, from what nine sessions have taught:** a per-channel, per-id **"every subscribed id
  produced rows"** assertion (§13.2 — this is what would have caught §9.4 on day one), and a
  **session-completeness** check that flags a day whose coverage is materially short of
  09:15–15:30 (§13.12) instead of leaving it to be spotted in a `du` listing.
- **Two new checks the real data enables:** raw-log-vs-Parquet byte agreement, and
  **WebSocket-vs-option-chain top-of-book agreement** — two independent sources
  disagreeing means one is wrong, and that is free. **Note the second is now partly blocked:**
  chain polling errored on most sessions and nothing has ever parsed the saved replies
  (§13.2), so the option-chain side of that comparison is unproven.
- **`tests/test_channels.py` and `tests/test_chain.py`** (§13.2).
- **Tier C generator** (`src/bnfmm/synthetic/`) — assumed parameters, now recalibratable
  against real Tier A within the same week rather than at Phase 5. Still an empty package.
- **Ruff** (§13.4). **`DECISIONS.md` backlog** (§13.3).

**Done when:** `reports/data_quality.md` renders from real data, the two agreement checks
pass on a real session, and `pytest` + `ruff check` are both clean with no untested module
over 200 lines. **That last clause now has eight offenders, not two** (§13.2).

### 2 — Clock, sources, book, fair value (planned Sep 1–12) — ⚠ **half built**

- `sim/clock.py` — ❌ **never built.** `Clock` protocol; `SimClock` advances on events,
  `WallClock` reads the OS. **Test: no module under `strategy/` or `sim/` references
  `time.time`, `datetime.now`, or `time.monotonic` directly.** Without this, look-ahead-free
  replay and live execution cannot share code — which is why **shadow mode is currently
  unbuildable**, not merely deferred.
- `sim/sources/` — ❌ **never built.** `EventSource` protocol yielding `(timestamp, event)`;
  `ReplaySource` reads Parquet, `LiveSource` wraps the depth WebSocket. `sim/backtest.py`
  reads Parquet directly instead.
- `book/` — ✅ **built.** `reconstruct.py` (281 lines) and `tape.py` (225). Note the scope
  change: **the planned `Δqty = adds − cancels − trades` decomposition is not possible** on a
  200 ms snapshot feed (§17.1), so `book/` does LOCF alignment and touch extraction, and the
  tape is recovered by differencing the quote feed's cumulative volume. Imbalance and OFI
  were not built **in `book/`** — but see §18.2: `imbalance()` exists in `fairvalue/`,
  uncalled.
- **`fairvalue/`** — ⚠ **layers 1–2 of the four in §10.** `microprice.py` (105) and
  `parity.py` (308). Layer 3 (`basis.py`) is unbuildable without index spot (§9.4); layer 4
  (`smile.py`, `greeks.py`) was deliberately skipped (§10.2). **`microprice.py` also contains
  `imbalance()` and `depth_weighted_micro()`, neither of which is called from anywhere**
  (§18.2).

**Done when:** book reconstruction passes against hand-built snapshot sequences; parity
matches hand-computed values; a constructed empty-parity-intersection case is **detected**;
the smile fit satisfies butterfly no-arbitrage; greeks match published values. **None of these
four criteria has been met** — the code exists, the tests do not (§13.2), and the last two
concern a layer that was not built.

### 3 — ★ Fill simulator (planned Sep 8–21) — ✅ **built early, one variant**

`naive`, `touch`, `queue`; **both cancellation bounds**. This is the centrepiece of the
writeup.

**What was actually built, and why it is one model rather than three.** The three-variant plan
assumed an event stream. The feed turned out to push a complete 200 ms snapshot instead
(§17.1), which makes a genuine queue model unsupportable — so `sim/fills/depletion.py` (422
lines, 22 tests) implements the single convention the data can defend, documents what it
overstates and understates, and exposes the optimistic/pessimistic queue toggle as a
parameter (`queue_at_price_ahead`) rather than as a separate model. **This is a downgrade in
ambition and an upgrade in honesty**, and it is the project's central epistemic result.

Property tests, non-negotiable: no fill without sufficient observed volume; queue position
monotone; traded volume conserved; **`pessimistic ≤ proportional` always**. ✅ Covered by
`tests/test_fills.py`.

**Done when:** all four property tests hold on real Tier A data and the naive-vs-queue gap
is measurable on at least one real session. **The gap is measurable but has not been
measured** — see §5.1 and §13.13. `--optimistic-queue` exists; nothing reports both columns
side by side.

### 4 — Quoting (planned Sep 15–21) — ⚠ **minimal version built early**

Fixed-spread baseline ✅ (`strategy/quoter.py`, 199 lines: `theo`, `skew`, `make_quotes`,
nine named stand-down reasons, tick rounding always *away* from the market).

**Not built:** Avellaneda–Stoikov reservation price and spread. The half-spread is a fixed
parameter swept by hand. **This is a real gap in the writeup's ambition** — §11.3 makes a
specific original point about A–S ignoring fixed per-trade costs, and that point cannot be
made without an A–S baseline to compare against.

**Delta via CE+PE skew, no hedging leg** ✅ (§10.4) — and §17.3 shows the consequence:
inventory skew as the *only* control meant the quoter sat pinned at its position cap 45% and
40% of the day, so **the cap became the strategy.**

**Forced unwind before expiry** ❌ **not built.** `strategy/inventory.py` does not exist. This
is the §12.2 item 3 commitment, and Aug 25 — the 0 DTE session that was captured specifically
to exercise it — has now been analysed *without* it. Instead the run carried an open position
into a dead book and exposed bug #19 (§13.9, `DECISIONS.md` #19), which is a useful accident
but not the test that was promised.

**Done when:** the forced-unwind constraint is tested and provably binds on the Aug 25
session. **Open.**

### 5 — ★ Attribution and validation (planned Sep 22 → Oct 7) — ⚠ **partly done, early**

The headline naive-vs-queue 2×2 ❌ (§5.1 — `PnlGrid` is bypassed) plus the full metric set of
§11.6 ⚠ (about half; see the status subsection there). **The attribution identity summing to
total PnL as a unit test** ✅ — `tests/test_pnl_decomposition.py`, 9 tests, and it is the one
piece of Phase 5 that landed exactly as specified.

Walk-forward by calendar day with the number of configurations tried reported ❌ — §17 reports
a hand-driven sweep on the only two days analysed, in-sample. `data/holdout.py` and
`config/frozen/` ❌ do not exist, so the §11.4 guardrails are unenforced. The §10.3 estimator
comparison ⚠ was run for two of five candidates (§10.3). Recalibrating Tier C ❌ — the
synthetic package is empty.

All of it reads saved Parquet, so **none of it needs entitlement.** This matters: it can
proceed after the subscription lapses at the end of September. **Confirmed by construction —
every §17 number was produced from Parquet on disk, with no API call.**

**Done when:** `PnlGrid` headline renders from Tier A, the attribution identity test
passes, and every number in the README is reproducible from committed config. **One of three.**

### 6 — Writeup (Oct 8–12)

README as the primary artifact, limitations written for a skeptic. Re-verify statutory
rates (§12.1, §13.10). **The deferred commit lands here at the latest** (§13.1).

**`reports/phase1_market_maker_results.md` (230 lines) is the first draft of this**, written
three weeks early because the result arrived early. It is the document to build the README
from. **The current README predates it and is stale** — it describes a project whose market
maker has not been run.

### Deferred, not cancelled — shadow mode

Live feed + live clock + simulated fills + **zero orders**. Needs frozen parameters and a
finished quoter, so it is out of the subscription month. Cheap to add later *because*
Phase 2 builds the abstractions anyway.

**⚠ That last clause is now false.** Phase 2's `sim/clock.py` and `sim/sources/` were never
built (see the actuals table above), so shadow mode is **not cheap to add** — it needs those
two modules first, plus a `LiveSource` that shares an interface with the Parquet reader that
`sim/backtest.py` currently bypasses. Budget it as real work, not as a wrapper.

There is also now a stronger reason not to bother: **the strategy loses money (§17)**, so
shadow-running it would measure the latency and fill realism of something already known not to
work. Shadow mode becomes worth building only after §17.6's improvements produce a
configuration that survives on the September sessions.

**What it would and would not prove.** It removes two of the three standard backtest lies:
look-ahead becomes **impossible** (the future has not arrived) and latency becomes real and
measured rather than modelled. **Queue position remains simulated** — the order was never
in the book, so there is no market impact and no self-selection from actually resting
there. Shadow PnL is therefore an **upper bound, not an estimate**, and the README must say
so in those words.

---

## 15. Runbook for a capture morning

> ⚙ **Status 2026-10-02: NOT IN USE — capture is closed.** Kept because it is the only record of
> how the corpus was produced, and would be the starting point if capture were ever resumed (it
> would need a renewed Dhan Data API subscription and a fresh instrument master).

**This is the procedure that has actually worked for nine sessions.** `main.py --mode record`
is the designed entry point; the command in daily use is `scripts/capture.py` directly, with
an explicit stop time and the stale-master override:

```bash
systemd-inhibit --what=sleep:idle .venv/bin/python scripts/capture.py --until 15:35 --allow-stale-master
```

The `main.py` path remains valid and is preferable when launching the night before, because it
waits for the open and preflights at `start − 600 s`:

```bash
systemd-inhibit --what=sleep:idle .venv/bin/python main.py --mode record
```

**Before launching:**

1. **Confirm today is a trading day.** The holiday list is still empty (§8.3, §13.5), so this
   is a human check. Nine for nine so far, which is luck.
2. **Mint a fresh access token.** Not "check whether it is probably still valid" — §13.7 is
   optimistic by construction, and a stale token cost most of 2026-08-26 (§13.12). This is the
   single highest-frequency failure mode in the whole operation.
3. **Charger connected**, and launch under `systemd-inhibit`. **Start above 90% battery.** Under
   `systemd-inhibit` the laptop *is* the UPS, so a mains cut with a charged battery produces **no
   gap at all** — this one line of discipline removes the entire power-cut failure mode (§19.6).

**Why `--allow-stale-master`.** The instrument master on disk is from 2026-08-23 and the
freshness guard would refuse to start (§13.9). **Do not "fix" this by refreshing the master** —
that would destroy the ability to label the August captures. Close §13.11 first.

**At 09:20, check — and check per channel, not just depth:**

- **depth rows present** (§9.3), **and feed rows present**, **and chain polls succeeding.**
  On Aug 26 depth authenticated while feed and chain did not, so a depth-only check passed on
  a session that was already broken (§13.12). A green depth light is not a green session.
- Raw log growing; Parquet partitions appearing under today's `date=` partition.
- No `805`/`806` in the log — and no **`808`**, which is the authentication failure that
  actually occurred.
- Decode stats clean.

**Expected exit codes:** `0` clean; `1` degraded — read the `degraded` list in the manifest,
most likely one channel received nothing; `2` failed.

**At the end of the session, confirm the manifest landed.** A hard-killed run writes no
`*-capture.json` and its data is left undescribed (§13.11):

```bash
ls data/tier_a/parquet/sessions/date=$(date +%F)/
```

If there is no `*-capture.json`, note the session id from the raw-log filenames — the
subscribed-id list is recoverable from the `BNRLOG1` preamble, but only if someone knows to go
looking.

**If capture dies mid-session, relaunch.** Restarts are cheap and produce a second session id
under the same date; the gap is visible and honest. Aug 26 was lost partly because it was not
relaunched after 10:52.

**⚠ The missing piece: nothing watches the feed after 09:20.** The check above is a single glance
at one moment. On 2026-08-26 the feed died at 10:52 and **nobody noticed for four and a half
hours** — the session was already unrecoverable by the time anyone looked. **A watchdog that
alarms on >30 s with no depth frames converts a lost session into a lost minute, and it is the
highest-value operational change available** (§19.6). It is worth more than any of the analysis
improvements in §18, because a session happens once and cannot be re-run.

**Optional standalone checks:**

```bash
.venv/bin/python scripts/check_entitlement.py
```

```bash
.venv/bin/python scripts/capture.py --dry-run --asof 2026-09-04
```

**Per-day config edits:** §7.7. The expiry roll is now automatic and has been observed working
unattended (§6.1), so no config edit is normally needed.

---

## 16. Reading order for someone starting cold

⚙ **Rewritten 2026-10-02.**

**Hour one — the state and the result.**

1. **§0 of this document.** Everything current: branches, results ledger, conclusion, rules,
   next steps. If you read nothing else, read §0.
2. **`README.md` on `main`** — the public version of the classic result (two minutes).
3. **`reports/phase2_portfolio_market_maker.md`** — the classic headline, latency caveat first.
4. **§22 of this document** — the latency and book-source robustness that changed the
   conclusion. Then **§23** — the fork's two NO-GO results and the combined conclusion.

**Hour two — why it is believable.**

5. **`DECISIONS.md` #17 and #19** — three bugs that manufactured profit, caught by disbelieving
   good numbers. Then **#20–#24** — everything decided from 2026-09-30 on.
6. **§17.4 and §17.8** — how a profitable run was shown to be a directional bet, and the method
   that found the bugs.
7. **`reports/data_integrity.md`** — the corpus verdict; and **§21.3** — how the holdout and the
   configuration log are enforced in code.

**Before touching code.**

8. **§21** — how the portfolio simulator, the driver and the guardrails actually work, flag by
   flag, and how to reproduce every number in §0.4.
9. **§25** — operational traps on this machine (memory, long runs, process-matching, buffering).
10. **`src/bnfmm/sim/fills/depletion.py` module docstring** — the central epistemic argument (why
    a 200 ms snapshot feed cannot support a true queue model) and the signed list of what the
    fill model over- and understates. Then **`src/bnfmm/sim/portfolio.py`'s docstring**.
11. **§1–§5** — framing and binding constraints. **§4** only if you will touch the Dhan API
    (capture is closed). **§10–§12** — the mathematics of fair value, evaluation and costs.

**History, when you need it.** §13–§19 are the dated record of open items and plans; each has a
⚙ status banner. `reports/phase1_market_maker_results.md` is the Phase 1 deliverable.
Appendix A holds superseded status snapshots.

**A note on the comments in this codebase.** They are unusually dense, and deliberately so:
they record *why* a thing is the way it is, especially where the obvious implementation is
wrong. `config/capture.yaml`'s cap arithmetic, `channels.py`'s two-events-one-exception
note, `store.py`'s rename-per-flush rationale, `market_calendar.yaml`'s empty-list
justification, `depletion.py`'s `_swept` three-conditions docstring and `backtest.py`'s
prevailing-midpoint note are all load-bearing. **Read them before changing the code they sit
above.** Two of them — `_swept` and the prevailing-midpoint note — exist because the obvious
implementation was tried first and manufactured profit (`DECISIONS.md` #17).

---

## 17. Phase 1 — the market maker, built and measured

> ⚙ **Status 2026-10-02: HISTORY.** Phase 1 is concluded and superseded by the portfolio market
> maker (§20–§22). Its code (`strategy/quoter.py`, `sim/backtest.py:run_day`,
> `scripts/backtest.py`) is still on `main` and still runs; `scripts/backtest.py` now goes
> through the holdout guard and the config log. Every diagnosis below — pick-off, inventory skew
> as the strategy, the cost floor — was confirmed and then addressed in §20.2. §17.8's bug
> stories are still the strongest methodological content in the repo.

**Built 2026-08-26 → 2026-09-03. Run on the two sessions available at the time. The result is
negative.** Long form in `reports/phase1_market_maker_results.md` (230 lines); reasoning in
`DECISIONS.md` #17, #18, #19. This section is the briefing-level account, with the parts a
colleague needs in order to continue rather than re-derive.

### The answer, in one paragraph

A two-sided quoter was run against real captured order books for the two highest-information
sessions available — **2026-08-24 (1 DTE)** and **2026-08-25 (0 DTE, expiry day)** — at the
at-the-money strike, both legs. **It does not work.** The single profitable configuration found
(member net **+₹92,433**) earned **58% of its gross PnL from directional inventory rather than
from quoting**, and the same configuration lost **₹207,712** the next day. With the directional
component squeezed out by tighter inventory skew, the result goes to **−₹4,664** and then
**−₹54,582**. **Spread capture never covers costs in any configuration tested, on either day.**
On Dhan's ₹20 flat brokerage every configuration loses heavily at one-lot size.

The finding is not "the strategy needs tuning". It is that **the statutory cost floor consumes
most of the available spread** at the only strikes with enough flow to matter, and what remains
is smaller than the measured adverse selection.

### What is real and what is simulated

§2's labelling constraint is binding, so this comes before the numbers.

| Component | Status |
|---|---|
| Order books, 20 levels, both days | **Real.** Captured live from Dhan's depth feed |
| Trade tape | **Derived** from real data — differenced cumulative volume. Lossy; see §17.1 |
| Aggressor side of each trade | **Inferred.** Lee-Ready quote rule with tick-test fallback; not published by the feed |
| Forward price | **Derived** from real option books by cross-strike put-call parity |
| Our own fills | **Simulated.** No order was ever placed |
| Costs | **Real published rates** (`config/costs.yaml`) — **pending the verification in §13.10** |
| Index spot | **Absent.** Never captured (§9.4) |

**No live capital was deployed, no order was placed, and no trading endpoint is wired up
anywhere in this repository** — `tests/test_no_order_placement.py` (23 tests) enforces it, and
`set_ip`/`modify_ip`/`get_ip` are asserted unreachable from `src/`.

### 17.1 What was built, and the constraint that shaped it

**⚠ Read `DECISIONS.md` #17 before trusting any fill number here.** Two bugs in this pipeline
each manufactured profit and each was caught by disbelieving a good result rather than by a
test (§17.8). The pipeline is one week old and has 31 tests behind it (§13.2).

The chain, in execution order, all new since the last revision of this document:

| Stage | Module | Lines | What it does |
|---|---|---|---|
| 1 | `book/reconstruct.py` | 281 | Parquet frames → `BookSeries` on one clock. LOCF alignment via `searchsorted(..., side="right") - 1` — the only causally honest join. `usable()`, `best_bid`/`best_ask`, crossed/one-sided flags |
| 2 | `book/tape.py` | 225 | Recovers a trade tape by differencing the quote feed's cumulative volume. Lee-Ready classification, `FILLS_BID = SELL`, `FILLS_ASK = BUY` |
| 3 | `fairvalue/microprice.py` | 105 | $(\text{bid}\cdot Q_{ask} + \text{ask}\cdot Q_{bid})/(Q_{bid}+Q_{ask})$ |
| 4 | `fairvalue/parity.py` | 308 | Cross-strike consensus forward from all 23 strikes, precision-weighted $1/(s_C+s_P)^2$ and clipped into the bound intersection |
| 5 | `strategy/quoter.py` | 199 | `theo`, `skew`, `make_quotes`. Nine **named** stand-down reasons; tick rounding always *away* from the market |
| 6 | `sim/fills/depletion.py` | 422 | The fill model. 22 tests |
| 7 | `sim/backtest.py` | 454 | `run_day`, `huang_stoll`, `decompose_pnl`, `apply_costs`. 9 tests |
| — | `scripts/backtest.py` | 430 | The CLI that drives all of it |

**The constraint that shaped every one of those choices.** Dhan's 20-depth feed pushes a
**complete snapshot of every subscribed instrument on a fixed ~200 ms cadence** — median
**201 ms**, p05 157 ms, p95 442 ms — not an event-driven delta stream. Payload sizes are all
multiples of 332 bytes and the median payload is exactly 96 frames = 48 instruments × 2 sides.

So between two snapshots an unknown number of adds, cancels and trades collapse into one net
difference per level. A level going 120 → 90 units is equally consistent with a 30-unit trade, a
30-unit cancel, or a 90-unit trade plus a 60-unit add. **True queue-position modelling is
therefore unsupportable on this data**, and the three-variant `naive`/`touch`/`queue` plan of
§14 Phase 3 collapsed into one honest model: snapshot-to-snapshot depletion, with the
optimistic/pessimistic queue convention as a *parameter* rather than a separate model.

This is the project's central epistemic result, and it is a *downgrade in ambition and an
upgrade in honesty*. A simulator claiming queue positions from this feed would be inventing the
one thing the data cannot supply, and inventing it in the direction that flatters the answer.

**Three ways the tape is lossy, stated because they bound everything downstream:**

1. Multiple trades inside one 200 ms packet **collapse to a single price** — a sweep that walked
   three levels is booked at the final price, crediting a resting order there with volume that
   executed elsewhere.
2. The aggressor side is **not published** and is inferred. Volume that cannot be classified is
   **discarded, not guessed** — 8.3% of units on the 1 DTE at-the-money call, 1.2% on expiry day.
3. `last_trade_epoch` has only **one-second** resolution.

**What the fill model overstates:** hidden/iceberg orders make `queue_ahead` a lower bound;
orders joining our level between snapshots are invisible; interval volume is attributed to the
last print's price. **What it understates:** unclassifiable volume is dropped; we always join the
*back* of our price level, never the front; and `RestingOrder.queue_ahead` is fixed at placement
and depleted only by *traded* volume, so **cancels ahead of us never help us** — which in reality
they genuinely do. Fills during a feed gap are refused outright (`max_stale_ns`, default 1 s ≈ 2×
the measured p95). **These do not cancel out and are not claimed to**; every one is counted in
`FillStats` per run.

### 17.2 The arithmetic screen, before any simulation

`scripts/strike_economics.py` (143 lines) answers the §11.2 question without simulating
anything, and it is the more durable half of §17.

**Options STT is levied on premium, not notional**, so the round-trip cost floor scales roughly
**linearly with the option's price** while the bid-ask spread does not. That single fact
determines where a market maker can operate. At the 2026-08-24 at-the-money call (median mid
**₹203.60**, median spread **11 ticks**):

| Profile | Lots | Breakeven (ticks) | vs the 11-tick market spread |
|---|---|---|---|
| Member (zero brokerage) | 1 | **9.65** | **88% of the entire spread** |
| Dhan (₹20 flat) | 1 | **41.12** | 3.7× the spread |
| Dhan | 5 | 15.95 | 1.45× |
| Dhan | 20 | 11.23 | 1.02× |

The cost is also **asymmetric**: at ₹190 premium a **sell** fill costs ₹10.95 against a buy
fill's ₹2.57 — **4.3×** — because STT falls on the sell side and is **78% of that ₹10.95.**
(Which is why §13.10 is the highest-leverage unverified fact in the repo.)

Screening all 46 contracts per day on arithmetic alone:

* **Member profile:** 45/46 clear the floor on Aug 24, 46/46 on Aug 25.
* **Dhan profile:** 10/46 and 9/46.

**But headroom without flow is worthless.** The deep out-of-the-money puts show +150 ticks of
headroom precisely *because* nobody trades them. Ranking by **headroom × flow** puts the
near-the-money strikes on top with a ratio of only **1.2–1.4× the floor**. That is the honest
picture, and it is the sentence to remember from this whole section:

> **The strikes with room have no flow, and the strikes with flow have almost no room.**

Note what this means for §14's "spread survey" deliverable: it was the one question Phase 0 could
not answer, and it is now answered — *thin, and negative once adverse selection is charged.*

### 17.3 The results

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

Read the last two columns together. **Adverse selection exceeds the effective spread captured at
every single width**, so the realised half-spread is negative throughout. Widening does not
escape it — you simply trade less, at worse selection. The member column is negative all the way
down and merely approaches zero as volume collapses toward nothing. **There is no width at which
this turns profitable.**

Also note the `eff/unit` column tracks the quoted half-spread almost exactly (2 ticks → +0.1072,
4 → +0.2068, 6 → +0.3065, …). That is not a result, it is a **validation**: it is what the column
must do by construction, and it did *not* before the two bugs in §17.8 were fixed.

**Inventory pinned at the cap.** On the one profitable run the quoter sat at **maximum short
calls 45% of the day and maximum long puts 40%** — a synthetic short forward. This is the direct
consequence of §10.4's decision to use inventory skew *as* the delta hedge:

> **When inventory skew is the only control, the position limit becomes the strategy.**

§11.6 already names "fraction of time pinned at the limit (high ⇒ the limit is the strategy)" as
a diagnostic. It fired.

### 17.4 Why the one profitable run is not a result

The parity anchor's **+₹92,433** was attacked before being reported. Two candidate explanations:
one ruled out, one confirmed.

**Ruled out — self-reference.** The consensus forward is built from all 23 strikes *including the
pair being quoted*, so the leg's own book feeds the value that prices it. The algebra says this is
conservative rather than circular:

$$\text{adj} = \text{fair value} - \text{micro} = D\,(F - F_K) = D\,(1 - w_K)(F_{\text{rest}} - F_K)$$

Including yourself scales the adjustment by $(1 - w_K) < 1$ — it can only **shrink** the signal
toward the option's own microprice. But the near-the-money pair carries the ladder's largest
precision weight, so $w_K$ is not small and the algebra alone is not decisive. `--exclude-self`
rebuilds the forward from the other 22 strikes: **+₹93,112**, versus +₹92,433 with self included.
**Unchanged, and marginally better, exactly as predicted. Not circularity.**

**Confirmed — it is a directional bet.** Gross PnL telescopes exactly:

$$\text{gross} = \sum_i s_i (M_T - P_i) = \underbrace{\sum_i s_i (M_i - P_i)}_{\text{spread capture}} + \underbrace{\sum_i s_i (M_T - M_i)}_{\text{inventory PnL}}$$

| | Spread capture | Inventory PnL | Inventory share |
|---|---|---|---|
| 57400 CE | 33,201 | 82,248 | **71%** |
| 57400 PE | 40,816 | 19,649 | 33% |
| **Total** | **74,017** | **101,897** | **58%** |

**Spread capture of ₹74,017 against member costs of ₹83,481: the quoting itself loses money.**
Every rupee of profit came from inventory, on a day the market fell.

Three independent confirmations:

* **Skew sweep** — squeezing the directional exposure out destroys the profit:
  **+₹92,433 → −₹4,664 → −₹54,582** as skew goes 0.5 → 2.0 → 6.0. Spread capture never covers
  costs at any setting.
* **Day two** — the identical configuration loses **₹207,712**.
* **All four day × anchor cells** — inventory PnL dominates gross in every one.

**Two days cannot distinguish a correct directional position from luck.** This is reported as a
*failure to demonstrate market making*, not as a strategy. The identity is now computed for every
run and pinned by an exact-residual test (`tests/test_pnl_decomposition.py`, 9 tests including a
brute-force cross-check), because **reporting gross PnL alone would have called this a working
market maker.**

### 17.5 Diagnosis: picked off, not inventory risk

Adverse selection measured at **four horizons** separates two diagnoses that a single horizon
conflates. Aug 24, 6 ticks, microprice anchor:

| Leg | 1 s | 5 s | 30 s | 300 s |
|---|---|---|---|---|
| CE | +0.2829 | +0.2540 | +0.3223 | +0.2219 |
| PE | +0.3461 | +0.3228 | +0.3486 | +0.3129 |

**Flat across horizons** — already at full magnitude within one second and no larger at five
minutes. That is the signature of being **picked off**: we buy ~0.30 below the mid and the mid
immediately falls ~0.28, permanently. It is *not* inventory risk, which would **grow** with the
horizon and would be fixable by flattening faster.

The two diagnostics agree, which is what makes the conclusion hard to argue with:

* The horizon curve says the problem is pick-off, so **flattening faster will not fix it.**
* The skew sweep (§17.4) independently shows that **flattening faster destroys the PnL.**

⇒ **There is no configuration of this strategy that works on this data.** The fourth horizon
(300 s) is the one that made this legible and was not in the §11.1 plan; it earned its place.

### 17.6 What would have to change

* **Fee structure is decisive, not incidental.** Nothing works on Dhan's flat ₹20 at one lot. At
  20 lots the flat fee amortises to 11.23 ticks against a 9.65-tick member floor — but **20 lots
  is also the exchange freeze quantity** (601 units), so that is the ceiling, not a starting
  point. The honest framing for a writeup is that this strategy requires exchange membership,
  and saying so is a result.
* **The strategy must stop being picked off**, and §17.5 says quoting wider will not achieve it.
  That points at **conditioning on order-flow imbalance before quoting** rather than quoting
  symmetrically at all times — i.e. §11.3's "quote selectively" lever, whose signature is
  **PnL-per-round-trip rising as fill count falls**. **`imbalance()` already exists**
  (`fairvalue/microprice.py:58`) and is called from nowhere — see §18.2.
* **Size, because brokerage is fixed per order.** Already quantified in §11.3 and
  `analysis/quote_size.py`; it moves the Dhan column and does nothing for the member column,
  which is the one that was already marginal.
* **An Avellaneda–Stoikov baseline**, which was planned and not built (§14 Phase 4). Without it
  §11.3's genuinely original point — that A–S assumes no fixed per-trade cost and therefore says
  nothing about the size trade-off — has nothing to be measured against.
* **More days.** Two sessions of one expiry cannot separate skill from a directional bet, which
  is precisely what §17.4 ran into. This is the cheapest of all the levers, because the data is
  already on disk — see §17.7.

### 17.7 The seven unanalysed September sessions

**This is the most valuable unexploited asset in the repo.** Nine sessions are captured (§9.5);
**only two have been analysed.** The seven September sessions — Aug 27, Aug 28, Aug 31, Sep 1,
Sep 2, Sep 3, plus the broken Aug 26 — are on disk, reconciled, and completely unexamined.

What they would settle that two days cannot:

1. **Whether the directional profit was luck.** §17.4's argument rests on one day going the other
   way. Six clean days of the same configuration would replace an argument with a distribution —
   and per §11.5, moving to **per-round-trip** observations ($N \sim 10^4$–$10^5$) rather than
   per-day makes that statistically meaningful, where a daily Sharpe on 21 sessions never can be.
2. **Whether the cost-floor finding is a 0-1 DTE artefact.** Both analysed days are near expiry
   on the *August* series, where premium is low and the floor in ticks is correspondingly low.
   The September sessions run **~34 DTE down to ~26 DTE** — higher premium, so a *higher* floor
   in absolute rupees but a different ratio to the spread.

   **The standing prediction is that the ratio is roughly invariant, so the finding survives.**
   Statutory cost is a fixed *percentage of premium* (≈0.25–0.30% round trip, §18.1), and the
   bid-ask on a liquid ATM index option is empirically a similar percentage of premium — at 1 DTE
   it was ₹0.55 on ₹203.60, i.e. 0.27%. If a 30 DTE ATM shows ₹2.00 on ₹800, that is 0.25%: the
   same. **Both the numerator and the denominator scale with premium, so moneyness and DTE mostly
   cancel.** Record this prediction before looking, then check it — a confirmed prediction is a
   much stronger claim than the same number found by inspection, and a refuted one is more
   interesting still.
3. **The regime breakdown** §11.6 asks for (DTE bucket, realised-vol tercile, time of day) needs
   more than two days by definition.
4. **A genuine out-of-sample test.** Aug 24/25 are now thoroughly in-sample — every parameter in
   §17.3 was chosen by looking at them. The September sessions are the only clean holdout that
   will ever exist for this project, and §11.4's `data/holdout.py` and `config/frozen/` were
   built for exactly this and **do not exist yet** (§14 Phase 5).

**Do these three things first, in order:**

1. **Close §13.11** — Sep 2 and Sep 3 have no capture manifest and no contracts sidecar, so their
   strikes are unlabelled and the September series prunes from the master after 2026-09-29. This
   is the only item with a hard deadline.
2. **Build the §14 Phase 1c data-quality pass**, at minimum the per-id "every subscribed id
   produced rows" check and a session-completeness check. §13.12 exists because this was skipped;
   do not analyse seven sessions the same way.
3. **Freeze the parameters from §17.3 before looking at the new days**, in writing, in
   `config/frozen/`. It costs ten minutes now and is the difference between a holdout and a
   claim about a holdout.

Then re-run §17.3's grid unchanged across all eight usable sessions.

### 17.8 The method that found the bugs, which is the transferable part

Three bugs were found in one week. **None was found by a failing test.** All three were found by
treating a favourable number as a bug report until it survived an attack. Recording the method
because it is more reusable than any of the fixes (`DECISIONS.md` #17, #19).

**Bug 1 — the sweep rule filled us for free.** The first implementation tested only whether the
touch had moved past our price (`best_bid < order.price`), which is **permanently true for any
quote resting inside the spread**. The quoter improves the book **79.5%** of the time on the 1 DTE
ATM call (median book spread 11 ticks against a 2-tick half-spread), so a single eligible print
filled an entire order, bypassing `queue_ahead` completely. **15,218 of 18,012 fills (84%) were
sweeps.**

*The tell was not the PnL.* It was an **effective half-spread of −0.0493**: a market maker cannot
be paid a negative spread on its own quotes, so the number was **structurally impossible**. The
repair needs three conditions jointly — the queue ahead must have cleared, the interval must
contain actual prints (a level that vanished with no prints was *cancelled*, not executed), and
the touch must have moved through. Sweeps fell to **1,925**. See `DepletionSimulator._swept`,
whose docstring records all three and why each is necessary.

**Bug 2 — Huang-Stoll marked fills after the move they caused.** With bug 1 fixed the effective
half-spread was still **−0.0489**. Fills were being marked at `mark[i]`, the book at the *end* of
the interval the fill happened in — and that move is partly the market impact of the very flow
that filled us. Charging it to the effective spread makes the term that measures what we were
*paid* absorb the adverse selection instead. Huang-Stoll's $M_t$ is the **prevailing** midpoint;
Lee-Ready lag theirs for the same reason. Marking at `mark[i-1]` moves the move into the
realised-spread term, where it belongs.

*The validation that both fixes are right* is not that the sign flipped. It is that the effective
half-spread then **tracks the quoted half-spread almost exactly across the whole sweep** — which
it must do by construction, and did not before.

**Bug 3 — a NaN closing mark silently valued open positions at zero.** `Position.unrealised`
returns 0.0 for a non-finite mark. Expiry-day books go one-sided into the 15:30 close, making the
microprice NaN — so on 2026-08-25 the **57500 PE ended long 150 units marked at NaN and booked at
zero PnL** while the forward sat below the strike, i.e. the position had real intrinsic value and
the reported loss was **understated**. `run_day` now falls back to the **last finite microprice**
— a price the book actually showed, rather than an extrapolation or a settlement value that was
never captured — and records `final_mark_stale_ns` so the report discloses it. On Aug 25 the
57500 PE marks at ₹0.05, **210.8 s stale**. Pinned by
`test_a_dead_closing_book_marks_at_the_last_finite_price`.

**Why this matters beyond the three fixes.** The method worked three times and **is not a
method** — it depends on someone noticing. §13.2's asymmetry (400+ tests on the byte-moving layer,
31 on the layer that produces conclusions) is the structural version of the same problem. The
next bug of this class will be found the same way or not at all.

### 17.9 Limitations, and where they are written down

`reports/phase1_market_maker_results.md` §7 is the canonical list and **§17 does not supersede
it**. Read it before quoting any number above. In summary: the fill model cannot represent queue
position (§17.1); the tape is lossy in three known ways (§17.1); **there is no index spot**
(§9.4); the **0.15% STT rate is unverified** (§13.10); the closing mark can be stale (§17.8);
and **two days, one strike, one expiry — nothing here generalises.**

**One limitation that has since been closed.** `RunResult.n_orders`' docstring concedes that
charging brokerage per *fill* overstates Dhan's per-*order* fee whenever one order fills in
pieces. Measured on the 6-tick Aug 24 run, **it never happens**: 602,220 units over 20,074 fills
is exactly 30.0 units per fill, i.e. one lot, i.e. one whole order every time. The reported
member↔Dhan gap of **₹473,746 is exactly 20,074 × ₹20 × 1.18**. So the Dhan figure is **not** an
upper bound with slack in it — it is tight, and there is no recovery available from this
direction. (The member column has no fixed fee and was never affected.)

Data gaps, handled by standing the quoter down when the book is stale and visible in the
per-run stand-down attribution:

| Day | Coverage | Gaps > 2 s | Total | Largest |
|---|---|---|---|---|
| 2026-08-24 | 09:27:01 → 15:34:59 | 6 | 51.1 s | — |
| 2026-08-25 | 09:15:02 → 15:34:59 | 27 | 367.2 s | 88.1 s, clustered in the first 25 min |

Two further limitations that §17 adds and the report does not state as such:

* **The §5.1 reporting guarantee is bypassed.** `scripts/backtest.py` does not route through
  `PnlGrid`, so the cancellation/queue convention appears as a CLI flag (`--optimistic-queue`)
  rather than as a mandatory column. **The §17 numbers are not reported to the standard this
  document sets** (§5.1, §13.13).
* **Every number is in-sample.** The parameters in §17.3 were chosen while looking at the two
  days they are reported on, the guardrail machinery of §11.4 does not exist, and the number of
  configurations tried was not systematically logged. §17.7 item 3 is the fix.

### 17.10 Reproducing

```bash
.venv/bin/python scripts/strike_economics.py --date 2026-08-24
```

```bash
.venv/bin/python scripts/backtest.py --date 2026-08-24 --strikes 1 --lots 1 --half-spread 6 --anchor parity
```

```bash
.venv/bin/python scripts/backtest.py --date 2026-08-24 --anchor parity --exclude-self
```

```bash
.venv/bin/python scripts/backtest.py --date 2026-08-24 --sweep 2 --sweep 4 --sweep 6 --sweep 8 --sweep 12 --sweep 16 --sweep 24
```

Other flags worth knowing: `--strike` (absolute, repeatable) and `--leg ce|pe|both` to pick
contracts; `--max-position` and `--skew` for the inventory controls; `--optimistic-queue` for the
front-of-queue convention; `--json` to dump the full result. All of it reads Parquet from disk —
**no API call, no entitlement, and no possibility of placing an order.**

---

## 18. The route to breakeven — plan of record, 2026-09-04

> ⚙ **Status 2026-10-02: EXECUTED — here is what each step became.**
>
> | Step | Outcome |
> |---|---|
> | 0. Verify STT | Done: 0.15% is correct (§13.10). No easy rescue from the cost side |
> | 1. Cost floor in the gate | Done, in a better form: per-side cost from the rate table + a spread gate in bp of premium (37.8 bp, derived) — §20.2 |
> | 2. Imbalance conditioning | Measured and rejected: textbook sign confirmed but too weak; the falsification test failed — §20.3 |
> | 3. Choose contracts with the screen | Superseded: quote the whole ±11 band and let the bp spread gate choose, causally, at each instant |
> | 4. Size up for Dhan | Not done (member profile was the target; Dhan reported) |
> | 5. Gap protocol and β/R² | β/R² built into every run (`sim/portfolio.summarise`); the flatten-on-reconnect protocol of §19.4 was not built |
> | 18.5 protocol | Done exactly: frozen split, config log, both cost columns, capture/inventory split — §21.3 |
> | 18.6 if no breakeven | Breakeven reached at zero latency, lost at 500 ms (§22) — the §18.6 "requires membership" framing became "requires co-location" |
>
> Two new structural changes not anticipated here turned out to matter most: one shared
> portfolio (Phase 1 ran legs independently) and keeping queue priority (Phase 1 re-placed every
> 200 ms). §20.2.

**Why this section exists.** The project's value as a portfolio artifact is materially higher if
the market maker is at least marginally profitable, and the user has asked for that. This section
is the plan. It is written as a plan and not as a result: **nothing in §18 has been run.**

Two things make the ask legitimate rather than a request to torture the data. First, the measured
gap is small — §18.1. Second, the levers that would close it are **already written in this
repository and connected to nothing** — §18.2. What follows is wiring up existing code and
measuring the result, not inventing a new strategy until one appears to work.

The way this goes wrong is §18.5, and it is the only part of this section that is not optional.

### 18.1 How far from breakeven the strategy actually is

§17.3's headline invites the reading that the strategy is hopeless. On the parity anchor it is not:

```
spread capture (2026-08-24, parity, 6 ticks)   Rs  74,017
member costs on the same run                   Rs  83,481
                                               -----------
deficit                                        Rs   9,464   = 89% of the way there
```

**₹9,464 over ~602,000 units is ₹0.016/unit, about 0.3 ticks per fill.** That is the distance to
breakeven on the effective-spread line, and it is small enough that the §18.2 levers are
plausibly sufficient.

**The single number that explains the whole instrument:**

```
sell-side STT  = 0.15% x Rs 203.60 premium  = Rs 0.3054 / unit
effective half-spread earned at 6 ticks     = Rs 0.3065 / unit
```

**STT on the sell leg alone almost exactly equals the entire half-spread captured.** Total
round-trip statutory cost is ≈0.25–0.30% of premium; the 1 DTE ATM spread was ₹0.55 on ₹203.60,
i.e. 0.27%. The two are the same size. Everything else in §17 follows from that coincidence.

Which is why **§13.10 outranks every item in §18**: at 0.10% instead of 0.15% the sell-side STT
is ₹0.2036/unit, the same run clears comfortably on the member profile, **and no strategy change
is needed at all.** Verify the rate before writing a line of new code. It is a 20-minute task
that could move the headline result on its own, and the current 0.15% in `config/costs.yaml` is
an unverified transcription (§13.10).

⚠ Read the deficit above with its caveat. Spread capture is measured **before** adverse
selection, which sits in the inventory term. On Aug 24 the inventory term was *positive* because
the market fell while we were short; in expectation it is negative. So "89% of the way there"
describes the effective-spread line only, and a fair-coin day is further from breakeven than
₹9,464. It is still the right target to attack, because it is the term a strategy can influence.

### 18.2 Three levers already written and connected to nothing

This is the finding that makes §18 worth doing, and it was not known when §17 was written.

| Lever | Defined at | Called from |
|---|---|---|
| `imbalance()` | `fairvalue/microprice.py:58` | **nowhere** |
| `depth_weighted_micro()` | `fairvalue/microprice.py:73` | **nowhere** |
| `CostModel.breakeven_ticks()` | `sim/costs.py:313` | reporting scripts and `analysis/quote_size.py` — **never the quoter** |

The third is the sharpest. `QuoteParams.half_spread_ticks`' own docstring says the edge "must
clear the round-trip cost floor… so the floor is computed per contract by `sim.costs` rather than
assumed here." The gate that actually runs is [quoter.py:169](src/bnfmm/strategy/quoter.py:169):

```python
min_book_spread_ticks: float = 1.0
...
if book_spread < params.min_book_spread_ticks * tick - 1e-9:
    return down("book_tighter_than_edge")
```

**The strategy computes its own cost floor for the report and ignores it when deciding whether to
quote.** It quotes into 2-tick books that cannot pay a 9.65-tick floor. The placeholder is a
plausible-looking constant, which is why it survived review.

And a fourth, structural one: `iter_snapshots` hands out all 20 levels, and
`microprice` takes `x[:, 0]`. **41 GB of 20-level depth is being consumed as a top-of-book feed
— 19 of 20 levels are unread.** The two dead functions above are exactly the ones that would use
them.

So the honest position is not "the strategy needs new ideas." **The disease diagnosed in §17.5
already has its remedy sitting uncalled in the source tree.**

### 18.3 Why adverse selection ≈ effective spread, mechanically

§17.3 notes the ratio is above 1 at every width without explaining why. The explanation is what
the §18.4 gate fixes, so it belongs here.

At a 6-tick half-spread the quoter posts a **12-tick-wide market against a median 11-tick book**.
So it is sitting *at or just outside* the touch. A quote outside the touch is not filled by
uninformed flow shopping the spread — **it is filled only when price travels to it**, which is
the definition of adverse selection. Hence `adverse ≈ effective`, at every width, by construction
of where the quotes sit.

The escape is not a wider quote (§17.3 proves that) but a **narrower market that is still above
the floor** — i.e. only competing when the book is wide enough to sit *strictly inside* it with
edge above the per-side cost floor. That is a joint condition on the book spread and the floor,
and it is precisely what the placeholder gate fails to express.

### 18.4 The plan, in order

**Step 0 — verify the STT rate (§13.10).** Gates the interpretation of everything below. Highest
leverage per minute in the entire project.

**Step 1 — wire the cost floor into the gate.** Replace `min_book_spread_ticks` with a
per-snapshot requirement that the book spread exceed
`breakeven_ticks(premium, n_lots) + 2 × half_spread_ticks`, so the quoter competes only when it
can rest strictly inside the book with edge above its own floor. Add the per-contract premium and
lot count to the quoter's inputs; the function already exists and is already tested
(`tests/test_costs.py`, 8 assertions on it).

**This is not a fitted parameter.** It is "do not trade below cost," it is derived from published
charges, and it survives an interviewer asking where the number came from. Expect fill count to
fall sharply and PnL-per-fill to rise — which is §11.3's predicted signature.

**Step 2 — condition on depth imbalance.** Use `imbalance(bid_qty, ask_qty, levels=5)` to pull or
widen the side the book is about to run through. §17.5 says the loss is pick-off, and adverse
selection exceeds effective spread by only ~9% at 6 ticks, so a modest reduction flips the
realised half-spread positive.

**There is a falsification test, and it must be reported.** If imbalance-conditioning is real,
the **realised** half-spread rises while the **effective** half-spread stays put. If both rise
together, the rule has merely quoted wider in disguise, and §17.3 already showed where that leads.
Report both columns for every variant.

**Step 3 — choose contracts with the screen that already exists.** `strike_economics.py` ranks by
headroom × flow and found strikes at **1.2–1.4× the floor** — and **every backtest in §17.3 ran
`--strikes 1`, ATM only.** The screen's output was never fed back into the strategy. 20–40% of
headroom is exactly the order of magnitude the §18.1 deficit needs.

**Step 4 — size up, for the Dhan column only.** Verified arithmetic:

| Quote size | Brokerage per unit | vs the ₹0.3065 captured edge |
|---|---|---|
| 1 lot (30 units) | ₹0.667 | **2.18× the entire edge** |
| 20 lots (600 units, the freeze cap) | ₹0.033 | 0.11× |

Already supported: `--lots 20 --max-position 200`, no new code. **It does nothing for the member
column**, where every charge is proportional — so this is a narrative fix for the retail case,
not a route to breakeven. Note that 20 lots accumulates inventory 20× faster per fill, so the
position cap must scale with it, and the depletion model will produce partial fills — which is
more realistic and worth reporting as such.

**Step 5 — the gap protocol and the β/R² diagnostic**, §19. Additive: they change no existing
number, so the §17.3 grid stays comparable.

### 18.5 The protocol that makes a positive number worth anything

⚠ **This is the non-optional part.** Steps 1–4 are five knobs on two days of data. Swept until
Aug 24 prints positive and reported as a result, they produce a number that dies to the first
competent question — *how many configurations did you try?* — and takes the credibility of the
rest of the repo with it. That, not the negative result, is the actual risk to the CV.

Before touching any knob:

1. **Freeze the holdout in writing**, in `config/frozen/` — the machinery §11.4 specified and §14
   never built. Proposed split: **develop on Aug 24, 25, 27, 28; hold out Aug 31, Sep 1, 2, 3.**
2. **Log every configuration tried**, cumulatively, and report the count. A number quoted with
   "from N configurations" is evidence; the same number without N is an anecdote.
3. **Keep both cost columns on every table** (§5.1, §12), and keep reporting the
   capture-vs-inventory split (§17.4) so a new positive number is decomposed the same way the old
   one was — the decomposition is what caught the first false positive and it must not be dropped
   the moment it starts saying something welcome.

**"Marginally profitable out-of-sample on four unseen sessions, from N configurations" is worth
an order of magnitude more than "profitable", and it is the only version that survives scrutiny.**

### 18.6 If it does not reach breakeven

A real possibility, and worth deciding the response now rather than under pressure. The statutory
floor is untouchable by any strategy change; if 0.15% is confirmed, the §18.1 arithmetic may
simply preclude a passive symmetric quoter at retail cost levels.

In that case the defensible result is: **"passive two-sided quoting in Indian index options
requires exchange membership, and here is the measurement that shows why"** — the cost-floor
screen (§17.2), the pick-off diagnosis (§17.5), and the fee-structure decomposition (§12). That
is a genuine, quantitative, falsifiable finding about a real market.

And the two simulator bugs (§17.8) remain the strongest single item in the repo either way.
Almost every profitable student backtest is an undetected look-ahead bug; this one has the
documented story of catching two, by disbelief rather than by luck. **Do not trade that away for
a positive number obtained by the route §18.5 forbids.**

---

## 19. Data gaps — what they do to PnL, and the protocol

> ⚙ **Status 2026-10-02.** Implemented: the inclusion rule as a written, mechanical criterion
> (`config/frozen/protocol.yaml`, applied by `scripts/qa_report.py`); the β/R² diagnostic (every
> `mm.py` run). Not implemented: flatten-on-reconnect (§19.4) and formal per-segment simulation.
> The feed watchdog (§19.6.1) is moot because capture is closed. The holdout R² values of
> 0.002–0.18 suggest gap-carried inventory is not a material directional channel for mm_v1.

The capture host is a laptop on domestic power and consumer internet — locked decision 8 in §3,
"capture host: laptop, expect gaps" — and the gaps are real: **Aug 24 has 6 gaps >2 s totalling
51.1 s and does not start until 09:27:01** (twelve minutes of the open lost); **Aug 25 has 27 gaps
>2 s totalling 367.2 s, largest 88.1 s, clustered in the first 25 minutes.** Aug 26 lost the
session outright (§13.12).

This section answers: do they affect reported PnL, and what is the protocol. **Written
2026-09-04; nothing in §19 is implemented.**

**§5.5 already decided half of this and the code does not honour it.** That section makes
*segment*, not session, the unit of analysis: a gap exceeding `max_gap_s` ends a segment and each
segment gets independent QA. **`max_gap_s` appears nowhere in `src/`, `scripts/`, `config/` or
`tests/`, and no module under `sim/`, `book/` or `analysis/` mentions a segment at all.** `run_day`
runs a whole calendar day as one continuous series with a staleness flag. So §5.5 joins §5.1's
`PnlGrid` and §18.2's cost-floor gate as **a decision recorded in this document and absent from
the code** — three instances of the same failure, which is now a pattern worth naming rather than
three separate to-dos.

§5.5 handles gaps at the **data-QA** level (which observations are comparable). §19 handles them
at the **strategy** level (what the position does while blind). Both are needed and neither exists.

### 19.1 What the gaps already cannot do — verified in code

Three of the four channels by which a gap could corrupt PnL are already blocked. Each was checked
rather than assumed:

| Channel | Guard | Where |
|---|---|---|
| Fills invented across a gap | interval refused when `gap > max_stale_ns`, counted in `stale_units_skipped` | [depletion.py:322](src/bnfmm/sim/fills/depletion.py:322) |
| Quoting into a dead book | `stale = (now_ns - prev_ns) > max_stale_ns`, passed to the quoter → `stale_book` | [backtest.py:212](src/bnfmm/sim/backtest.py:212) |
| **The reconnect volume burst** | same staleness guard | `depletion.py:322` |

The third deserves emphasis because it is the subtle one. `tape.py` recovers the tape by
differencing cumulative volume, so **the first snapshot after an 88-second gap carries the entire
gap's volume as a single interval, attributed to a single price.** Unguarded, that would clear any
queue ahead of a resting order in one step and manufacture a large fill at a stale price — a
fill-inflation bug of exactly the class as #17.1. It is caught, and the units are **discarded**
rather than deferred, which is the conservative direction.

### 19.2 The one channel that is not guarded: carried inventory

**Nothing flattens the position.** `run_day` carries inventory straight through the gap: the
quoter cannot quote, cannot be filled, and **cannot skew**, so whatever position is open when the
feed dies rides the entire price move across the gap.

That move lands in exactly one place — `Σ sᵢ(M_T − Mᵢ)`, the **inventory term** of §17.4's
decomposition.

> **The gaps do not add symmetric noise. They inflate the one term the project's credibility
> argument depends on disowning.**

§17.4 dismissed the only profitable run because inventory PnL was 58% of gross. Some unmeasured
share of that 58% is not a strategy decision at all — it is an internet outage.

### 19.3 Three reasons this is worse than it sounds

1. **The position at gap onset is near the cap, not near zero.** §17.3: pinned at max short calls
   **45%** of the day and max long puts **40%**. A randomly-timed gap therefore catches the book
   at maximum inventory roughly half the time — the worst possible moment to lose control. Any
   analysis assuming a typically-flat book at gap onset understates the exposure badly.
2. **The gaps cluster at the open** (Aug 25: 27 gaps in the first 25 minutes), which is the
   highest-volatility, highest-flow, fastest-inventory-accumulation window. The censoring is
   **not random in time**.
3. **Therefore the censoring is correlated with volatility, and it biases every statistic, not
   just PnL.** The surviving sample is disproportionately calm, so **the measured adverse
   selection of 0.335 is likely an *under*statement** — the gaps are hiding the worst of it. This
   cuts against the §17 conclusion, not for it, and should be stated that way.

### 19.4 The protocol — and the look-ahead trap in the obvious version

The right instinct is to flatten when the book has been gone too long. **The obvious
implementation of it is look-ahead, and would be the third profit-manufacturing bug.**

⚠ **You only know a gap's duration after it ends.** Deciding *at gap onset* to flatten, using
knowledge of how long the gap will turn out to last, exits the position before an adverse move
that could not have been seen. That is the same failure mode as #17.1 and #17.2, in the most
flattering direction.

The causally honest sequence:

| When | What happens | Cost |
|---|---|---|
| Gap onset | **Nothing.** No data arrived; that is all that is known. Quoter stands down — already implemented | free |
| Reconnect (first fresh snapshot) | Duration is *now* known. If it exceeded the threshold, **flatten at the post-gap price, crossing the spread, paying full costs** | spread + full costs |
| The gap's price move | **Charged to inventory PnL.** The position was held, the price moved, it counts | — |

So the honest claim for this protocol is **bounding, not repair**: it cannot fix the gap just
crossed, only stop the next one compounding. Describing it as "we flatten on outages" would imply
the damage was removed, and it is not.

**What "too long" is, from the measured data.** Snapshot cadence is median 201 ms / p95 442 ms,
and §17.5 found adverse selection **already at full magnitude within 1 second and flat out to
300 s**. So the informational lifetime of a quote here is under a second; beyond that, control is
already fully lost. But two thresholds are needed, because they cost different amounts:

| Parameter | Value | Action | Cost |
|---|---|---|---|
| `max_stale_ns` | 1 s (exists, ≈2× p95) | stop quoting, refuse fills | free |
| `flatten_after_ns` | **new** | flatten at reconnect | spread + full costs |

Flattening on every 1.1 s hiccup would bleed crossing costs all day. So rather than choosing a
constant — the mistake `min_book_spread_ticks` already made (§18.2) — **sweep it: 1 s / 5 s / 30 s
/ never, and report the inventory-term sensitivity.** Same pattern as the half-spread sweep: an
arbitrary choice becomes a measurement.

**That sweep is also the measurement of the gap damage.** The difference in the inventory term
between `flatten_after = never` and `flatten_after = 5 s` **is** the gap exposure, exactly
attributed, with no estimation. And `never` reproduces today's §17.3 numbers exactly, so the grid
stays comparable.

### 19.5 The diagnostic this is really about: β and R² on underlying returns

The objective behind the protocol is that **PnL should be uncorrelated with market direction**.
§17.4's capture-vs-inventory split is a good instrument but a point estimate on one realised path.
The sharper form is a regression of **PnL increments on underlying returns**:

* **β** — directional exposure in delta units. **β ≈ 0 is the market-neutrality claim**, stated
  as a number instead of an assurance.
* **R²** — the fraction of PnL variance explained by direction. This is *literally* "how much of
  this is a directional bet", and it is computable **within a single day** from per-interval
  increments, so it does not wait on n sessions the way a daily Sharpe does (§11.5).

**Report β and R² with and without the flatten protocol — it discriminates between the two
candidate causes, which no single number can:**

* **R² falls when flattening is enabled** ⇒ the gaps were a material source of directional
  exposure, and §19.4 is the fix.
* **R² does not fall** ⇒ the gaps were never the problem. The directional exposure is
  **inventory-skew-as-delta-hedge** itself (§10.4, §17.3's pinning), which is a design decision
  and needs an entirely different fix.

Either answer is worth having, and getting it costs one regression.

### 19.6 Capture-side minimisation, cheapest first

**1. A feed watchdog — the highest-value operational change in the project.** §15's 09:20 check is
a single glance at one moment. On Aug 26 the feed died at 10:52 and **nobody noticed for four and
a half hours.** An alarm on >30 s with no depth frames converts a lost session into a lost minute.
It is small, and **it is worth more than every analysis improvement in §18**, because a session
happens once and cannot be re-run. Entitlement lapses 2026-09-22, so there are only ~13 left.

**2. Start each session above 90% battery** (§15). Under `systemd-inhibit` the laptop already *is*
the UPS, so a mains cut with a charged battery produces **no gap at all**. One line of discipline
removes the whole power-cut failure mode.

**3. Make uptime a written inclusion criterion, before the September sessions are analysed.**
§13.12 excludes Aug 26 at ~20% of normal volume — correctly, but as a judgement call made after
seeing the data. Turn it into a rule (minimum coverage fraction, maximum total gap seconds,
maximum single gap) and apply it mechanically. Deciding which sessions count *after* seeing which
ones are profitable is a form of the §18.5 problem.

**4. Relaunch on death** (§15) — already doctrine, and Aug 26 shows the cost of not doing it.

### 19.7 Do not smooth the data

The instinct to interpolate across the gaps should be resisted, and this is the one place where
the fix must not touch the input.

Interpolating book states across a gap **fabricates order books that never existed**, and the
fill model would then fill against them — manufacturing fills from invented data. That is the
same failure class as #17.1 and #17.2 but strictly worse, because it lives in the *input*, where
no downstream test can catch it and where every statistic silently inherits it. Note that LOCF
already holds the last book forward across a gap; the only thing that makes that safe is that the
staleness is **tracked and acted on** rather than smoothed away.

> **Fix the strategy's response to missing data. Never fix the missing data.**

A gap must remain visible as a gap, in the arrays and in the report — which is what
`intervals_stale`, `stale_units_skipped` and `stale_pct` already exist for, and what
`final_mark_stale_ns` (§17.8) exists for at the close.

---

## 20. Phase 2 — the portfolio market maker (profitable at zero latency; see §22 for 500 ms)

**Built and evaluated 2026-09-30.** Long form: `reports/phase2_portfolio_market_maker.md`.
Reasoning: `DECISIONS.md` #20–22. Data verdict: `reports/data_integrity.md`.

### 20.1 The result

The frozen strategy `mm_v1` (`config/frozen/mm_v1.yaml`, sha256 `4f144314…`), run once on the
five holdout sessions (2026-08-31 → 2026-09-04, 25–29 DTE):

| | Member | Liquidated at close | Dhan ₹20/order |
|---|---|---|---|
| Net, 5 days | **+₹199,537** | **+₹155,159** | −₹121,494 |
| Days positive | 5/5 | 5/5 | 1/5 |

Spread capture ₹735,694; inventory term −₹222,432. R² of per-minute PnL on the parity forward
0.002–0.18. Per-fill realised PnL net of cost, 95% CI [₹6.34, ₹20.40] (5-minute block bootstrap).
Develop days (Aug 27, 28): +₹63.8k/day; holdout +₹39.9k/day — a one-third degradation.
**From 26 configurations tried**, all on develop days.

This answers §18.6: passive quoting in BANKNIFTY options **can** clear the statutory floor at
exchange-member cost, but only in books whose spread in basis points of premium covers cost plus
adverse selection — mostly the wide in-the-money books, priced off their liquid out-of-the-money
partners by parity. At retail cost and one lot it still loses, as §17.6 predicted.

### 20.2 Why Phase 1 failed and this does not — the four changes

1. **Portfolio delta and vega** replace per-leg inventory skew (§10.4's trade-off, now resolved:
   still no hedge leg, but exposure is netted across strikes and hard-limited).
2. **Queue priority**: unchanged quotes rest instead of being re-placed every 200 ms.
3. **Symmetric cost split**: per-side gating made the book structurally long options.
4. **Spread gate at 37.8 bp** = round-trip cost 23.7 bp + 2 × measured adverse selection 7.05 bp —
   the §18.4 step 1 cost floor, expressed in the units in which cost actually scales.

Plus `min_days_to_expiry: 3`, pre-registered in `config/instruments.yaml` before any data existed,
which excludes the expiry-day regime where the realised spread is zero.

### 20.3 What was tried and did not help

Fresher causally-merged book (depth + quote feed); 5-level imbalance pull (the §18.4 step 2
falsification test failed — realised spread unchanged); vega skew; join-only placement. The L1
microprice proved no better than the plain mid as a price forecast (§10.3's comparison, finally
run: against future prints all estimators are indistinguishable).

### 20.4 Limitations

As in the report: simulated fills on a 200 ms snapshot feed, no latency model, exchange-member
costs, one monthly cycle at 25–33 DTE, one lot, fills ~0.3% of band volume with no impact model,
and a spent holdout. The report's §"Limitations" is canonical.

### 20.5 Reproducing

```bash
.venv/bin/python scripts/qa_report.py
```

```bash
.venv/bin/python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8
```

### 20.6 ⚠ Latency and book-source robustness — the result that qualifies §20.1

Added later on 2026-09-30; full account in `DECISIONS.md` #23 and at the top of the Phase 2 report.
Same frozen strategy, only simulator realism varied:

| | 0 ms | 500 ms | 1000 ms |
|---|---|---|---|
| Develop (Aug 27+28), member net | +127,594 | +13,323 | −33,823 |
| **Holdout (5 days), member net** | +199,537 | **−40,500** | −96,808 |

Trades arrive ≈0.3–0.5 s after they execute, so ≈0.4–0.8 s is the realistic activation delay
for a non-co-located participant. Book source matters too: depth merged with the fresher quote
feed gives +69,666 on develop; quote feed alone −39,520. The longer-dated series (quote feed
only) were therefore not run — the adapter failed validation.

**The honest one-line result for the write-up:** *the quoting logic captures spread and stays
market-neutral, but on this data the edge is smaller than the cost of latency; profitable market
making here is a co-location question, not a strategy question.*

## 21. How the Phase 2 machinery works — enough to change it safely

Everything in this section is on both branches unless marked (rv).

### 21.1 Data QA and the tape fix

**`src/bnfmm/data/qa.py`** — pure functions, arrays in, dicts out (16 tests):

| Function | What it returns |
|---|---|
| `session_bounds_ns(day)` | Epoch-ns of 09:15:00 and 15:30:00 IST |
| `segments(ts, max_gap_s)` | Maximal runs with no gap above `max_gap_s` |
| `coverage(ts, window, max_gap_s=30)` | Fraction of the window inside segments, gap counts, largest gap, list of large gaps. A late start counts as a gap |
| `cadence(ts)` | Inter-arrival p05/p50/p95 in ms (intervals ≤ 5 s) |
| `book_checks(bid_px, bid_qty, ask_px, ask_qty, tick)` | Two-sided / one-sided / crossed / locked / non-monotone %, off-tick and non-positive counts, spread percentiles in ticks |
| `tape_checks(recv_wall, recv_mono, volume, ltp, last_trade_epoch, tick)` | Rewinds, out-of-order, exchange lag (auto-detects the IST-wall-clock epoch, offset 19,800 s), wall-vs-monotonic skew events |
| `touch_agreement(...)` | How often the quote feed's level 1 equals the depth feed's (depth row ≤ 1 s older) |
| `presence(subscribed, present)` | Subscribed ids with no rows — the check that would have caught §9.4 on day one |
| `include_day(depth_cov, feed_cov, ...)` | The frozen inclusion rule |

**`scripts/qa_report.py`** walks every day (10 worker processes by default, ~2 min/day, ~6–7 GB
peak) and writes `reports/qa/<date>.json` (gitignored) plus `reports/data_quality.md`.
`reports/data_integrity.md` is the hand-written verdict on top of it.

**Findings that shape everything downstream** (detail in `reports/data_integrity.md`):

* **Stale-packet replays** — the quote feed occasionally re-sends a packet whose cumulative volume
  and `last_trade_epoch` are older than one already received. Fixed in `book/tape.py`:
  `volume_increments()` differences the counter's **running maximum**, so a replay and the climb
  back from it add nothing. Phantom share was ≤0.01% on nine days and 5.8% on 2026-09-04.
* **Depth feed lag** — measured by shifting one feed's clock against the other and maximising the
  exact level-1 match rate: the peak is at a depth delay of 200–250 ms on every day, hour and
  instrument (futures match 85–93% at the peak; fast option books 40–70%). Constant and
  load-independent, so a vendor property. Constant `book.merge.DEPTH_LAG_NS = 225 ms`.
* **`last_trade_epoch` is IST wall-clock seconds, not UTC** (raw offset −19,800 s). Not consumed
  anywhere except QA. Corrected exchange-to-receipt lag: 0.8–1.0 s median; because the field is
  truncated to the second, the true print delay is ≈0.3–0.5 s — the number behind §22.
* **Benign:** the 33 ms unchanged-packet storm on Sep 4 (09:30–11:30); clock-skew events exactly
  at capture restarts (monotonic clocks are not comparable across processes); duplicate depth
  rows (two pushes in one socket read; the reader keeps the later); a stable futures basis
  (parity forward 8–25 points below the future's mid).

### 21.2 The portfolio simulator — `src/bnfmm/sim/portfolio.py`

**Inputs.** A list of `Leg`s, each with its own clock: `book` (a `BookSeries`), `tape`,
`fair_value` and `delta`/`vega` arrays on the book's clock, contract facts (tick, lot 30, freeze
601), and optionally `stale` (per-interval socket staleness, used by the quote-feed mode).

**The event loop.** Every (leg, snapshot k ≥ 1) is one event; events are processed in global
receive-time order. At each event:

1. **Settle** the interval (t[k−1], t[k]] for that leg: tape prints in the interval go to the
   leg's `DepletionSimulator`; fills update the leg's `Position` (average-cost accounting, from
   `sim/backtest.py`), the portfolio delta and vega, and are recorded with the **prevailing**
   marks `micro[k−1]` and `fair_value[k−1]` (the Huang-Stoll convention, `DECISIONS.md` #17).
2. **Refresh** the leg's delta/vega contribution to the portfolio totals.
3. **Re-quote.** Stand-down checks in order: `stale_book` (gap > 1 s, or the `stale` array),
   `no_usable_book`, `no_fair_value`, `premium_below_floor` (₹5), `spread_below_cost_floor`
   (book spread < `min_spread_bp` of fair value). If nothing that sets the quote changed (same
   touch, same fair value, no fill anywhere since this leg last quoted), the existing orders are
   left untouched. Otherwise, for each side:

   * target: `bid = V − r_b·V − buffer − shift`, `ask = V + r_s·V + buffer − shift`, rounded away
     from the market, where `r_b`, `r_s` are statutory cost per rupee of premium
     (`cost_split="symmetric"`: both = half the 23.71 bp round trip; `"own"`: 4.50 / 19.20 bp);
   * capped at one tick inside the touch (`improve`), or the touch (`join_only`);
   * per-side stand-downs: `would_cross`, `imbalance` (if enabled), `edge_below_cost` (would sit
     more than `max_behind_ticks` behind the touch), `leg_cap`, `delta_limit`, `vega_limit`
     (only the side that would increase the exposure is pulled), `close_only`;
   * **if the resting order already has this price, it is kept** — that is the queue-priority
     change; otherwise it is cancelled (instantly, or after the latency, §22.1) and a new order is
     placed with its queue position read from the current book.

   `shift = delta_skew·(portfolio delta in lots)·leg_delta + leg_skew_ticks·(leg lots)·tick
   + vega_skew·(portfolio vega in ATM-lots)·(leg vega / reference vega)`.

**`summarise(result, forward_t, forward)`** returns: fills, units, orders, swept %; `costs` per
profile with brokerage charged **per order** (fills carry their order id); `gross_micro`,
`gross_fair_value`, `gross_liquidation` (longs closed at the last bid, shorts at the last ask)
and `net_member_liquidated` (also pays the closing trade's statutory cost); `spread_capture` and
`inventory_pnl` (exact split; their sum equals `gross_micro`, tested); Huang-Stoll effective and
realised half-spread at 1/5/30/300 s; per-leg tables; and the **directional diagnostics**: β (₹
per forward point, and in lots) and R² of per-minute PnL increments regressed on parity-forward
changes, mean |delta|, mean vega.

**Fair value and greeks are built by the driver**, not the simulator (`scripts/mm.py`):

* `fair_value(mode="blend")`: precision-weighted average of the option's own L1 microprice
  (weight 1/(own half-spread)²) and its parity value `m_other ± D·(F − K)` (weight 1/(other
  half-spread)²), all carried forward causally. A wide ITM book defers to its tight OTM partner.
* `leg_greeks`: Black-76 implied vol from the fair value every 25 snapshots, then delta and vega;
  where no implied vol exists (value at intrinsic), delta = ±D if ITM, 0 if OTM, vega = 0.
* F is the cross-strike parity forward (`fairvalue/parity.implied_forward`) on the aligned grid.

### 21.3 The guardrails — frozen protocol, holdout loader, configuration log

`src/bnfmm/analysis/holdout.py` (13 tests):

* `load_protocol(path)` parses a frozen YAML (develop, holdout, excluded, expiry map, inclusion
  rule, baseline) and records its **sha256**. It rejects overlapping sets and a holdout that is
  not strictly later than every develop day.
* `require_access(protocol, days, unlock_reason=...)` raises `HoldoutLocked` for any holdout day
  unless a non-empty reason is given, in which case it appends a row (UTC time, days, reason,
  protocol hash, command line) to `reports/holdout_log.md`. Excluded or unknown days raise
  `ProtocolError`.
* `log_config(protocol, record)` appends a JSON line (time, protocol hash, script, config, days)
  to `reports/config_log.jsonl`; `count_configs()` counts **distinct config dicts across all
  scripts**, which is why the RV scripts print a running total that includes the classic runs.

Frozen files and their hashes: `protocol.yaml` `0e865025…`, `mm_v1.yaml` `4f144314…`,
`protocol_rv.yaml` (rv) `9b070f78…`. `protocol_rv_patient.yaml` (rv) is read directly by its
script. `scripts/mm.py` and `scripts/backtest.py` default to the classic protocol; the RV scripts
load `protocol_rv.yaml`.

### 21.4 `scripts/mm.py` — the driver

```
--date D (repeatable; default = develop days)   --unlock-holdout REASON
--band N (strikes either side of ATM, default 11)  --leg both|ce|pe   --levels 10
--fv own|parity|blend (blend)   --book depth|merged (depth)   --source depth|quotes (depth)
--series front|next (front)     --min-dte N (default from config/instruments.yaml = 3)
--lots 1  --buffer TICKS  --cost-split own|symmetric  --join-only  --max-behind TICKS
--leg-cap LOTS (5)  --delta-limit LOTS (5)  --delta-skew (0.5)  --leg-skew (0.5)
--vega-skew (0)  --vega-limit ATM-LOTS (inf)  --min-premium (5)  --min-spread-bp (0)
--optimistic-queue  --latency-ms MS (0)  --imbalance X (1 = off)  --close-only SECONDS
--grid '{"arg_name":[v1,v2]}'  (cartesian product on the same loaded day; each logged)
--tag NAME  --per-leg  --dump-fills
```

Defaults are module defaults, **not** mm_v1. The frozen mm_v1 is:
`--band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8` (all else default).

Per day it loads all front-series depth books in the band (L1 for parity, 10 levels for quoted
legs), builds the forward, fair values and greeks, runs the simulator once per grid variant, and
prints: fills/units/orders; gross under three marks; costs per profile; net member, net Dhan, net
liquidated; capture vs inventory; per-unit capture and cost; Huang-Stoll; β, R², delta and vega;
stand-down counts; busiest legs. Output files (gitignored): `reports/mm/<tag>[-v<i>].json`
(config, params, per-day summaries) and, with `--dump-fills`,
`reports/mm/fills-<tag>-v<i>-<date>.npz` (per fill: t, leg, side, price, qty, mark, later300,
member cost, strike, is_call, delta, spread; plus `fwd_atm`).

**Cost of a run:** depth source, band 11: ~80–100 s to load a day, 10–40 s to simulate; peak memory was not measured separately but
stays within 15 GB when run alone. Quote source: ~270–380 s to load a day, ~7 GB used. A grid of 6 on two days ≈ 25–30 min.

### 21.5 Reproducing every number in §0.4

```bash
.venv/bin/python scripts/qa_report.py
```

```bash
.venv/bin/python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8
```

```bash
.venv/bin/python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8 --grid '{"latency_ms":[0,500,1000]}'
```

```bash
.venv/bin/python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8 --book merged
```

Holdout versions add `--date 2026-08-31 … --date 2026-09-04 --unlock-holdout "REASON"`, which
**appends a holdout read to the log** — do it only when you mean it. On `rv-market-maker`:

```bash
.venv/bin/python scripts/rv_persistence.py
```

```bash
.venv/bin/python scripts/rv_patient.py
```

---

## 22. Robustness of the classic result — latency, book source, longer-dated series

### 22.1 The latency model

`DepletionSimulator(activation_ns=L)` (and `PortfolioParams.activation_ns`, `--latency-ms`):

* a print counts toward an order only if it was **received** at least L after the order was
  placed — so an order cannot trade against prints that executed before it existed;
* **cancels take the same latency** (`cancel_at`): a repriced or withdrawn order stays live, and
  can fill, until its cancel arrives; then it is purged. Fills carry their own order id, so a fill
  on an order whose cancel is in flight is still charged to the right order.

The first version made cancels instant while new orders waited, which leaves a repricing quoter
with *no* order for L after every reprice — biased against the strategy. Its results (develop:
+₹8,585 at 500 ms, −₹2,852 at 1000 ms, with only 24k / 8k units filled) were discarded and are
recorded only here and in `DECISIONS.md` #25. Latency 0 reproduces the pre-latency results
exactly (checked: +₹127,594).

**Which latency is realistic.** Prints reach the capture 0.8–1.0 s (median) after their
exchange timestamp, which is truncated to the second, so ≈0.3–0.5 s after execution. Adding an
order's own trip to the exchange gives ≈0.4–0.8 s for a non-co-located participant.

### 22.2 Results, frozen mm_v1

| | 0 ms | 500 ms | 1000 ms |
|---|---|---|---|
| Develop (Aug 27+28), member net | +127,594 | +13,323 | −33,823 |
| Develop, liquidated | +105,441 | −6,236 | −53,206 |
| Develop, units filled | 241,650 | 139,800 | 121,140 |
| **Holdout (5 days), member net** | +199,537 | **−40,500** | −96,808 |
| Holdout, liquidated | +155,159 | −84,754 | −136,953 |
| Holdout, days positive | 5/5 | 1/5 (Sep 4) | 1/5 (Sep 4) |
| Holdout, R² range | 0.002–0.18 | 0.001–0.16 | 0.001–0.07 |

Effective half-spread per unit falls with latency (develop: ~1.8 → ~1.5 → ~1.0): orders priced on
information that is stale by the time they are live are filled at worse prices.

### 22.3 Book source, and why the longer-dated series were not run

* **Merged book** (`--book merged`, `book/merge.py`): the depth book, overwritten by a quote-feed
  state whenever that state is fresher (state time = receive time for quotes, receive time −
  225 ms for depth; a staler row never overwrites a fresher one). Strictly more information.
  Frozen mm_v1, develop, 0 ms: **+₹69,666** (Aug 27 +73,488; Aug 28 −3,822). Aug 28 shows a
  "max |delta| 26.7 lots" against a 5-lot limit: a noisy quote-feed row moved one leg's implied
  delta at a recomputation, not a real position. Worth hardening if this path is reused.
* **Quote-feed-only books** (`--source quotes`): 5-level books from the Full packets, socket-level
  staleness (union of packet times from the front band ±11 and both futures; gaps > 1 s are
  outages), parity on 23 strikes on a regular 200 ms grid. Validation on the develop days
  failed twice: evaluated per packet, Aug 27 alone was −₹59,172 (a quiet leg's quotes sat on a
  stale fair value while the forward moved); resampled onto a 200 ms decision clock (the current
  code), −₹39,520 over both days against the depth result of +₹127,594.
* **Consequence.** The Oct series (53–61 DTE) and the Sep series on Aug 24/25 (35–36 DTE) exist
  only on the quote feed. Because the adapter cannot reproduce a known result, mm_v1 was **not**
  run on them. Roughly half the zero-latency edge depends on how the depth feed represents the
  book — itself a finding.

### 22.4 The second holdout read

The latency sensitivity was run on the holdout (500 and 1000 ms), logged with the reason
"latency sensitivity of frozen mm_v1 (strategy unchanged; simulator order-latency assumption
varied)". No strategy parameter was chosen from it; it is a disclosure against the 0 ms headline.

---

## 23. The `rv-market-maker` fork — two latency-tolerant strategies, both NO-GO

### 23.1 Why the fork exists

The user's framing (2026-10-02): the classic market maker is exhausted and the problem is latency;
explore "a fresh option market making strategy where edge is not speed", as a fork so the classic
study keeps its own place. Two candidates were proposed: **#1** relative-value quoting off a
fitted volatility smile; **#2** patient liquidity provision far from fair value. Each needed a
pre-registered go/no-go before anything was built.

### 23.2 Protocol and the smile module

`config/frozen/protocol_rv.yaml` (committed and pushed in `656e092` before any RV statistic):
develop Aug 27/28; validation Aug 31 – Sep 4, explicitly described as "unseen by this strategy
family", not "unseen by the researcher"; excluded Aug 24/25/26; strategy #1 criteria.

`src/bnfmm/fairvalue/smile.py` — `fit_smile_loo(x, iv, weight, degree=2)`: per instant, a
weighted polynomial in log-moneyness x = ln(K/F), with **leave-one-out** predictions from the hat
matrix (`y_loo = y − r/(1 − h)`), so a strike is valued by the smile its neighbours imply. Rows
with fewer than degree+3 strikes are NaN. 4 tests, including a brute-force LOO cross-check.

### 23.3 Strategy #1 — smile relative value

`scripts/rv_persistence.py`. Each second: parity F; IV of each strike's OTM option from its mid;
quadratic smile weighted by (vega / half-spread)²; residual r = mid − leave-one-out smile value,
for both calls and puts at all 23 strikes.

| Measure | Value |
|---|---|
| Residual autocorrelation 1 / 5 / 30 / 300 s | 0.718 / **0.587** / 0.467 / 0.297 |
| Median \|residual\| vs median half-spread | 11.6 bp vs 21.6 bp |
| Correction toward smile, \|r\| > half-spread, delay 0: 5 / 30 / 300 s | +1.65 / +2.63 / +5.50 bp |
| Same after a 1 s entry delay | +0.68 / +1.62 / **+4.46 bp** (se 0.29) |
| Bars | autocorrelation ≥ 0.5 → PASS; delayed correction > 11.85 bp → **FAIL** |

**NO-GO.** The signal beats latency (one second costs ~1 bp of it) but not cost. Report:
`reports/rv_strategy1_smile_go_no_go.md`.

### 23.4 Strategy #2 — patient liquidity provision

`config/frozen/protocol_rv_patient.yaml` (pushed in `c062633` before measuring);
`scripts/rv_patient.py`. Resting orders at fair·(1 ∓ δ) with fair the book mid **1 s before**
each print; a print at or through the price fills at the order's price; after a fill that side is
empty for 1 s. Markout vs the 23.71 bp round trip; minute-clustered standard errors; t ≥ 3 and
≥ 50 fills/day required.

| δ | τ | Fills/day | Mean (bp) | se | t vs bar |
|---|---|---|---|---|---|
| 50 | 30 s | 6,508.5 | −3.75 | 4.59 | −5.99 |
| 50 | 300 s | 6,431.5 | +17.55 | 14.25 | −0.43 |
| 100 | 30 s | 687.5 | +16.78 | 10.04 | −0.69 |
| 100 | 300 s | 684.5 | +21.63 | 29.91 | −0.07 |
| 200 | 30 s | 28.5 | −5.75 | 48.36 | −0.61 |
| 200 | 300 s | 28.5 | −4.34 | 49.63 | −0.57 |

**NO-GO.** Close in, the stale order is picked off; far out, reversion is just below cost and
within noise. Caveat: resting prices were not tick-rounded, so the "strict trade-through"
sensitivity coincides with the main rule. Report: `reports/rv_strategy2_patient_go_no_go.md`.

### 23.5 What is still clean, and the combined conclusion

Neither strategy was built, so **no RV analysis has touched the validation days**. The combined
result (`DECISIONS.md` #24): edges large enough to pay ~24 bp of round-trip statutory cost decay
faster than ~0.5 s; edges that survive the latency are smaller than the cost.

---

## 24. Everything tried on the way, with numbers (develop days unless stated)

All member-profile net PnL in rupees, depth book, 0 ms latency, from `reports/config_log.jsonl`
lines 1–27. "Band 3" = ATM ±3 strikes on 2026-08-28; "band 11" = the full depth band.

| Run | Result | What it taught |
|---|---|---|
| Band 3, base (own cost split, buffer 0, improve) | −92,200; capture 409,655 vs inventory −172,800; realised 0.62/unit vs cost 0.84 | Spread capture now positive (Phase 1's was not); loss is post-fill adverse movement |
| Band 3, vega skew 0.5/2.0 × limit 3/10 | −175,732 to −312,346 | Skew makes the reducing side more aggressive → more adverse fills |
| Band 3, buffer {0,3,8} × improve/join | −92,200 / −63,553 / −44,587 / −34,088 / −17,597 / −15,650 | Wider = trades less, approaches zero from below; join is worse per unit |
| Band 11, own split, buffer 3 | −58,991; mean vega +₹226,515/vol pt; β −4.3 lots, R² 0.28 | Long-vega drift masquerades as direction; ITM wide books pay |
| Band 11, merged book (own split) | −99,660 | Fresher book did not reduce adverse selection |
| Band 11, symmetric split, vega limit ∞ / 10 / 3 | −33,052 / −14,974 / −17,071 (R² 0.000 at limit 3) | Symmetric split + vega cap = market-neutral |
| 4 days, symmetric + vega 3, no spread gate | Aug 24 −55,359; **Aug 25 −515,486**; Aug 27 +22,528; Aug 28 −17,071 | Expiry day realised spread ≈ 0 → excluded by the pre-registered DTE rule |
| Imbalance pull 0.4 | −19,373 (vs −17,071) | Failed falsification |
| **Spread gate 37.8 bp** (the mm_v1 config) | Aug 27 +80,643; Aug 28 +46,951 | Derived gate turns it positive |
| Gate 30 / 45 bp × pessimistic / optimistic queue | +119,201 / +123,635; +94,203 / +103,345 | Not a knife edge; queue convention < 10% |

**Diagnostics that motivated the gate** (fills from the band-11 symmetric run, Aug 27+28): net
realised-at-300 s PnL per unit by book spread at fill: < 20 bp −0.870, 20–30 bp −0.122,
30–40 bp −0.101, 40–60 bp **+0.929**, 60–100 bp +1.254. By moneyness: deep ITM (+600–1200 pts)
+1.824, near-ATM OTM (−300 to −100) −0.492. Inputs to the derivation: round-trip statutory cost
23.71 bp (buy 4.50, sell 19.20), adverse selection at 300 s 7.05 bp.

**Other measurements:** fair-value estimators scored against future prints are
indistinguishable (mid, L1 microprice, their average: RMSE within 0.5 tick), and against future
microprice the mid is as good as the microprice — the L1 microprice's lean toward the thin side
reverts within a second. 5-level imbalance does predict the mid with the textbook sign
(≈ −1.2 ticks over 5 s at −0.4; +1.3 at +0.2), about 1/6 of the per-fill adverse selection.

**Bugs found while building Phase 2** (`DECISIONS.md` #25): order ids taken from Python `id()`
were reused after garbage collection, merging sell fills into buy orders and dropping their STT
(member cost understated ~16% on the first runs); fixed before any reported number.

---

## 25. Operational notes for this machine and repo

* **Run everything with `.venv/bin/python`** from the repo root. `pytest` config is in
  `pyproject.toml` (`pythonpath = src`); use `-o addopts=""` to see the summary line.
* **Memory (15 GB) is the binding resource.** Two runs were OOM-killed (exit 137): the per-strike
  screen while 10 QA workers were running, and the quote-feed parity solve on the union of
  unsynchronised packet clocks (millions of grid points × 101 strikes). Run heavy jobs one at a
  time; solve parity on a regular grid when sources are unsynchronised.
* **Long runs:** launch in the background with `python -u` (otherwise stdout is block-buffered and
  a log stays empty until the end) and an explicit `timeout`; write to a log and `grep` it.
* **Do not `pkill -f` / `pgrep -f` with a pattern that also appears in your own shell command** —
  it matches the shell itself (this killed a session twice). Match on something unique, or use PIDs.
* **Git:** HTTPS remote with credential helper `store`; pushes need a GitHub personal access token
  entered by the user at the prompt. Never paste a token into a chat or a file.
* **Gitignored outputs** (`reports/mm/`, `reports/qa/`, `reports/rv/`, `data/`) exist only on the
  original machine. The fill dumps for the mm_v1 holdout are
  `reports/mm/fills-holdout-mm_v1-v0-<date>.npz`.
* **The instrument master** `data/reference/api-scrip-master-detailed.csv` is deliberately stale
  (2026-08-23); `scripts/capture.py` needs `--allow-stale-master` with it. Capture is closed anyway.

---

## 26. Glossary

| Term | Meaning here |
|---|---|
| Member profile | Exchange member: statutory charges only, ₹0 brokerage. The favourable cost bound |
| Dhan profile | Retail: flat ₹20 per executed order (+18% GST) on top of statutory charges |
| Round trip | A buy and a sell of the same option. Statutory cost ≈ 23.71 bp of premium (STT 0.15% on the sell side dominates) |
| DTE | Calendar days to expiry. BANKNIFTY monthly options expire on Tuesdays at 15:30 IST |
| Lot | 30 units. Freeze quantity 601 units = 20 lots |
| Tick | ₹0.05 for options, ₹0.20 for futures |
| Depth feed / quote feed | Dhan's 20-level depth WebSocket (~200 ms snapshots, 48 ids) / general "Full" packet feed (5 levels + volume, event-driven, 326 ids) |
| Tape | Trades inferred by differencing the quote feed's cumulative volume; aggressor by Lee-Ready |
| Parity forward | F implied by C − P = D(F − K) across strikes, precision-weighted (`fairvalue/parity.py`) |
| Spread capture / inventory PnL | Exact split of gross PnL: Σ s(M_fill − P) and Σ s(M_close − M_fill) |
| Liquidated | Gross with open positions closed at the touch and that closing trade's cost paid |
| Effective / realised half-spread | Huang-Stoll: edge vs the prevailing mark at the fill / vs the mark τ later; the difference is adverse selection |
| β, R² | Per-minute PnL increments regressed on parity-forward changes; R² ≈ 0 means market-neutral |
| ATM-lot (vega) | One lot of the most vega-rich (at-the-money) option; the unit of the vega limit |
| Activation latency | Delay before a new order (or a cancel) takes effect in the fill model (§22.1) |
| Develop / holdout / validation | Days used to build a strategy / read once to evaluate it (classic) / unseen by the RV family |
| mm_v1 | The frozen classic strategy, `config/frozen/mm_v1.yaml` |


---

## Appendix A — superseded status snapshots, kept for the record

> These were §0 at earlier points. They are wrong about the current state; read §0.

### A.1 Status as of 2026-09-30 (before the latency result and the fork)

| | |
|---|---|
| **Phase** | Capture **closed** (entitlement lapsed 2026-09-22; user: no more collection). Phase 2 market maker built, frozen, and evaluated out of sample |
| **Headline result** | **`mm_v1` is genuine market making (R² on the market ≤0.18) and makes +₹199,537 on 5/5 unseen sessions at zero latency — but −₹40,500 at 500 ms order latency and −₹96,808 at 1000 ms.** The edge is smaller than the cost of not being fast. §20.6, `DECISIONS.md` #23 |
| **Data** | **10 sessions**, 2026-08-24 → 2026-09-04, 64 GB. Integrity verdict: fit for simulation, one tape bug fixed. `reports/data_integrity.md` |
| **Tests** | **580 collected: 579 passing, 1 failing** — the known stale-master test, §13.9 (now safe to fix, see below) |
| **Configurations tried** | 26 on develop days (`reports/config_log.jsonl`); holdout read once (`reports/holdout_log.md`) |
| **Broker** | Dhan (DhanHQ v2). Entitlement **lapsed**. No order endpoint exists in the repo |
| **Instrument** | BANKNIFTY monthly options, ±11 strikes around ATM, both legs, front series |
| **Open gap** | No index spot was ever captured (§9.4) — the forward is parity-only |
| **Committed to git** | **Nothing.** Deferred by user instruction; the exposure is now larger (§13.1) |

#### What changed on 2026-09-30

1. **Capture is over.** Nothing was recorded after 2026-09-04 (one more session than §9.5 lists).
   The user has decided no more data will be collected.
2. **The 0.15% options STT rate is verified** (Union Budget 2026, effective 2026-04-01) — §13.10
   closed; `config/costs.yaml` records the sources.
3. **§13.11 closed.** `scripts/snapshot_contracts.py` now falls back to raw-log preambles and
   resolves ids within `(exchange, segment)`; all 19 runs across 10 days have correct sidecars.
   The full 2026-08-23 master is archived in `data/reference/archive/`, so the master can now be
   refreshed and §13.9's failing test fixed (a 36 MB public download — ask first).
4. **The holdout split is frozen and enforced in code** (`config/frozen/protocol.yaml`,
   `analysis/holdout.py`) — §18.5's machinery now exists.
5. **Data integrity checked** (`data/qa.py`, `scripts/qa_report.py`, `reports/data_quality.md`,
   `reports/data_integrity.md`): no corrupt books; a tape bug that booked replayed packets as
   trades fixed (DECISIONS #20); the depth feed runs a stable 225 ms behind the quote feed.
6. **The market maker was rebuilt as one portfolio** (`sim/portfolio.py`, `scripts/mm.py`,
   `fairvalue/black76.py`) and is profitable out of sample — §20, DECISIONS #21–22.

#### Next actions, if the project continues

1. **Write-up (Phase 6).** `README.md` still predates both §17 and §20 and is the public artifact.
   The story: Phase 1's negative result, the diagnosis, the four structural changes, the frozen
   holdout. Keep the Phase 1 bugs (§17.8) — they remain the strongest methodological content.
2. **Commit**, once the user sets `user.name`/`user.email`; rename `master` → `main` at the first
   commit (§13.1). Everything is untracked.
3. **Refresh the master and fix §13.9** (download needs the user's go-ahead).
4. **Close the remaining analysis-layer test gaps**: `book/reconstruct.py`, `fairvalue/parity.py`,
   `strategy/quoter.py` (Phase 1 path), `data/channels.py`, `data/chain.py`. The new modules
   (`qa`, `tape` differencing, `merge`, `black76`, `portfolio`, `holdout`) are tested.
5. **Do not re-tune `mm_v1` on the holdout days.** They are spent. Any `mm_v2` has no clean
   out-of-sample data in this corpus; say so if one is built.

#### Superseded status (2026-09-03), kept for the record

#### What changed since the last revision of this document

The previous revision was written on the night of 2026-08-23/24, before any real market data
existed. Four things have happened since, in order:

1. **Capture ran, and keeps running.** Nine sessions are on disk (2026-08-24 through
   2026-09-03, with 2026-09-03 still in progress as this is written). The depth decoder —
   the one risk that could not be retired offline — decoded real bytes correctly. §9.3 is
   now a retired risk rather than an open one.
2. **Phases 2–4 were built out of order and fast**, because the user asked for a working
   market maker on the first two days rather than for the scheduled Sep 1–21 sequence.
   `book/`, `fairvalue/`, `sim/fills/` and `strategy/` are no longer empty. §6.
3. **The market maker was run on 2026-08-24 (1 DTE) and 2026-08-25 (0 DTE, expiry).** It
   loses money in every configuration tested. The one profitable cell was attacked and
   turned out to be a directional bet, not market making. Full account in §17 and
   `reports/phase1_market_maker_results.md`.
4. **Two simulator bugs were found and fixed, each of which manufactured profit.** Both
   were caught by disbelieving a good number rather than by a failing test. `DECISIONS.md`
   #17. This is the most transferable methodological content in the repo.

#### The immediate next actions, in priority order

**§18 is the plan of record for getting the strategy to breakeven; §19 is the plan for the data
gaps. Neither has been implemented.** Both were written 2026-09-04 and supersede the looser
"what would have to change" list in §17.6.

1. **Keep capturing** — and **build the feed watchdog** (§19.6). §15's check is one glance at
   09:20; on Aug 26 the feed died at 10:52 and nobody noticed for four and a half hours. An alarm
   on >30 s of no depth frames turns a lost session into a lost minute. **A session happens once**,
   and entitlement lapses 2026-09-22, so ~13 remain. This outranks every analysis item below.
2. **Verify the 0.15% options STT rate** (§13.10, §18.1). Sell-side STT at that rate is
   **₹0.3054/unit against a captured half-spread of ₹0.3065** — it *is* the result. At 0.10% the
   existing run clears on the member profile with **no strategy change at all.** Twenty minutes,
   highest leverage in the project.
3. **Snapshot the five manifest-less captures** (§13.11). **The only item with a hard external
   deadline: 2026-09-29**, when the vendor prunes the September series and those captures become
   20-level books of unknown strikes. Recoverable from the raw-log preambles today, not afterwards.
4. **Wire the cost floor into the quoter's gate** (§18.4 step 1). `breakeven_ticks()` exists and
   the gate is a hardcoded `1.0` placeholder — **the strategy computes its cost floor for the
   report and ignores it when deciding whether to quote** (§18.2).
5. **Freeze the holdout before touching any knob** (§18.5). Non-optional: §18's levers are five
   knobs on two days, and a number swept into existence is worth less than the negative result it
   replaced.
6. **Close the `PnlGrid` gap** (§5.1). A broken guarantee, not a to-do.
7. **Write tests for the six untested new modules** (§13.2). They carry the headline result.
8. **Analyse the seven unexamined sessions** (§17.7) — the largest available gain, since the data
   is already on disk. Do the §14 Phase 1c quality pass first, with uptime as a *written*
   inclusion criterion (§19.6).

---


