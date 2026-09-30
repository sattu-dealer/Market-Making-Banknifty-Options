"""A multi-strike options market maker with one inventory and one delta.

WHAT CHANGED FROM ``sim.backtest.run_day``, AND WHY
    Phase 1 (BRIEFING §17) ran one contract at a time. Three consequences of that
    design, each of which this module removes:

    1.  **No netting.** Each leg managed its own inventory, so a book short calls
        and long puts -- one directional bet taken twice -- looked "hedged" leg by
        leg. Here every leg shares one portfolio, whose exposure is measured in
        *delta* (``fairvalue.black76``), skewed against, and hard-limited.

    2.  **No queue priority.** ``run_day`` cancelled and re-placed every quote on
        every 200 ms snapshot, sending it to the back of its level each time. A
        quote that never ages can only fill when one interval clears the entire
        displayed level: the sweeps that informed traders send. Here an order whose
        price is unchanged is *left resting*, and its queue ahead depletes against
        traded volume exactly as ``DepletionSimulator`` already models.

    3.  **A placeholder cost gate.** The quoter's gate was a hardcoded 1 tick
        (§18.2). Here each side must clear *its own* statutory cost at the fair
        value -- sell-side STT makes a sell roughly 4x a buy -- plus an explicit
        adverse-selection buffer. "Do not trade below cost" is derived from the
        published rate table, not fitted.

WHERE QUOTES GO
    For each leg at each of its snapshots, with fair value ``V``:

        bid_target = V - c_buy(V)  - buffer - skew
        ask_target = V + c_sell(V) + buffer - skew

    rounded away from the market. The quote is then placed *no more aggressively
    than one tick inside the touch* (``improve``) -- a market maker wants to be
    first in the queue, not to give away the spread -- and is withdrawn if it would
    sit more than ``max_behind_ticks`` behind the touch, because an order resting
    behind the touch fills only when the market sweeps through it, which is the
    definition of adverse selection (§18.3). It never crosses.

    ``skew`` is the Avellaneda-Stoikov reservation-price shift generalised to a
    portfolio: ``delta_skew * (portfolio delta in lots) * leg delta``, plus a small
    per-leg inventory term that stops one strike absorbing the whole book.

WHAT IS HELD FIXED FROM PHASE 1
    The fill model (``DepletionSimulator``: pessimistic queue by default, sweeps
    only under the three conditions of ``_swept``, no fills across a gap), fills
    settled *before* re-quoting, fill marks at the *prevailing* microprice, and the
    exact spread-capture / inventory decomposition. The two bugs of DECISIONS #17
    stay fixed because none of that code is touched.

LOOK-AHEAD
    Every array indexed at snapshot ``k`` of a leg (book, fair value, delta) was
    computed from data received at or before that snapshot's receive time; the
    caller is responsible for that and ``scripts/mm.py`` builds them with
    last-observation-carried-forward joins only. Within the loop, legs are
    processed in global receive-time order, so the portfolio delta a leg is skewed
    against contains only fills that had already happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..book.reconstruct import BookSeries
from ..book.tape import Tape
from ..fairvalue.microprice import imbalance, microprice
from .backtest import Position
from .costs import CostModel, Product
from .fills.depletion import DepletionSimulator

NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class PortfolioParams:
    """Every knob. Each one is logged with every run (``reports/config_log.jsonl``)."""

    size_lots: int = 1
    #: Adverse-selection buffer per side, in ticks, on top of that side's cost.
    buffer_ticks: float = 0.0
    #: How the round-trip cost is charged to the two sides. "own": each side
    #: clears its own cost -- but sell-side STT is ~4x the buy side, so asks sit
    #: further from fair value than bids, bids fill more often, and the book drifts
    #: structurally long options (long vega). "symmetric": each side clears half of
    #: the round trip. The edge demanded per round trip is identical; only its
    #: split changes, so the two sides fill at balanced rates.
    cost_split: str = "own"
    #: Place at most one tick inside the touch (True) or only join it (False).
    improve: bool = True
    #: How far behind the touch a quote may rest before it is withdrawn.
    max_behind_ticks: float = 0.0
    #: Hard cap on one leg's position, in lots.
    leg_cap_lots: int = 5
    #: Hard cap on |portfolio delta|, in lots of the underlying.
    delta_limit_lots: float = 5.0
    #: Reservation shift, rupees, per lot of portfolio delta, on a delta-1 option.
    delta_skew: float = 0.5
    #: Reservation shift, rupees, on the reference (most vega-rich) option, per
    #: reference-lot of portfolio vega. Controls net long/short *option* inventory,
    #: which delta does not see: long calls plus long puts is delta-flat and
    #: still bleeds theta and loses when implied vol falls.
    vega_skew: float = 0.0
    #: Hard cap on |portfolio vega|, in reference-option lots.
    vega_limit_lots: float = 1e9
    #: Per-leg inventory skew, ticks per lot held in that leg.
    leg_skew_ticks: float = 0.5
    #: Stand a leg down while its book spread is below this, in basis points of
    #: fair value. Both the statutory cost and adverse selection scale with
    #: premium while the spread in ticks does not, so the viable books are the
    #: ones whose spread *as a fraction of premium* covers the round trip. The
    #: value used is derived, not swept: round-trip cost (23.7 bp) + 2 x measured
    #: 300 s adverse selection (7.05 bp, develop days) = 37.8 bp. 0 disables it.
    min_spread_bp: float = 0.0
    #: Do not quote options priced below this.
    min_premium: float = 5.0
    #: A snapshot gap longer than this stands the leg down and refuses fills.
    max_stale_ns: int = NS
    #: Pessimistic queue: join the back of our price level (True), or the front.
    queue_at_price_ahead: bool = True
    #: Pull the side about to be run over when |5-level imbalance| exceeds this:
    #: imbalance < -x pulls the bid (sell-side depth dominates, price leans down),
    #: > +x pulls the ask. 1.0 or more disables it. BRIEFING §18.4 step 2.
    imbalance_pull: float = 1.0
    imbalance_levels: int = 5
    #: Stop opening new risk this many seconds before the close; only reduce.
    close_only_last_s: float = 0.0


@dataclass(slots=True)
class Leg:
    """One quoted option, with every array on its own book clock."""

    security_id: int
    name: str
    strike: float
    is_call: bool
    tick: float
    lot_size: int
    freeze_qty: int
    book: BookSeries
    tape: Tape
    fair_value: np.ndarray  # quote anchor
    delta: np.ndarray  # dV/dF per unit, for risk only
    vega: np.ndarray | None = None  # dV per vol point per unit, for risk only


@dataclass(slots=True)
class FillRecord:
    t: int
    leg: int
    side: str  # "bid" we bought, "ask" we sold
    price: float
    qty: int
    order_id: int
    mark_micro: float  # prevailing own microprice
    mark_fv: float  # prevailing fair value
    delta: float
    vega: float
    swept: bool
    queue_ahead: int


@dataclass(slots=True)
class PortfolioResult:
    legs: list[Leg]
    params: PortfolioParams
    fills: list[FillRecord] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    stand_down: dict[str, int] = field(default_factory=dict)
    quoted_events: int = 0
    events: int = 0
    #: (t, realised + mark-to-micro, cumulative member cost, portfolio delta)
    equity: list[tuple[int, float, float, float]] = field(default_factory=list)
    close_marks: dict[str, np.ndarray] = field(default_factory=dict)
    max_abs_delta_lots: float = 0.0
    max_abs_vega_lots: float = 0.0
    delta_pinned_events: int = 0
    vega_pinned_events: int = 0

    # -- accounting ----------------------------------------------------------

    def final_units(self) -> np.ndarray:
        return np.array([p.units for p in self.positions], dtype=float)

    def gross(self, mark: str = "micro") -> float:
        m = self.close_marks[mark]
        return float(sum(p.realised + p.unrealised(float(x)) for p, x in zip(self.positions, m)))


def _side_cost_rates(model: CostModel) -> tuple[float, float]:
    """Statutory cost per rupee of premium, buy side and sell side (no brokerage).

    Every statutory component is proportional to premium, so one unit priced at
    Rs 1 gives the rate. Brokerage is excluded on purpose: it is per order and
    profile-specific, and the gate prices the member case -- the Dhan column is
    reported, not optimised for (BRIEFING §18.4 step 4).
    """
    b = model.fill_cost(Product.OPTIONS, "buy", 1.0, 1.0)
    s = model.fill_cost(Product.OPTIONS, "sell", 1.0, 1.0)
    return b.total - b.brokerage * (1 + 0.18), s.total - s.brokerage * (1 + 0.18)


def run_portfolio(
    legs: list[Leg],
    params: PortfolioParams,
    *,
    cost_model: CostModel | None = None,
    session_end_ns: int | None = None,
    equity_every_ns: int = 60 * NS,
) -> PortfolioResult:
    """Quote every leg over one day, sharing one inventory. See the module docstring."""
    model = cost_model or CostModel.for_profile("member")
    rate_buy, rate_sell = _side_cost_rates(model)
    p = params
    L = len(legs)
    res = PortfolioResult(legs=legs, params=p, positions=[Position() for _ in legs])

    sims = [DepletionSimulator(max_stale_ns=p.max_stale_ns,
                               queue_at_price_ahead=p.queue_at_price_ahead) for _ in legs]
    micro = [microprice(g.book.bid_px, g.book.bid_qty, g.book.ask_px, g.book.ask_qty)
             for g in legs]
    imb = [imbalance(g.book.bid_qty, g.book.ask_qty, levels=p.imbalance_levels) for g in legs]
    # Raw targets without skew, vectorised once per leg.
    raw_bid, raw_ask = [], []
    if p.cost_split == "symmetric":
        rb = rs = 0.5 * (rate_buy + rate_sell)
    elif p.cost_split == "own":
        rb, rs = rate_buy, rate_sell
    else:
        raise ValueError(f"cost_split must be 'own' or 'symmetric', got {p.cost_split!r}")
    for g in legs:
        v = g.fair_value
        buf = p.buffer_ticks * g.tick
        raw_bid.append(v - rb * v - buf)
        raw_ask.append(v + rs * v + buf)
    # Tape prints per snapshot interval (t[k-1], t[k]].
    lo_hi = []
    for g in legs:
        bucket = np.searchsorted(g.book.recv_wall_ns, g.tape.recv_wall_ns, side="left")
        ks = np.arange(len(g.book))
        lo_hi.append((np.searchsorted(bucket, ks, "left"), np.searchsorted(bucket, ks, "right")))

    # Global event order: every (leg, snapshot k>=1), by receive time.
    ev_t = np.concatenate([g.book.recv_wall_ns[1:] for g in legs]) if L else np.zeros(0, np.int64)
    ev_leg = np.concatenate([np.full(len(g.book) - 1, i) for i, g in enumerate(legs)]) if L else ev_t
    ev_k = np.concatenate([np.arange(1, len(g.book)) for g in legs]) if L else ev_t
    order = np.argsort(ev_t, kind="stable")
    ev_t, ev_leg, ev_k = ev_t[order], ev_leg[order], ev_k[order]

    resting: list[dict[str, object]] = [{"bid": None, "ask": None} for _ in legs]
    cur_delta = np.zeros(L)
    cur_vega = np.zeros(L)
    port_vega = 0.0  # rupees per vol point
    # The reference option: the most vega-rich leg (the ATM), one lot of it.
    vref = max((float(np.nanmedian(g.vega)) for g in legs
                if g.vega is not None and np.isfinite(g.vega).any()), default=0.0)
    vref_lot = vref * (legs[0].lot_size if L else 1)
    order_seq: dict[int, int] = {}  # id(live RestingOrder) -> unique order number
    next_order = [0]
    last_micro = np.full(L, np.nan)
    port_delta = 0.0  # units of the underlying
    fill_counter = 0
    seen_fills = np.full(L, -1)
    live_last = np.zeros(L, dtype=bool)  # was the leg quoting-eligible last event
    cum_cost = 0.0
    next_eq = int(ev_t[0]) + equity_every_ns if len(ev_t) else 0
    lot = legs[0].lot_size if L else 1
    down = res.stand_down

    def stand(reason: str) -> None:
        down[reason] = down.get(reason, 0) + 1

    def pull(i: int, side: str) -> None:
        o = resting[i][side]
        if o is not None:
            sims[i].cancel(o)
            resting[i][side] = None

    for e in range(len(ev_t)):
        t = int(ev_t[e])
        i = int(ev_leg[e])
        k = int(ev_k[e])
        g = legs[i]
        bk = g.book
        res.events += 1

        # -- equity sample (before this event changes anything) ----------------
        if t >= next_eq:
            mtm = sum(pos.realised + pos.unrealised(float(m))
                      for pos, m in zip(res.positions, last_micro))
            res.equity.append((t, mtm, cum_cost, port_delta, port_vega))
            next_eq = t + equity_every_ns

        prev_t = int(bk.recv_wall_ns[k - 1])
        gap = t - prev_t

        # -- 1. settle the interval (prev_t, t] against orders resting in it ------
        lo, hi = lo_hi[i][0][k], lo_hi[i][1][k]
        if hi > lo and (resting[i]["bid"] is not None or resting[i]["ask"] is not None):
            fills = sims[i].step(
                now_ns=t, prev_ns=prev_t,
                tape_price=g.tape.price[lo:hi], tape_qty=g.tape.quantity[lo:hi],
                tape_aggressor=g.tape.aggressor[lo:hi],
                best_bid=float(bk.best_bid[k]), best_ask=float(bk.best_ask[k]),
            )
            for f in fills:
                pos = res.positions[i]
                pos.apply(f.side, f.price, f.quantity)
                signed = f.quantity if f.side == "bid" else -f.quantity
                port_delta += signed * cur_delta[i]
                port_vega += signed * cur_vega[i]
                o = resting[i][f.side]
                cost = model.fill_cost(Product.OPTIONS, "buy" if f.side == "bid" else "sell",
                                       f.price, f.quantity).total
                cum_cost += cost
                res.fills.append(FillRecord(
                    t=t, leg=i, side=f.side, price=f.price, qty=f.quantity,
                    order_id=order_seq.get(id(o), -1), mark_micro=float(micro[i][k - 1]),
                    mark_fv=float(g.fair_value[k - 1]), delta=float(cur_delta[i]),
                    vega=float(cur_vega[i]),
                    swept=f.swept, queue_ahead=f.queue_ahead,
                ))
                fill_counter += 1
                if o is not None and not o.live:
                    pull(i, f.side)

        # -- refresh this leg's state ------------------------------------------
        d_new = float(g.delta[k])
        if np.isfinite(d_new):
            port_delta += res.positions[i].units * (d_new - cur_delta[i])
            cur_delta[i] = d_new
        if g.vega is not None:
            v_new = float(g.vega[k])
            if np.isfinite(v_new):
                port_vega += res.positions[i].units * (v_new - cur_vega[i])
                cur_vega[i] = v_new
        if np.isfinite(micro[i][k]):
            last_micro[i] = micro[i][k]
        res.max_abs_delta_lots = max(res.max_abs_delta_lots, abs(port_delta) / lot)
        if vref_lot > 0:
            res.max_abs_vega_lots = max(res.max_abs_vega_lots, abs(port_vega) / vref_lot)

        # -- 2. re-quote -----------------------------------------------------------
        bb, ba = float(bk.best_bid[k]), float(bk.best_ask[k])
        v = float(g.fair_value[k])
        units = res.positions[i].units
        reason = None
        if gap > p.max_stale_ns:
            reason = "stale_book"
        elif not (np.isfinite(bb) and np.isfinite(ba) and bb < ba):
            reason = "no_usable_book"
        elif not np.isfinite(v):
            reason = "no_fair_value"
        elif v < p.min_premium:
            reason = "premium_below_floor"
        elif p.min_spread_bp > 0 and (ba - bb) / v * 1e4 < p.min_spread_bp:
            reason = "spread_below_cost_floor"
        if reason is not None:
            pull(i, "bid")
            pull(i, "ask")
            stand(reason)
            live_last[i] = False
            continue

        # Skip the arithmetic when nothing that sets the quote has moved: same book
        # touch, same fair value, no fill anywhere since this leg last quoted.
        unchanged = (
            live_last[i] and seen_fills[i] == fill_counter
            and bb == bk.best_bid[k - 1] and ba == bk.best_ask[k - 1]
            and v == g.fair_value[k - 1]
            and (p.imbalance_pull >= 1.0 or imb[i][k] == imb[i][k - 1])
        )
        if unchanged:
            if resting[i]["bid"] is not None or resting[i]["ask"] is not None:
                res.quoted_events += 1
            continue
        seen_fills[i] = fill_counter
        live_last[i] = True

        tick = g.tick
        delta_lots = port_delta / lot
        shift = p.delta_skew * delta_lots * cur_delta[i] + p.leg_skew_ticks * (units / lot) * tick
        vega_lots = port_vega / vref_lot if vref_lot > 0 else 0.0
        if vref > 0:
            shift += p.vega_skew * vega_lots * (cur_vega[i] / vref)
        closing = (session_end_ns is not None and p.close_only_last_s > 0
                   and t >= session_end_ns - p.close_only_last_s * NS)

        size = p.size_lots * g.lot_size
        if g.freeze_qty > 0:
            size = min(size, (g.freeze_qty // g.lot_size) * g.lot_size)

        quoted = False
        for side in ("bid", "ask"):
            if side == "bid":
                px = np.floor((raw_bid[i][k] - shift) / tick + 1e-9) * tick
                best_allowed = bb + tick if p.improve and bb + tick < ba - 1e-9 else bb
                px = min(px, best_allowed)
                behind = (bb - px) / tick
                crosses = px >= ba - 1e-9
                d_after = port_delta + size * cur_delta[i]
                v_after = port_vega + size * cur_vega[i]
                cap_hit = units + size > p.leg_cap_lots * g.lot_size
                reduces = units < 0
            else:
                px = np.ceil((raw_ask[i][k] - shift) / tick - 1e-9) * tick
                best_allowed = ba - tick if p.improve and ba - tick > bb + 1e-9 else ba
                px = max(px, best_allowed)
                behind = (px - ba) / tick
                crosses = px <= bb + 1e-9
                d_after = port_delta - size * cur_delta[i]
                v_after = port_vega - size * cur_vega[i]
                cap_hit = units - size < -p.leg_cap_lots * g.lot_size
                reduces = units > 0

            why = None
            if crosses:
                why = "would_cross"
            elif p.imbalance_pull < 1.0 and (
                (side == "bid" and imb[i][k] < -p.imbalance_pull)
                or (side == "ask" and imb[i][k] > p.imbalance_pull)
            ):
                why = "imbalance"
            elif behind > p.max_behind_ticks + 1e-9:
                why = "edge_below_cost"
            elif cap_hit:
                why = "leg_cap"
            elif (abs(d_after) > p.delta_limit_lots * lot
                  and abs(d_after) > abs(port_delta)):
                why = "delta_limit"
                res.delta_pinned_events += 1
            elif (vref_lot > 0 and abs(v_after) > p.vega_limit_lots * vref_lot
                  and abs(v_after) > abs(port_vega)):
                why = "vega_limit"
                res.vega_pinned_events += 1
            elif closing and not reduces:
                why = "close_only"
            if why is not None:
                pull(i, side)
                stand(why)
                continue

            o = resting[i][side]
            if o is not None and abs(o.price - px) < 1e-9 and o.live:
                quoted = True  # unchanged price: keep the order and its queue place
                continue
            pull(i, side)
            o = sims[i].place(
                security_id=g.security_id, side=side, price=float(px), quantity=size,
                now_ns=t, bid_px=bk.bid_px[k], bid_qty=bk.bid_qty[k],
                ask_px=bk.ask_px[k], ask_qty=bk.ask_qty[k],
            )
            resting[i][side] = o
            order_seq[id(o)] = next_order[0]
            next_order[0] += 1
            quoted = True
        if quoted:
            res.quoted_events += 1

    # -- close marks --------------------------------------------------------------
    last = [np.flatnonzero(np.isfinite(m)) for m in micro]
    res.close_marks["micro"] = np.array([m[j[-1]] if len(j) else np.nan for m, j in zip(micro, last)])
    fv_last = [np.flatnonzero(np.isfinite(g.fair_value)) for g in legs]
    res.close_marks["fair_value"] = np.array(
        [g.fair_value[j[-1]] if len(j) else np.nan for g, j in zip(legs, fv_last)])
    # Liquidation: longs sold at the last bid, shorts bought at the last ask.
    liq = []
    for g, pos in zip(legs, res.positions):
        ok = np.flatnonzero(np.isfinite(g.book.best_bid) & np.isfinite(g.book.best_ask))
        if not len(ok):
            liq.append(np.nan)
            continue
        j = ok[-1]
        liq.append(g.book.best_bid[j] if pos.units > 0 else g.book.best_ask[j])
    res.close_marks["liquidation"] = np.array(liq)
    return res


def summarise(res: PortfolioResult, *, forward_t: np.ndarray | None = None,
              forward: np.ndarray | None = None) -> dict:
    """Every number the report needs, for both cost profiles."""
    legs, fills = res.legs, res.fills
    out: dict = {"fills": len(fills), "events": res.events, "quoted_events": res.quoted_events,
                 "stand_down": dict(sorted(res.stand_down.items(), key=lambda x: -x[1])),
                 "max_abs_delta_lots": res.max_abs_delta_lots,
                 "delta_pinned_events": res.delta_pinned_events}
    if not fills:
        return out
    side = np.array([1.0 if f.side == "bid" else -1.0 for f in fills])
    qty = np.array([f.qty for f in fills], dtype=float)
    px = np.array([f.price for f in fills])
    mm = np.array([f.mark_micro for f in fills])
    mfv = np.array([f.mark_fv for f in fills])
    leg = np.array([f.leg for f in fills])
    t = np.array([f.t for f in fills], dtype=np.int64)
    s = side * qty
    out["units"] = int(qty.sum())
    out["swept_pct"] = 100 * float(np.mean([f.swept for f in fills]))

    # Costs, per executed ORDER for brokerage (DECISIONS #12): group fills by order.
    costs = {}
    for prof in ("member", "dhan"):
        m = CostModel.for_profile(prof)
        orders: dict[int, object] = {}
        for f in fills:
            oc = orders.get(f.order_id)
            if oc is None:
                oc = orders[f.order_id] = m.order(Product.OPTIONS,
                                                  "buy" if f.side == "bid" else "sell")
            oc.add_fill(f.price, f.qty)
        costs[prof] = float(sum(o.total for o in orders.values()))
    out["orders"] = len({f.order_id for f in fills})

    close_mark = {k: v for k, v in res.close_marks.items()}
    units_end = res.final_units()
    for mk in ("micro", "fair_value", "liquidation"):
        g = res.gross(mk)
        out[f"gross_{mk}"] = g
    # The liquidation mark also pays the closing trade's statutory cost.
    liq_cost = 0.0
    mcost = CostModel.for_profile("member")
    for u, x in zip(units_end, close_mark["liquidation"]):
        if u and np.isfinite(x):
            liq_cost += mcost.fill_cost(Product.OPTIONS, "sell" if u > 0 else "buy", x, abs(u)).total
    out["costs"] = costs
    out["liquidation_close_cost"] = liq_cost
    for prof in ("member", "dhan"):
        out[f"net_{prof}"] = out["gross_micro"] - costs[prof]
    out["net_member_liquidated"] = out["gross_liquidation"] - costs["member"] - liq_cost

    # Decomposition, exact: gross = capture + inventory, per mark convention.
    final_micro = close_mark["micro"][leg]
    ok = np.isfinite(mm) & np.isfinite(final_micro)
    out["spread_capture"] = float(np.sum((s * (mm - px))[ok]))
    out["inventory_pnl"] = float(np.sum((s * (final_micro - mm))[ok]))
    okf = np.isfinite(mfv)
    out["spread_capture_fv"] = float(np.sum((s * (mfv - px))[okf]))
    out["capture_per_unit"] = out["spread_capture"] / out["units"]
    out["cost_per_unit_member"] = costs["member"] / out["units"]
    out["end_abs_units"] = int(np.abs(units_end).sum())

    # Huang-Stoll on the own microprice, pooled over legs, at several horizons.
    hs = {}
    eff = side * (mm - px)
    for tau in (1, 5, 30, 300):
        later = np.full(len(fills), np.nan)
        for li in np.unique(leg):
            sel = leg == li
            b = legs[li].book
            mic = microprice(b.bid_px[:, :1], b.bid_qty[:, :1], b.ask_px[:, :1], b.ask_qty[:, :1])
            j = np.searchsorted(b.recv_wall_ns, t[sel] + tau * NS, side="right") - 1
            later[sel] = mic[np.clip(j, 0, len(b) - 1)]
        real = side * (later - px)
        m2 = np.isfinite(eff) & np.isfinite(real)
        hs[f"{tau}s"] = {
            "effective": float(np.average(eff[m2], weights=qty[m2])),
            "realised": float(np.average(real[m2], weights=qty[m2])),
        }
    out["huang_stoll"] = hs

    # Per leg: where the money is made and lost. Realised at 300 s against the
    # leg's own member cost is the sentence that decides which options to quote.
    mcost = CostModel.for_profile("member")
    later300 = np.full(len(fills), np.nan)
    per_leg = []
    for li in np.unique(leg):
        sel = leg == li
        b = legs[li].book
        mic = microprice(b.bid_px[:, :1], b.bid_qty[:, :1], b.ask_px[:, :1], b.ask_qty[:, :1])
        j = np.searchsorted(b.recv_wall_ns, t[sel] + 300 * NS, side="right") - 1
        later300[sel] = mic[np.clip(j, 0, len(b) - 1)]
        u = qty[sel]
        cst = sum(mcost.fill_cost(Product.OPTIONS, "buy" if f.side == "bid" else "sell",
                                  f.price, f.qty).total for f, m in zip(fills, sel) if m)
        e_ = side[sel] * (mm[sel] - px[sel])
        r_ = side[sel] * (later300[sel] - px[sel])
        okk = np.isfinite(e_) & np.isfinite(r_)
        g_ = legs[li]
        per_leg.append({
            "leg": g_.name, "strike": g_.strike, "cp": "C" if g_.is_call else "P",
            "units": int(u.sum()), "buys": int(u[side[sel] > 0].sum()),
            "eff": float(np.average(e_[okk], weights=u[okk])) if okk.any() else None,
            "real300": float(np.average(r_[okk], weights=u[okk])) if okk.any() else None,
            "cost_per_unit": cst / u.sum(),
            "capture": float(np.nansum(s[sel] * (mm[sel] - px[sel]))),
            "inventory": float(np.nansum(s[sel] * (final_micro[sel] - mm[sel]))),
            "cost": cst, "end_units": int(res.positions[li].units),
            "median_price": float(np.median(px[sel])),
        })
    per_leg.sort(key=lambda r: (r["strike"], r["cp"]))
    out["legs"] = per_leg

    # Directional exposure: regress per-minute PnL increments on forward changes.
    if forward is not None and len(res.equity) > 10:
        et = np.array([e[0] for e in res.equity], dtype=np.int64)
        pnl = np.array([e[1] for e in res.equity])
        j = np.searchsorted(forward_t, et, side="right") - 1
        f = np.where(j >= 0, forward[np.clip(j, 0, len(forward) - 1)], np.nan)
        dp, df = np.diff(pnl), np.diff(f)
        ok = np.isfinite(dp) & np.isfinite(df)
        if ok.sum() > 10 and np.var(df[ok]) > 0:
            beta = float(np.cov(dp[ok], df[ok])[0, 1] / np.var(df[ok], ddof=1))
            r = np.corrcoef(dp[ok], df[ok])[0, 1]
            out["beta_rupees_per_point"] = beta
            out["beta_lots"] = beta / (legs[0].lot_size if legs else 1)
            out["r2"] = float(r * r)
            out["minutes"] = int(ok.sum())
        dl = np.array([e[3] for e in res.equity]) / (legs[0].lot_size if legs else 1)
        out["mean_abs_delta_lots"] = float(np.mean(np.abs(dl)))
        out["mean_vega_rupees"] = float(np.mean([e[4] for e in res.equity]))
    out["max_abs_vega_lots"] = res.max_abs_vega_lots
    return out
