#!/usr/bin/env python3
"""Phase 0: resolve the BANKNIFTY universe and test instrument viability on costs.

This is the Phase 0 decision gate from the plan, run *before* any feed exists.
It answers one question with arithmetic rather than opinion:

    given the Indian F&O statutory cost stack, how much spread must a market
    maker capture per round trip merely to break even -- and is that number
    within reach of the instrument's actual quoted spread?

Everything it needs comes from the public instrument master and config/costs.yaml,
so it runs with no broker account and no network beyond the master download.

    python scripts/phase0_recon.py [--report reports/phase0_instrument_selection.md]

Every figure is reported under **both** brokerage regimes, side by side, per
DECISIONS.md #13. There is deliberately no single headline cost number.

WHAT THIS SCRIPT DOES NOT DO: it does not measure spreads. Comparing breakeven
against a real quoted spread requires the depth feed, which is the other half of
Phase 0. Every spread figure here is labelled as a hypothesis, never a result.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bnfmm.analysis.quote_size import freeze_lot_cap, size_ladder  # noqa: E402
from bnfmm.analysis.report import markdown_table  # noqa: E402
from bnfmm.data import instruments as ins  # noqa: E402
from bnfmm.sim.costs import CostModel, Product  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Premium ladder for the options sweep. Not a claim about where BANKNIFTY ATM
# premium sits -- that is a Phase 0 measurement. A sweep so the shape of the
# relationship is visible regardless.
PREMIUM_LADDER = (100.0, 250.0, 500.0, 1000.0, 1500.0, 2000.0)

# The reference premium the prose quotes. Mid-ladder, and the one a nearest-month
# ATM BANKNIFTY option is most likely to be near -- to be checked, not assumed.
REF_PREMIUM = 1000.0

# The two regimes config/costs.yaml declares as reported. The prose below names
# them explicitly, so a change to the config must fail loudly rather than
# silently invalidate the narrative. See `_load_regimes`.
EXPECTED_PROFILES = ("member", "dhan")

# Short column labels. The full labels come from the config and are printed once
# as a legend; table cells stay narrow.
SHORT = {"member": "member", "dhan": "retail"}


def _load_regimes(costs_path: Path) -> tuple[CostModel, CostModel]:
    """The (member, retail) pair, checked against what the config declares."""
    declared = CostModel.reported_profiles(costs_path)
    if declared != EXPECTED_PROFILES:
        raise SystemExit(
            f"{costs_path} declares reported_profiles={list(declared)}, but this "
            f"report's prose is written for {list(EXPECTED_PROFILES)}. Update the "
            "narrative before changing the reported pair -- a paired report that "
            "silently drops a regime is exactly what DECISIONS.md #13 forbids."
        )
    return tuple(CostModel.for_profile(name, costs_path) for name in declared)  # type: ignore[return-value]


def legend(models: tuple[CostModel, ...]) -> str:
    lines = [
        f"- **{SHORT[m.profile]}** — {m.label}" for m in models  # type: ignore[index]
    ]
    return "\n".join(lines)


def leg_table(models: tuple[CostModel, ...], lot: float, price: float, premium: float) -> str:
    """Itemised cost of one buy leg and one sell leg, per lot, both regimes."""
    cases = [
        ("futures buy", Product.FUTURES, "buy", price),
        ("futures sell", Product.FUTURES, "sell", price),
        ("options buy", Product.OPTIONS, "buy", premium),
        ("options sell", Product.OPTIONS, "sell", premium),
    ]
    components = ["stt", "exchange_txn", "sebi_fee", "stamp_duty", "ipft", "brokerage", "gst"]
    headers = ["leg", "regime", *[c.replace("_", " ") for c in components], "total"]
    rows = []
    for label, product, side, p in cases:
        for model in models:
            breakdown = model.fill_cost(product, side, p, lot).as_dict()
            rows.append(
                [label, SHORT[model.profile]]  # type: ignore[index]
                + [f"{breakdown[c]:,.2f}" for c in components]
                + [f"**{breakdown['total']:,.2f}**"]
            )
    return markdown_table(headers, rows)


def breakeven_table(
    models: tuple[CostModel, ...],
    lot: float,
    price: float,
    fut_tick: float,
    opt_tick: float,
) -> str:
    """Breakeven per round trip, paired across regimes.

    The final two columns are the invariant check: round-trip cost as a fraction
    of the base. It is constant in the member regime and *not* constant at
    retail, because a fixed per-order fee is amortised over whatever base the
    contract happens to carry.
    """
    headers = ["instrument", "base", "tick (Rs)", "tick value/lot"]
    for m in models:
        short = SHORT[m.profile]  # type: ignore[index]
        headers += [f"round trip ({short})", f"breakeven ({short})", f"% of base ({short})"]

    cases: list[tuple[str, Product, float, float]] = [
        ("futures", Product.FUTURES, price, fut_tick),
        *[("options", Product.OPTIONS, p, opt_tick) for p in PREMIUM_LADDER],
    ]

    rows = []
    for name, product, base, tick in cases:
        base_label = f"{base:,.0f} " + ("notional" if product is Product.FUTURES else "premium")
        row = [name, base_label, f"{tick:.2f}", f"Rs {tick * lot:,.2f}"]
        for model in models:
            rt = model.round_trip_cost(product, base, base, lot).total
            be = model.breakeven_ticks(product, base, lot, tick)
            row += [
                f"Rs {rt:,.2f}",
                f"{be:,.1f}t = Rs {be * tick:,.2f}",
                f"{rt / (base * lot) * 100:.4f}%",
            ]
        rows.append(row)
    return markdown_table(headers, rows)


def size_table(
    models: tuple[CostModel, ...], lot: float, premium: float, opt_tick: float, cap: int
) -> str:
    """Per-unit cost against quote size, paired across regimes."""
    ladders = {
        m.profile: size_ladder(m, Product.OPTIONS, premium, lot, opt_tick)
        for m in models
    }
    headers = ["lots", "quantity"]
    for m in models:
        short = SHORT[m.profile]  # type: ignore[index]
        headers += [f"round trip ({short})", f"per unit ({short})", f"brokerage share ({short})"]

    n_rows = len(next(iter(ladders.values())))
    rows = []
    for i in range(n_rows):
        first = ladders[models[0].profile][i]
        flag = " (freeze cap)" if first.n_lots == cap else ""
        row = [f"{first.n_lots}{flag}", f"{first.quantity:,.0f}"]
        for m in models:
            r = ladders[m.profile][i]
            row += [f"Rs {r.round_trip:,.2f}", f"Rs {r.per_unit:.2f}", f"{r.brokerage_share:.0%}"]
        rows.append(row)
    return markdown_table(headers, rows)


def build_report(
    universe: ins.BankniftyUniverse,
    models: tuple[CostModel, CostModel],
    asof: date,
) -> str:
    member, retail = models
    fut = universe.front_future
    lot = fut.lot_size
    price = universe.spot_estimate or 0.0
    opt_tick = universe.options[0].tick_size if universe.options else 0.05
    fut_tick = fut.tick_size

    def fut_stats(m: CostModel) -> tuple[float, float, float]:
        rt = m.round_trip_cost(Product.FUTURES, price, price, lot).total
        be = m.breakeven_ticks(Product.FUTURES, price, lot, fut_tick)
        return rt, be, be * fut_tick

    def opt_stats(m: CostModel) -> tuple[float, float, float]:
        rt = m.round_trip_cost(Product.OPTIONS, REF_PREMIUM, REF_PREMIUM, lot).total
        be = m.breakeven_ticks(Product.OPTIONS, REF_PREMIUM, lot, opt_tick)
        return rt, be, be * opt_tick

    m_fut_rt, m_fut_ticks, m_fut_rs = fut_stats(member)
    r_fut_rt, r_fut_ticks, r_fut_rs = fut_stats(retail)
    m_opt_rt, m_opt_ticks, m_opt_rs = opt_stats(member)
    r_opt_rt, r_opt_ticks, r_opt_rs = opt_stats(retail)

    m_fut_pct = m_fut_rt / (price * lot) * 100
    m_opt_pct = m_opt_rt / (REF_PREMIUM * lot) * 100

    stt_sell = member.fill_cost(Product.FUTURES, "sell", price, lot).stt
    brokerage_rt = r_opt_rt - m_opt_rt

    cap = freeze_lot_cap(fut.freeze_qty, lot)
    ladder = size_ladder(retail, Product.OPTIONS, REF_PREMIUM, lot, opt_tick)
    one_lot, capped = ladder[0], ladder[-1]

    # Cost of neutralising the delta of one option lot with the front future.
    ref_delta = 0.5  # ATM
    m_hedge = ref_delta * m_fut_rt
    r_hedge = ref_delta * r_fut_rt
    hedge_in_option_ticks = m_hedge / (opt_tick * lot)

    return f"""# Phase 0 — Instrument selection on cost grounds

