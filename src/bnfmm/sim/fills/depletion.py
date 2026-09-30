"""Snapshot-to-snapshot fill simulation for passive quotes.

WHY THIS IS NOT A QUEUE-POSITION MODEL
    The honest answer to "did my order fill?" needs the order book's *event*
    stream: every add, cancel and trade in sequence, so a resting order's queue
    position can be tracked and depleted. Dhan's 20-level feed does not provide
    that. Measured on the captured bytes (2026-08-24, 48 instruments):

      * The feed pushes a complete snapshot of every subscribed instrument on a
        fixed ~200 ms cadence (median 201 ms, p05 157 ms, p95 442 ms), not an
        event-driven delta stream. Payload sizes are all multiples of 332 bytes
        and the median payload is exactly 96 frames = 48 instruments x 2 sides.
      * So between two snapshots an unknown number of adds, cancels and trades
        collapse into one net difference per level. If level 1 went from 120 units
        to 90, that is consistent with a 30-unit trade, a 30-unit cancel, or a
        90-unit trade plus a 60-unit add.
      * The trade tape is separately recoverable by differencing the quote feed's
        cumulative volume counter (see ``book.tape``), but at a coarser cadence
        (~1.66 packets/second/instrument) and without aggressor side, which is
        inferred.

    A simulator that claimed queue positions from this data would be inventing
    the one thing the data cannot supply, and that invention would flatter the
    results in exactly the direction that matters. So this module models what the
    data supports -- *snapshot-to-snapshot depletion* -- and states its assumptions
    in the open, where they can be argued with.

THE MODEL
    A resting passive order at price ``P`` on side ``S`` fills over an interval
    when the tape shows volume that must have traded at or through ``P`` against
    orders on ``S``, and that volume exceeds the queue ahead of us.

    Between snapshot ``t`` and ``t+1``, for a bid at price ``P``:

    1.  *Trade-through.* If any classified sell-aggressor volume printed at a
        price <= P, that volume hit the bid side at or below our price. Our order
        is at the front of nothing; the queue ahead is what the book showed
        resting at P and better.

    2.  *Queue ahead.* ``Q_ahead`` is the displayed quantity at prices strictly
        better than ``P``, plus the quantity at ``P`` itself (we joined the back
        of that queue -- the pessimistic and correct default, since our order
        arrived after everything the snapshot displayed).

    3.  *Depletion.* Cumulative eligible volume since the order rested is tracked.
        We fill when it exceeds ``Q_ahead``, and only for the excess:

            filled = clip(cum_volume - Q_ahead, 0, order_size)

        This is the standard "volume must first exhaust the queue in front of you"
        rule. It is conservative in that it never lets us jump the displayed
        queue, and optimistic in that it assumes the whole eligible print was
        available to the price level rather than partly consumed by hidden or
        newly-arrived orders ahead of us.

    4.  *Sweep.* If the book's best price on our side moves strictly through ``P``
        while the queue that was ahead of us has demonstrably cleared, the level was
        taken out and any unfilled remainder fills at ``P`` (never better: a passive
        order is filled at its own limit). This rule is deliberately narrow -- see
        ``DepletionSimulator._swept`` for the three conditions and for what a looser
        version of it did to the measured effective spread.

    A quote placed *inside* the displayed spread is a distinct and common case: it
    is alone at a price the book never showed, so ``Q_ahead`` is zero and rule 3
    fills it from the first eligible unit. That is the intended behaviour, and it is
    why rule 4 must not also apply there.

WHAT THIS OVERSTATES AND UNDERSTATES, EXPLICITLY
    Overstates fills:
      * Hidden/iceberg orders ahead of us are invisible, so ``Q_ahead`` is a lower
        bound and we fill sooner than reality.
      * Orders that joined our price level between snapshots are invisible; in
        reality some of them are ahead of us in time.
      * ``book.tape`` attributes an interval's whole volume to the last print's
        price, so a sweep that walked three levels is booked at the final price,
        crediting a resting order at that price with volume that actually
        executed elsewhere.
    Understates fills:
      * Volume whose aggressor side could not be classified is discarded rather
        than split (8.3% of units on the 1 DTE at-the-money call, 1.2% on expiry
        day).
      * We always join the back of the queue at our price, never the front, even
        when we would have been the first order at a newly-created level.
      * Fills during a feed gap are refused outright (see ``max_stale_ns``).

    These do not cancel out and are not claimed to. They are reported per run in
    ``FillStats`` so a reader can see how much of the result rests on them, and
    the sensitivity of the headline number to the queue rule is a first-class
    output (see ``sim.fills.sensitivity``).

STALENESS
    A quote may only be live against a fresh book. If the gap since the last
    snapshot exceeds ``max_stale_ns`` the simulator refuses to fill and marks the
    interval stale, because a fill inferred across a 88-second data gap -- the
    largest single gap on 2026-08-25 -- is fiction. The refused volume is counted,
    not silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...book.tape import BUY, SELL

#: Default staleness ceiling. The feed's measured p95 inter-snapshot gap is
#: ~442 ms, so 1 second is ~2x the p95 -- loose enough not to fight normal
#: jitter, tight enough that a real dropout stands the quoter down.
DEFAULT_MAX_STALE_NS = 1_000_000_000

#: Which side of the tape can fill which side of our quote. A resting BID is
#: taken out by a SELL aggressor; a resting ASK by a BUY aggressor.
FILLS_BID = SELL
FILLS_ASK = BUY


@dataclass(slots=True)
class FillStats:
    """Diagnostics for one simulated run. Every number here is a caveat made countable."""

    intervals: int = 0
    intervals_stale: int = 0
    intervals_no_book: int = 0
    eligible_units: int = 0
    unclassified_units_skipped: int = 0
    stale_units_skipped: int = 0
    filled_units: int = 0
    fills: int = 0
    swept_fills: int = 0
    queue_jumped_units: int = 0

    def as_dict(self) -> dict[str, float]:
        d = {f: getattr(self, f) for f in self.__slots__}
        if self.eligible_units:
            d["fill_ratio_pct"] = 100.0 * self.filled_units / self.eligible_units
        if self.intervals:
            d["stale_pct"] = 100.0 * self.intervals_stale / self.intervals
        return d


@dataclass(slots=True)
class Fill:
    """One execution of a passive order."""

    recv_wall_ns: int
    security_id: int
    side: str  # "bid" (we bought) or "ask" (we sold)
    price: float
    quantity: int
    swept: bool  # filled by a level sweep rather than queue depletion
    queue_ahead: int  # displayed size ahead of us when the order rested
    cum_volume: int  # eligible volume accumulated when this fill triggered


def queue_ahead(
    price: float,
    side: str,
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
) -> int:
    """Displayed units at or better than ``price`` on our own side of the book.

    "Better" means a higher bid or a lower ask -- those orders execute before ours.
    Size at exactly ``price`` counts as ahead of us: our order arrived after the
    snapshot was taken, so it joined the back of that queue. That is the
    pessimistic reading and the defensible default; ``sim.fills.sensitivity``
    measures what happens under the optimistic one.
    """
    if side == "bid":
        px, qty = bid_px, bid_qty
        at_or_better = np.isfinite(px) & (px >= price - 1e-9)
    else:
        px, qty = ask_px, ask_qty
        at_or_better = np.isfinite(px) & (px <= price + 1e-9)
    return int(qty[at_or_better].sum())


def eligible_volume(
    price: float,
    side: str,
    tape_price: np.ndarray,
    tape_qty: np.ndarray,
    tape_aggressor: np.ndarray,
) -> np.ndarray:
    """Per-print volume that could have executed against a resting order at ``price``.

    A bid at ``P`` can only be filled by a seller who printed at or below ``P``.
    Prints whose aggressor is unknown are excluded -- see the module docstring's
    accounting of which direction each exclusion biases.
    """
    if side == "bid":
        hits = (tape_aggressor == FILLS_BID) & (tape_price <= price + 1e-9)
    else:
        hits = (tape_aggressor == FILLS_ASK) & (tape_price >= price - 1e-9)
    return np.where(hits, tape_qty, 0)


@dataclass(slots=True)
class RestingOrder:
    """A passive limit order live in the simulation.

    ``queue_ahead`` is fixed when the order is placed and then depleted, rather
    than re-read from each snapshot. That is deliberate: re-reading would let a
    *cancel* by someone ahead of us count as progress toward our fill, and a
    cancel does not execute anything. Depleting only against traded volume keeps
    the accounting causal. The cost is that we do not benefit from cancels ahead
    of us, which genuinely do improve queue position in reality -- another
    understatement, listed with the others.
    """

    security_id: int
    side: str  # "bid" | "ask"
    price: float
    quantity: int
    placed_ns: int
    queue_ahead: int
    filled: int = 0
    cum_volume: int = 0

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled

    @property
    def live(self) -> bool:
        return self.remaining > 0


class DepletionSimulator:
    """Fill passive orders against a snapshot book and an inferred tape.

    Usage is a single forward pass over a common clock: place orders, advance to
    the next snapshot, collect fills. The simulator never looks ahead -- every
    decision at time ``T`` uses only the book and tape observed at or before
    ``T`` -- which is what makes the resulting PnL a backtest rather than a
    curve fit.
    """

    def __init__(
        self,
        *,
        max_stale_ns: int = DEFAULT_MAX_STALE_NS,
        queue_at_price_ahead: bool = True,
        allow_sweep_fill: bool = True,
    ) -> None:
        self.max_stale_ns = max_stale_ns
        # False = optimistic: assume we were first at our own price level.
        self.queue_at_price_ahead = queue_at_price_ahead
        self.allow_sweep_fill = allow_sweep_fill
        self.stats = FillStats()
        self._orders: list[RestingOrder] = []

    # -- order management ----------------------------------------------------

    def place(
        self,
        *,
        security_id: int,
        side: str,
        price: float,
        quantity: int,
        now_ns: int,
        bid_px: np.ndarray,
        bid_qty: np.ndarray,
        ask_px: np.ndarray,
        ask_qty: np.ndarray,
    ) -> RestingOrder:
        """Rest a new passive order, fixing its queue position from the book now."""
        ahead = queue_ahead(price, side, bid_px, bid_qty, ask_px, ask_qty)
        if not self.queue_at_price_ahead:
            # Optimistic variant: discount the size at our own price level, so we
            # sit at the front of our level instead of the back.
            own = _size_at(price, side, bid_px, bid_qty, ask_px, ask_qty)
            ahead = max(0, ahead - own)
        order = RestingOrder(
            security_id=security_id,
            side=side,
            price=price,
            quantity=quantity,
            placed_ns=now_ns,
            queue_ahead=ahead,
        )
        self._orders.append(order)
        return order

    def cancel_all(self) -> None:
        """Pull every resting order. A market maker re-quotes, it does not accumulate."""
        self._orders.clear()

    def cancel(self, order: RestingOrder) -> None:
        """Pull one order. Leaving the others resting is what keeps queue priority.

        Cancel-and-replace at an unchanged price sends an order to the *back* of
        its level. A simulator that does that every snapshot (as ``run_day`` does)
        never accumulates queue position, so it can only be filled when an interval
        clears the whole displayed level -- i.e. by the large, informed flow a
        market maker most wants to avoid.
        """
        try:
            self._orders.remove(order)
        except ValueError:
            pass

    @property
    def resting(self) -> list[RestingOrder]:
        return [o for o in self._orders if o.live]

    # -- the forward step ----------------------------------------------------

    def step(
        self,
        *,
        now_ns: int,
        prev_ns: int,
        tape_price: np.ndarray,
        tape_qty: np.ndarray,
        tape_aggressor: np.ndarray,
        best_bid: float,
        best_ask: float,
    ) -> list[Fill]:
        """Advance one snapshot interval and return the fills it produced.

        ``tape_*`` are the prints observed in ``(prev_ns, now_ns]``. ``best_bid``
        and ``best_ask`` are the book *after* the interval, used only for the
        sweep test.
        """
        self.stats.intervals += 1
        fills: list[Fill] = []
        if not self._orders:
            return fills

        gap = now_ns - prev_ns
        if gap > self.max_stale_ns:
            # A dropout. Refusing to fill is the honest choice; count what we
            # refused so the cost of the decision is visible.
            self.stats.intervals_stale += 1
            self.stats.stale_units_skipped += int(tape_qty.sum())
            return fills

        for order in self._orders:
            if not order.live:
                continue
            vol = eligible_volume(
                order.price, order.side, tape_price, tape_qty, tape_aggressor
            )
            total = int(vol.sum())
            if total:
                order.cum_volume += total
                self.stats.eligible_units += total

            # Queue depletion: fill only the excess over the queue ahead.
            target = min(order.quantity, max(0, order.cum_volume - order.queue_ahead))
            gained = target - order.filled
            swept = False

            if self.allow_sweep_fill and order.remaining > 0 and total:
                # `total` is required: a level that vanished with no prints in the
                # interval was cancelled, not executed.
                swept = self._swept(order, best_bid, best_ask)
                if swept:
                    gained = order.remaining

            if gained <= 0:
                continue
            order.filled += gained
            self.stats.filled_units += gained
            self.stats.fills += 1
            if swept:
                self.stats.swept_fills += 1
            fills.append(
                Fill(
                    recv_wall_ns=now_ns,
                    security_id=order.security_id,
                    side=order.side,
                    price=order.price,
                    quantity=gained,
                    swept=swept,
                    queue_ahead=order.queue_ahead,
                    cum_volume=order.cum_volume,
                )
            )

        return fills

    def _swept(self, order: RestingOrder, best_bid: float, best_ask: float) -> bool:
        """Did our price level trade through, such that the remainder must have filled?

        Three conditions, all necessary. Dropping any one of them makes the rule
        manufacture fills, which is exactly what an earlier version of it did:

        1.  *There was a queue at our price* (``queue_ahead > 0``). A quote placed
            inside the spread is alone at a price level the book never displayed, so
            there is no level to "trade through" -- ordinary depletion already fills
            it from the first eligible unit, correctly and generously. Without this
            guard the test below is *permanently* true for any book-improving quote,
            because the displayed touch is by construction worse than our price. On
            the 1 DTE at-the-money call the quoter improves the book 79.5% of the
            time (its median spread is 11 ticks against a 2-tick half-spread), so
            omitting this condition made 84% of all fills sweeps and drove the
            measured effective half-spread negative -- the unmistakable signature of
            a simulator paying itself.

        2.  *The queue in front really did clear* (``cum_volume >= queue_ahead``).
            The sweep may only accelerate the tail of a fill whose queue has already
            been proven consumed by observed volume. Otherwise one printed unit
            fills a whole order.

        3.  *The touch has moved strictly through our price.* A bid at ``P`` is swept
            only once the book's best bid ends below ``P``; there is then nothing
            left at ``P`` for us to be resting behind.

        The caller additionally requires trade volume in the interval, because a
        level that vanished with no prints was cancelled, not executed.
        """
        if order.queue_ahead <= 0 or order.cum_volume < order.queue_ahead:
            return False
        if order.side == "bid":
            return bool(np.isfinite(best_bid) and best_bid < order.price - 1e-9)
        return bool(np.isfinite(best_ask) and best_ask > order.price + 1e-9)


def _size_at(
    price: float,
    side: str,
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
) -> int:
    """Displayed units at exactly ``price`` on our side."""
    px, qty = (bid_px, bid_qty) if side == "bid" else (ask_px, ask_qty)
    at = np.isfinite(px) & (np.abs(px - price) < 1e-9)
    return int(qty[at].sum())
