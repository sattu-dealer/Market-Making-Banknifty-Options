"""Run the quoter over a captured day and account for the result.

THE LOOP
    One forward pass over the depth feed's own snapshot clock. At each snapshot:

        1.  Pull all resting quotes (a market maker re-quotes; it does not leave
            stale orders in the book).
        2.  Advance the fill simulator by one interval, using the prints that
            arrived in that interval, and book any fills.
        3.  Re-quote from the new book and the new inventory.

    Fills are settled *before* re-quoting, so a fill can never be matched against
    a quote that was priced using knowledge of that same fill. Order matters here
    and getting it backwards is the classic backtest look-ahead.

WHAT IS BEING MEASURED
    Not just PnL. A market maker that made money on one day of one strike has
    demonstrated very little; what is informative is the decomposition:

      * *Gross spread capture* -- what the strategy earned from quoting, before
        any cost. This is the raw edge.
      * *Adverse selection* -- how much of that edge was given back because the
        market moved against each fill. Huang-Stoll: the effective spread is
        measured at the fill, the realised spread some horizon later, and the
        difference is what informed flow took. A market maker whose realised
        spread is negative is being picked off.
      * *Inventory PnL* -- mark-to-market on the position carried between fills,
        which is the cost of not hedging delta (see ``strategy.quoter``).
      * *Costs* -- statutory charges plus brokerage, reported paired: once at
        zero brokerage (exchange member) and once at Dhan's Rs 20 flat per order.
        The pairing is a standing project decision, because the flat fee is what
        decides viability at small size and hiding it would flatter the result.

    Marking uses the parity forward's option-implied value, never the last trade,
    for the reasons in ``fairvalue.parity``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from ..book.reconstruct import BookSeries
from ..book.tape import Tape
from ..fairvalue.microprice import microprice
from ..sim.costs import CostModel, Product
from ..sim.fills.depletion import DepletionSimulator, Fill
from ..strategy.quoter import QuoteParams, make_quotes


@dataclass(slots=True)
class Position:
    """Inventory in one contract, with average-cost accounting.

    Realised PnL is booked when a fill reduces the position, using average cost.
    That is the convention a trading desk uses and it keeps realised and unrealised
    additive, so ``realised + unrealised`` is always total PnL with no
    reconciliation term.
    """

    units: int = 0
    avg_price: float = 0.0
    realised: float = 0.0

    def apply(self, side: str, price: float, quantity: int) -> float:
        """Book a fill. Returns the realised PnL it produced."""
        signed = quantity if side == "bid" else -quantity
        realised = 0.0
        if self.units == 0 or (self.units > 0) == (signed > 0):
            # Opening or adding: re-average, nothing realised.
            total = self.units + signed
            self.avg_price = (self.avg_price * self.units + price * signed) / total
            self.units = total
        else:
            # Reducing, and possibly flipping.
            closing = min(abs(signed), abs(self.units))
            direction = 1 if self.units > 0 else -1
            realised = direction * closing * (price - self.avg_price)
            self.units += signed
            if self.units == 0:
                self.avg_price = 0.0
            elif (self.units > 0) != (direction > 0):
                # Flipped through zero: the remainder opens a new position.
                self.avg_price = price
        self.realised += realised
        return realised

    def unrealised(self, mark: float) -> float:
        if self.units == 0 or not np.isfinite(mark):
            return 0.0
        return self.units * (mark - self.avg_price)


@dataclass(slots=True)
class RunResult:
    """Everything one backtest produced, enough to write the report from."""

    security_id: int
    trading_date: date
    snapshots: int
    quoted_snapshots: int
    stand_down: dict[str, int] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    fill_marks: list[float] = field(default_factory=list)  # fair value at each fill
    realised: float = 0.0
    final_units: int = 0
    final_mark: float = float("nan")
    #: How stale the closing mark was, in nanoseconds. Non-zero when the book's
    #: last snapshots were one-sided and the mark fell back to an earlier one.
    final_mark_stale_ns: int = 0
    unrealised: float = 0.0
    max_abs_position: int = 0
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    fill_stats: dict[str, float] = field(default_factory=dict)

    @property
    def gross_pnl(self) -> float:
        return self.realised + self.unrealised

    @property
    def filled_units(self) -> int:
        return sum(f.quantity for f in self.fills)

    @property
    def n_orders(self) -> int:
        """Fills counted as orders for brokerage.

        Dhan charges Rs 20 per *order*, not per fill, so charging per fill
        overstates the fee whenever one order fills in pieces. Counting distinct
        (side, price, placement) groups is the closer accounting; this returns the
        fill count as the conservative upper bound and the report states so.
        """
        return len(self.fills)


def run_day(
    *,
    book: BookSeries,
    tape: Tape,
    params: QuoteParams,
    tick: float,
    lot_size: int,
    freeze_qty: int,
    fair_value: np.ndarray | None = None,
    max_stale_ns: int = 1_000_000_000,
    queue_at_price_ahead: bool = True,
) -> RunResult:
    """Quote one contract across one captured day.

    ``fair_value``, if given, must be on the book's own clock and is used **only to
    price quotes**. Marking is always done at the option's own microprice, never at
    ``fair_value``: the mark decides reported PnL, and marking a position at a value
    the option's own book does not support would book profit we could not realise.
    Keeping the two separate also keeps the Huang-Stoll decomposition honest, since
    the effective and realised half-spreads are then measured against the same
    quantity and their difference is adverse selection alone.
    """
    n = len(book)
    sim = DepletionSimulator(
        max_stale_ns=max_stale_ns, queue_at_price_ahead=queue_at_price_ahead
    )
    pos = Position()
    result = RunResult(
        security_id=book.security_id, trading_date=book.trading_date, snapshots=n,
        quoted_snapshots=0,
    )
    stand_down: dict[str, int] = {}

    micro = microprice(book.bid_px, book.bid_qty, book.ask_px, book.ask_qty)
    mark = micro
    bb, ba = book.best_bid, book.best_ask
    usable = book.usable()

    # Bucket the tape into snapshot intervals once, rather than searching per step.
    # Interval i covers (recv_wall_ns[i-1], recv_wall_ns[i]].
    bucket = np.searchsorted(book.recv_wall_ns, tape.recv_wall_ns, side="left")

    for i in range(1, n):
        prev_ns, now_ns = int(book.recv_wall_ns[i - 1]), int(book.recv_wall_ns[i])

        # 1. Settle fills from the interval that just elapsed, against the quotes
        #    that were resting during it. Before re-quoting: no look-ahead.
        sel = bucket == i
        fills = sim.step(
            now_ns=now_ns,
            prev_ns=prev_ns,
            tape_price=tape.price[sel],
            tape_qty=tape.quantity[sel],
            tape_aggressor=tape.aggressor[sel],
            best_bid=bb[i],
            best_ask=ba[i],
        )
        for f in fills:
            pos.apply(f.side, f.price, f.quantity)
            result.fills.append(f)
            # Mark the fill at the midpoint *prevailing before* it, not after.
            # The fill happened somewhere inside (prev_ns, now_ns]; mark[i] is the
            # book once the interval's move has already completed, and that move is
            # partly the impact of the very flow that filled us. Marking against it
            # charges adverse selection to the effective spread, which is the term
            # meant to measure what we were paid. Huang-Stoll's M_t is the prevailing
            # midpoint, and Lee-Ready deliberately lag it for this same reason; the
            # move from mark[i-1] to mark[i] is then correctly attributed to the
            # realised-spread term instead.
            result.fill_marks.append(float(mark[i - 1]))
            result.max_abs_position = max(result.max_abs_position, abs(pos.units))

        # 2. Re-quote from the fresh book and the post-fill inventory.
        sim.cancel_all()
        stale = (now_ns - prev_ns) > max_stale_ns
        adj = 0.0
        if fair_value is not None and np.isfinite(micro[i]) and np.isfinite(fair_value[i]):
            # Pull the option's own microprice toward the cross-strike consensus.
            # This is the whole parity signal: `fair_value[i] - micro[i]` is the
            # discounted amount by which this strike's own synthetic forward
            # disagrees with the ladder's, and nothing else.
            adj = float(fair_value[i] - micro[i])
        bid, ask = make_quotes(
            micro=float(micro[i]),
            book_bid=float(bb[i]),
            book_ask=float(ba[i]),
            tick=tick,
            lot_size=lot_size,
            freeze_qty=freeze_qty,
            position_lots=pos.units / lot_size,
            params=params,
            parity_adjustment=adj,
            stale=stale or not usable[i],
        )
        placed = False
        for q in (bid, ask):
            if q.price is None:
                stand_down[q.reason] = stand_down.get(q.reason, 0) + 1
                continue
            sim.place(
                security_id=book.security_id,
                side=q.side,
                price=q.price,
                quantity=q.quantity,
                now_ns=now_ns,
                bid_px=book.bid_px[i],
                bid_qty=book.bid_qty[i],
                ask_px=book.ask_px[i],
                ask_qty=book.ask_qty[i],
            )
            placed = True
        if placed:
            result.quoted_snapshots += 1
            if result.quoted_snapshots % 2000 == 0:
                result.equity_curve.append(
                    (now_ns, pos.realised + pos.unrealised(float(mark[i])))
                )

    # The closing mark. The final snapshot of an expiry-day book is frequently
    # one-sided or empty -- liquidity evaporates into the 15:30 close -- which makes
    # `micro[n-1]` NaN. Marking an open position at NaN silently books it at *zero*
    # PnL (see `Position.unrealised`), which on 0DTE can hide a position that still
    # carries real intrinsic value: on 2026-08-25 the 57500 PE ended long 150 units
    # with a NaN mark while the forward sat below the strike. Falling back to the
    # last finite microprice is the conservative repair -- it is a price the book
    # actually showed, rather than an extrapolation or a settlement value we did
    # not capture. `final_mark_stale_ns` reports how old that fallback was so the
    # report can disclose it instead of burying it.
    finite = np.flatnonzero(np.isfinite(mark)) if n else np.zeros(0, dtype=int)
    final_mark = float("nan")
    final_mark_stale_ns = 0
    if len(finite):
        last = int(finite[-1])
        final_mark = float(mark[last])
        final_mark_stale_ns = int(book.recv_wall_ns[n - 1] - book.recv_wall_ns[last])
    result.realised = pos.realised
    result.final_units = pos.units
    result.final_mark = final_mark
    result.final_mark_stale_ns = final_mark_stale_ns
    result.unrealised = pos.unrealised(final_mark)
    result.stand_down = stand_down
    result.fill_stats = sim.stats.as_dict()
    return result


def huang_stoll(
    result: RunResult,
    book: BookSeries,
    *,
    horizon_ns: int = 30_000_000_000,
    horizons_ns: tuple[int, ...] = (
        1_000_000_000,
        5_000_000_000,
        30_000_000_000,
        300_000_000_000,
    ),
) -> dict[str, float]:
    """Effective and realised spread, and the adverse selection between them.

    For a fill at price ``P`` with direction ``d`` (+1 we bought, -1 we sold) and
    fair value ``M`` at the fill:

        effective_half = d * (M_t - P)            -- edge we captured at the fill
        realised_half  = d * (M_{t+tau} - P)      -- edge still there tau later
        adverse        = effective_half - realised_half

    Signs are set so that a *positive* effective half-spread means we bought below
    fair value or sold above it, i.e. we were paid to provide liquidity. Positive
    adverse selection means the market moved against the fill afterwards, which is
    the price of that liquidity. Both are reported per unit of premium, so they can
    be compared against the tick and against the cost floor.

    THE HORIZON CURVE
        ``adverse_by_horizon`` reports the same decomposition at several ``tau``,
        which is what distinguishes two very different diagnoses that a single
        horizon conflates:

          * Adverse selection already large at 1 s and flat thereafter means the
            fills are being *picked off* -- informed flow, or a simulator filling us
            at prices that were never really available.
          * Adverse selection that grows steadily with ``tau`` is *inventory risk*:
            the position is fine at the fill and decays as the underlying trends.
            That is managed by flattening faster, not by quoting wider.

        The distinction decides whether a losing result is a fixable strategy
        problem or a structural one, so it is reported rather than assumed.
    """
    if not result.fills:
        return {"fills": 0}
    mark = np.asarray(result.fill_marks, dtype=float)
    price = np.array([f.price for f in result.fills], dtype=float)
    qty = np.array([f.quantity for f in result.fills], dtype=float)
    d = np.array([1.0 if f.side == "bid" else -1.0 for f in result.fills])
    t = np.array([f.recv_wall_ns for f in result.fills], dtype=np.int64)

    micro = microprice(book.bid_px, book.bid_qty, book.ask_px, book.ask_qty)

    def _realised(tau: int) -> np.ndarray:
        # Fair value `tau` later, from the book's own clock.
        later = np.searchsorted(book.recv_wall_ns, t + tau, side="right") - 1
        later = np.clip(later, 0, len(book) - 1)
        return d * (micro[later] - price)

    eff = d * (mark - price)
    real = _realised(horizon_ns)
    ok = np.isfinite(eff) & np.isfinite(real)
    if not ok.any():
        return {"fills": len(result.fills), "measurable": 0}

    w = qty[ok]
    out = {
        "fills": len(result.fills),
        "measurable": int(ok.sum()),
        "effective_half_spread": float(np.average(eff[ok], weights=w)),
        "realised_half_spread": float(np.average(real[ok], weights=w)),
        "adverse_selection": float(np.average((eff - real)[ok], weights=w)),
        "effective_half_spread_total": float(np.sum(eff[ok] * w)),
        "realised_half_spread_total": float(np.sum(real[ok] * w)),
    }
    curve = {}
    for tau in horizons_ns:
        r = _realised(tau)
        m = np.isfinite(eff) & np.isfinite(r)
        if m.any():
            curve[f"{tau // 1_000_000_000}s"] = float(
                np.average((eff - r)[m], weights=qty[m])
            )
    out["adverse_by_horizon"] = curve
    return out


def decompose_pnl(result: RunResult) -> dict[str, float]:
    """Split gross PnL into spread capture and inventory (directional) PnL.

    This is the decomposition that decides whether a profitable run is actually
    market making. Writing ``s_i`` for the signed quantity of fill ``i`` (positive
    when we bought), ``P_i`` its price, ``M_i`` the midpoint prevailing at the
    fill and ``M_T`` the final mark, total PnL telescopes exactly:

        gross = sum_i s_i * (M_T - P_i)
              = sum_i s_i * (M_i - P_i)  +  sum_i s_i * (M_T - M_i)
              = spread capture           +  inventory PnL

    The first term is what we were paid for providing liquidity -- the quantity a
    market maker is trying to earn, and the one that scales with fill count. The
    second is mark-to-market on positions carried between fills: pure directional
    exposure, which is what an inventory-skewing quoter accumulates when its fair
    value persistently disagrees with the book on one side.

    The split matters because the two have completely different meanings. Spread
    capture is a repeatable edge that survives out of sample if the flow does.
    Inventory PnL on a single day is one realisation of a directional bet, and a
    strategy whose profit is mostly inventory PnL has not been shown to make
    markets -- it has been shown to have been positioned correctly on that day,
    which two days of data cannot distinguish from luck.

    The identity is exact, so ``residual`` is a floating-point check and should be
    within a rupee or two of zero. It is returned rather than asserted because a
    large residual would mean the mark used for fills and the mark used for the
    final position had diverged, which is a bug worth surfacing.
    """
    if not result.fills:
        return {"fills": 0}
    marks = np.asarray(result.fill_marks, dtype=float)
    price = np.array([f.price for f in result.fills], dtype=float)
    signed = np.array(
        [f.quantity if f.side == "bid" else -f.quantity for f in result.fills],
        dtype=float,
    )
    final = result.final_mark
    ok = np.isfinite(marks) & np.isfinite(price)
    if not ok.any() or not np.isfinite(final):
        return {"fills": len(result.fills), "measurable": 0}

    capture = float(np.sum(signed[ok] * (marks[ok] - price[ok])))
    inventory = float(np.sum(signed[ok] * (final - marks[ok])))
    return {
        "fills": len(result.fills),
        "measurable": int(ok.sum()),
        "spread_capture": capture,
        "inventory_pnl": inventory,
        "gross_pnl": result.gross_pnl,
        # Share of gross that came from being positioned, not from quoting. Above
        # ~50% the result is a directional bet wearing a market maker's clothes.
        "inventory_share_pct": (
            100.0 * inventory / result.gross_pnl if result.gross_pnl else float("nan")
        ),
        "residual": result.gross_pnl - capture - inventory,
    }


def apply_costs(
    result: RunResult,
    *,
    profile: str,
    product: Product = Product.OPTIONS,
) -> dict[str, float]:
    """Charge one broker profile against a run's fills.

    Reported for both the zero-brokerage member profile and Dhan's flat fee, as a
    standing project decision: the flat Rs 20 is the term that decides whether the
    strategy is viable at one-lot size, and reporting only the member case would be
    the single most misleading number this project could publish.
    """
    model = CostModel.for_profile(profile)
    total = 0.0
    for f in result.fills:
        side = "buy" if f.side == "bid" else "sell"
        total += model.fill_cost(product, side, f.price, f.quantity).total
    net = result.gross_pnl - total
    return {
        "profile": profile,
        "gross_pnl": result.gross_pnl,
        "costs": total,
        "net_pnl": net,
        "cost_per_fill": total / len(result.fills) if result.fills else 0.0,
    }