*Generated by `scripts/phase0_recon.py` on {asof}. Reproduce with
`python scripts/phase0_recon.py`. Every number below is computed from
`config/costs.yaml` and the exchange instrument master — none are quoted from
memory or from secondary sources.*

**Data tier: none.** This report contains no market data. It is arithmetic on a
published fee schedule plus contract specs read from the exchange instrument
master. The spread figures it compares against are explicitly labelled
hypotheses and are the subject of the *next* Phase 0 step.

**Every cost figure is reported under both brokerage regimes:**

{legend(models)}

Neither is the headline. The member case is the most favourable regime that could
plausibly be modelled and is included as a *bound*, not as a scenario this
project could actually trade. Where the two regimes lead to different
conclusions, that difference is the result. See `DECISIONS.md` #13.

---

## Resolved universe

```
{universe.summary()}
```

Contract facts read from the master, not assumed — see `tests/test_master_facts.py`,
which fails if NSE changes any of them:

- **No weekly BANKNIFTY options.** Withdrawn by NSE; only monthly expiries list.
- **Expiry day is Tuesday**, not the Thursday most references still state.
- Lot size **{lot}**, futures tick **Rs {fut_tick:.2f}**, options tick **Rs {opt_tick:.2f}**.
- Freeze quantity **{fut.freeze_qty}** units — a hard ceiling on quote size,
  which is **{cap} lots**.
