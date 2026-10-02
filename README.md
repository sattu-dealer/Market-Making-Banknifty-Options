# Market making BANKNIFTY options — a measured, honest negative result

A research and simulation study of passive market making in NSE BANKNIFTY monthly options,
built on **10 sessions of self-captured 20-level order-book depth** (2026-08-24 → 2026-09-04,
~64 GB, Dhan's DhanHQ v2 feeds). No order was ever placed: there is no trading endpoint in the
repository, and a test suite enforces that.

**The answer, in one sentence:** a properly built options market maker on this data captures
spread and stays market-neutral, but its edge is shorter-lived than the latency of anyone
receiving these feeds — profitable at zero latency, loss-making at a realistic 500 ms.
**Profitable market making here is a co-location question, not a strategy question.**

| Frozen strategy `mm_v1`, 5 unseen sessions | Net, exchange-member cost | Liquidated at close | Days positive | R² of PnL on the market |
|---|---|---|---|---|
| 0 ms order latency | +₹199,537 | +₹155,159 | 5/5 | 0.002–0.18 |
| **500 ms** (realistic, non-co-located) | **−₹40,500** | **−₹84,754** | 1/5 | ≤0.16 |
| 1000 ms | −₹96,808 | −₹136,953 | 1/5 | ≤0.07 |

One lot per quote. At Dhan's retail ₹20/order it loses even at zero latency.

> This `main` branch (tag `classic-mm-v1`) is the complete classic market-making study. A
> separate branch, `rv-market-maker`, explores strategies whose edge is not speed.

---

## The story

**1. A first market maker that lost money — and a profit that turned out to be a bet.**
A single-strike quoter lost in every configuration on the 1-DTE and expiry-day sessions. Its one
profitable run (+₹92k) was decomposed exactly into spread capture and inventory PnL:
**58% of gross came from inventory** on a day the market fell. It was a directional bet, and was
reported as a failure, not a result. → [`reports/phase1_market_maker_results.md`](reports/phase1_market_maker_results.md)

**2. Data integrity before more strategy.** A QA pass over every session found no corrupt books
(across 34.3M option book snapshots: zero off-tick or non-positive prices, 0.004% crossed or
locked), one real bug — the trade
tape counted the feed's stale-packet replays as new volume (5.8% phantom volume on one day) —
and a stable 225 ms lag of the depth feed behind the quote feed.
→ [`reports/data_integrity.md`](reports/data_integrity.md)

**3. A portfolio market maker that works — at zero latency.** Four structural changes, each
driven by a measurement rather than by the PnL it produced:

* **One portfolio**: all strikes share one inventory; Black-76 delta and vega (implied from the
  market, used for risk only, never for price) are skewed against and hard-limited.
* **Queue priority**: unchanged quotes rest instead of being re-placed every 200 ms, which had
  left the old quoter permanently at the back of the queue, fillable only by informed sweeps.
* **Symmetric cost split**: sell-side STT is 4× the buy side; charging each side its own cost
  made the book drift structurally long options (150 ATM-lots of vega).
* **A cost gate in basis points of premium**: quote only books whose spread covers round-trip
  cost (23.7 bp) plus twice measured adverse selection (7.05 bp) = 37.8 bp. Derived, not swept.

Frozen after 26 logged configurations and run once on five held-out sessions: profitable on all
five, spread capture 3.3× the inventory term, statistically distinguishable from zero.
→ [`reports/phase2_portfolio_market_maker.md`](reports/phase2_portfolio_market_maker.md)

**4. The robustness check that changed the conclusion.** Trades reach us ~0.3–0.5 s after they
execute. With a symmetric order-latency model (new orders *and* cancels take effect only after
the latency), the same frozen strategy loses out of sample at 500 ms. Its edge is real but
decays faster than a non-co-located participant can act on it.

## Methodology worth looking at

* **Every PnL is decomposed** — spread capture vs inventory, exactly, plus Huang-Stoll
  effective/realised spread at 1 s / 5 s / 30 s / 300 s and β/R² of PnL on the parity forward.
  The decomposition is what caught the first false positive.
* **Three bugs that manufactured profit, each caught by disbelieving a good number**: a sweep
  rule that filled inside-spread quotes for free (effective spread came out *negative* —
  structurally impossible), marking fills after the move they caused, and a NaN closing mark that
  booked open positions at zero. → [`DECISIONS.md`](DECISIONS.md) #17, #19
* **Holdout enforced in code, not by discipline**: a frozen split refuses to load holdout days
  without a logged, reasoned unlock; every configuration ever run is logged; the strategy was
  hash-frozen before evaluation. → [`config/frozen/`](config/frozen/),
  [`reports/holdout_log.md`](reports/holdout_log.md), `reports/config_log.jsonl`
* **Real vs simulated is labelled throughout**: books and costs are real; the tape is derived
  from cumulative volume; aggressor side is inferred (Lee-Ready); our fills are simulated.
* **Instrument choice from cost evidence**: options, not futures — options STT is on premium,
  a 58× smaller base. → [`reports/phase0_instrument_selection.md`](reports/phase0_instrument_selection.md)

## Limitations

* **Fills are simulated** on a ~200 ms snapshot feed with no event stream: queue position is
  approximated by depletion against traded volume; hidden orders are invisible.
* **One monthly expiry cycle**, 25–33 days to expiry for the quoted sessions; near-expiry sessions
  are excluded by a rule written before any data existed. Ten sessions in total.
* **No index spot** was captured (a silent subscription failure); the forward is parity-only.
* **The holdout is spent.** Any further variant of this strategy has no untouched data.

## Layout

```
src/bnfmm/
  data/       capture: wire decoders, raw byte log, Parquet store, reconnect, QA
  book/       book reconstruction, trade tape, causal depth+quote merge
  fairvalue/  microprice, put-call parity forward, Black-76 (risk only)
  sim/        fill model (depletion), Phase 1 backtest, portfolio market maker, costs
  analysis/   result containers, holdout enforcement
scripts/      capture, qa_report, backtest (Phase 1), mm (Phase 2), strike_economics
config/       capture universe, costs, calendar, frozen protocol and strategy
reports/      phase reports, data integrity, holdout and configuration logs
BRIEFING.md   full handover document;  DECISIONS.md  every non-obvious decision, with reasons
```

## Running it

Python 3.11+. The captured data is not in the repository (it is tens of GB and licensed vendor
data), so the scripts below need a capture of your own under `data/`.

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

(One test, `test_spot_bootstrap_agrees_across_calls_and_puts`, fails against the stale
2026-08-23 instrument master kept deliberately to label the August captures; BRIEFING §13.9.)

```bash
python scripts/qa_report.py
```

```bash
python scripts/mm.py --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8 --latency-ms 500
```