- `TICK_SIZE` in the master is denominated in **paise**.

---

## The cost of one round trip

Per lot of {lot}. Brokerage is charged per *executed order*, so a quote that
fills in several partials pays it once — the simulator routes fills through
`OrderCost` for exactly this reason, and the per-leg figures below assume one
fill per order.

{leg_table(models, lot, price, REF_PREMIUM)}

---

## Breakeven: spread required to cover costs

{breakeven_table(models, lot, price, fut_tick, opt_tick)}

The `% of base` columns carry a result that is easy to miss. In the **member**
regime, round-trip cost is a fixed **{m_fut_pct:.4f}% of futures notional** and a
fixed **{m_opt_pct:.4f}% of options premium** — breakeven in ticks scales with the
price level, but that percentage does not.

At **retail it is not a fixed percentage at all**, because a flat per-order fee is
amortised over whatever base the contract carries. On the ladder above, retail
cost falls from **{retail.round_trip_cost(Product.OPTIONS, PREMIUM_LADDER[0], PREMIUM_LADDER[0], lot).total / (PREMIUM_LADDER[0] * lot) * 100:.2f}%** of premium
at Rs {PREMIUM_LADDER[0]:,.0f} to **{retail.round_trip_cost(Product.OPTIONS, PREMIUM_LADDER[-1], PREMIUM_LADDER[-1], lot).total / (PREMIUM_LADDER[-1] * lot) * 100:.2f}%** at
Rs {PREMIUM_LADDER[-1]:,.0f}. Cheap options are structurally worse for a
fixed-fee market maker, and no amount of quoting skill changes that.

---

## Finding 1 — BANKNIFTY futures market making is not viable at these rates

One futures tick is Rs {fut_tick:.2f}, i.e. **Rs {fut_tick * lot:,.2f} per lot**.
One round trip costs **Rs {m_fut_rt:,.2f} per lot at member rates and
Rs {r_fut_rt:,.2f} at retail**, of which **Rs {stt_sell:,.2f} is STT alone** — because
futures STT is levied on contract *notional* (Rs {price * lot:,.0f} per lot), not on
premium or on spread.

Breakeven is therefore **{m_fut_ticks:.0f} ticks (Rs {m_fut_rs:.2f}) at member rates
and {r_fut_ticks:.0f} ticks (Rs {r_fut_rs:.2f}) at retail.** BANKNIFTY futures do not
quote a Rs {m_fut_rs:.0f} spread in any normal market condition.

**This finding does not depend on the brokerage regime**, which is what makes it
solid. Brokerage moves breakeven by {(r_fut_rs - m_fut_rs) / m_fut_rs * 100:.0f}%
on a figure that is already an order of magnitude out of reach. The gap is not
marginal and it is not closable by better quoting: it is a property of the fee
schedule, and it survives the most favourable cost assumption available.

This is a genuine finding and it belongs in the writeup rather than being
quietly designed around. It is also the kind of thing an interviewer can check
in thirty seconds, which is precisely why it is worth being the one to state it.

## Finding 2 — Options are the defensible instrument, and are still marginal

Options STT is charged on **premium**, not notional. At a Rs {REF_PREMIUM:,.0f}
premium that is a Rs {REF_PREMIUM * lot:,.0f} base instead of Rs {price * lot:,.0f} —
a {price / REF_PREMIUM:.0f}x smaller base. Round trip and breakeven, paired:

| regime | round trip/lot | breakeven | vs futures |
|---|---|---|---|
| member | Rs {m_opt_rt:,.2f} | {m_opt_ticks:.0f} ticks = **Rs {m_opt_rs:.2f}** | {m_fut_rs / m_opt_rs:.0f}x more favourable |
| retail | Rs {r_opt_rt:,.2f} | {r_opt_ticks:.0f} ticks = **Rs {r_opt_rs:.2f}** | {r_fut_rs / r_opt_rs:.0f}x more favourable |

Both land in the same order of magnitude as a plausible ATM spread — but *only*
that. Neither is comfortably profitable; both sit close to the cost boundary.

**Unlike Finding 1, this finding does depend on the brokerage regime.** Two
Rs 20 orders plus GST add **Rs {brokerage_rt:.2f} to a Rs {m_opt_rt:,.2f} statutory
bill — {brokerage_rt / m_opt_rt * 100:.0f}%**, moving breakeven from
Rs {m_opt_rs:.2f} to Rs {r_opt_rs:.2f}. At one lot, brokerage is the single largest
component of the round trip. Any result stated without naming its brokerage
regime is therefore uninterpretable, which is why every figure in this project
is paired.

**This is the more interesting result, and it sharpens the project's thesis.**
The study was already framed as an execution-realism measurement rather than a
profit claim. The cost stack shows the margin is thin enough that the *fill
assumption alone may determine the sign of the PnL*: a naive instant-fill
backtest that assumes full spread capture on every quote will report a profit,
and a queue-aware simulator on the same data and the same strategy may not. That
is a much stronger claim than "queue-aware fills reduce PnL by some percentage,"
because it makes the methodology choice decision-relevant rather than merely
quantitative.

---

## Finding 3 — and delta hedging with futures is not affordable either

This one cuts scope rather than adding it. Neutralising the delta of one ATM
option lot (delta ~{ref_delta}) needs {ref_delta} futures lots, and a futures round
trip costs Rs {m_fut_rt:,.2f} per lot. So one hedge round trip costs
**Rs {m_hedge:,.2f} at member rates (Rs {r_hedge:,.2f} at retail) —
{m_hedge / m_opt_rt:.1f}x the entire round-trip cost of the option position it is
hedging**, or **{hedge_in_option_ticks:.0f} option ticks** of edge.

Finding 1 is why: the same notional-based STT that kills futures market making
also taxes every futures hedge. A strategy that re-hedges on any meaningful
frequency pays its whole edge to the exchange and the exchequer.

The consequence is a design constraint, not a research obstacle:

- **No futures hedging leg.** Quote **both the CE and the PE at the same
  strike**, so inventory arriving on the two sides partially offsets in delta by
  construction, and manage the residual by **skewing quotes** — which is exactly
  what the Avellaneda–Stoikov reservation-price shift already does, generalised
  from a directional inventory to a delta inventory.
- **Delta is needed as a number, not as an execution path.** One Black–Scholes
  delta function, testable against published values, plus the put–call parity
  work already in scope. No vol surface, no gamma or vega hedging.

Net effect on the plan: one small module added (`fairvalue/greeks.py`), and the
hedging execution layer that switching instruments seemed to imply is *not*
needed. Scope is roughly unchanged.

---

## Finding 4 — a fixed per-order fee makes quote size a decision variable

Statutory charges scale with quantity; brokerage does not. So at retail, per-unit
cost falls with order size, while in the member regime it is flat — which
isolates the fixed fee as the cause rather than anything about the contract.

{size_table(models, lot, REF_PREMIUM, opt_tick, cap)}

At one lot, **{one_lot.brokerage_share:.0%}** of the retail round trip is
brokerage and its GST; at the {capped.n_lots}-lot freeze cap it is
**{capped.brokerage_share:.0%}**. Per-unit breakeven falls from
**Rs {one_lot.per_unit:.2f}** to **Rs {capped.per_unit:.2f}** — a
**{(1 - capped.per_unit / one_lot.per_unit) * 100:.0f}% reduction in the hurdle rate
from size alone.**

**This is a gap in the standard model, not just a cost note.**
Avellaneda–Stoikov (2008) derives an optimal *spread* from inventory risk and
order-arrival intensity, and assumes no fixed per-trade cost — in that model,
quoting one lot and quoting twenty are the same decision scaled. With a flat
Rs 20 per order they are not. Cost pushes toward large quotes; inventory risk and
adverse selection push the other way; the exchange freeze quantity of
{fut.freeze_qty} units caps the top at {cap} lots. Quantifying that trade-off is a
small original extension and is scheduled as one — the cost half is
`src/bnfmm/analysis/quote_size.py`, the risk half needs the fill simulator.

---

## What this does not settle

1. **The actual quoted spread.** Every spread figure above is a hypothesis. The
   comparison that decides viability needs measured top-of-book spread, in
   ticks, over a real session. That is the depth-feed half of Phase 0, and the
   cheapest way to settle it is a `/optionchain` poll, which returns best
   bid/ask across every strike without touching the depth WebSocket.
2. **Depth at the touch.** Breakeven assumes the full quoted size fills. Queue
   position determines what fraction actually does, which is the whole point of
   the fill simulator.
3. **Adverse selection.** Not a fee, and not in this report. It is the other
   cost of quoting and is measured in Phase 5 attribution.
4. **The risk half of the size trade-off.** Finding 4 gives the cost curve only.
   Whether quoting {cap} lots is *wise* depends on fill probability and inventory
   risk at that size, which the simulator has to answer.
5. **Whether the rate table is current.** Statutory rates change by budget and
   circular; futures STT has been revised repeatedly. `config/costs.yaml`
   carries a `retrieved` date and must be re-verified against the live
   NSE/SEBI circular before any number here is published.

## Consequence for the plan

The approved plan named BANKNIFTY futures as the primary quoted instrument and
placed Greeks hedging out of scope. Finding 1 makes futures indefensible as the
quoted instrument, so the Phase 0 decision gate fires in favour of options;
Finding 3 keeps that switch cheap by ruling out a hedging leg; Finding 4 adds one
small module and one paragraph of original analysis. The user's kickoff decision
was "flexible on Phase 0 evidence" — this is that evidence. Logged in
`DECISIONS.md` #9, #12 and #13.

Futures do not leave the project. They remain the reference instrument for the
put–call parity synthetic-future comparison, and Finding 1 is itself a result
worth reporting.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--costs", default=None)
    parser.add_argument(
        "--report",
        nargs="?",
        const="reports/phase0_instrument_selection.md",
        default=None,
        help="also write the markdown report to this path",
    )
    parser.add_argument("--no-refresh", action="store_true", help="use the local master as-is")
    args = parser.parse_args()

    config_path = args.config or (REPO_ROOT / "config" / "instruments.yaml")
    costs_path = Path(args.costs or (REPO_ROOT / "config" / "costs.yaml"))

    universe = ins.resolve_from_disk(config_path, refresh=not args.no_refresh)
    models = _load_regimes(costs_path)

    report = build_report(universe, models, date.today())
    print(report)

    if args.report:
        out = REPO_ROOT / args.report
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\n[written] {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
