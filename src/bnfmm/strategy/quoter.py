"""A two-sided option quoter with inventory-skew risk control.

THE STRATEGY
    Quote both sides around a fair value derived from put-call parity, not from
    the last trade or the futures book (see ``fairvalue.parity`` for why both are
    rejected). Each cycle:

        1.  Read fair value ``F`` for the expiry, and the option's own book.
        2.  Convert ``F`` to a theoretical option value ``V`` -- here by the
            *parity-anchored* route: the option's own microprice, recentred so
            that the call and put of a strike are consistent with ``F``. This
            avoids needing a volatility model to quote (see ``theo``).
        3.  Skew the quote by inventory: a long position lowers both sides so the
            ask is more attractive and the bid less so, which makes flattening
            the more likely fill. This *is* the delta hedge -- see below.
        4.  Place bid at ``V - half_spread - skew`` and ask at
            ``V + half_spread - skew``, rounded to the tick, sized in lots.
        5.  Stand down entirely when the book is unusable, the data is stale, or
            inventory has hit its hard cap.

WHY INVENTORY SKEW IS THE DELTA HEDGE
    A conventional option market maker hedges delta by trading the underlying.
    That is not available here: the project places no orders (``BRIEFING.md`` §5),
    and more practically the front-month future's book was measured to be
    unusable -- median spread 41 ticks and 2.2% of snapshots crossed, one-sided,
    or carrying outright garbage prices (a Rs 0.60 ask against a Rs 57,780 bid).
    Hedging into that book would cost more than the edge being captured.

    So risk is controlled by *not accumulating it*: the skew term biases quotes so
    that mean reversion in our own fill flow flattens the book. The cost is that
    we carry directional risk between fills, which is precisely what the adverse
    selection measured by the Huang-Stoll decomposition captures. This is a
    deliberate, stated trade-off, not an omission.

SIZING AND THE EXCHANGE'S LIMITS
    Quote size is in lots (BANKNIFTY lot = 30 units) and capped by the exchange
    freeze quantity (601 units = 20 lots), which is a hard limit on a single
    order and is read from the contract, never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class QuoteParams:
    """Everything that shapes a quote. Defaults are starting points, not results.

    ``half_spread_ticks`` is the edge we ask for. It must clear the round-trip
    cost floor or the strategy cannot be profitable at any fill rate -- on Dhan's
    Rs 20 flat brokerage the breakeven in ticks scales with premium, so the floor
    is computed per contract by ``sim.costs`` rather than assumed here.
    """

    half_spread_ticks: float = 2.0
    #: Lots per side per quote.
    size_lots: int = 1
    #: Hard inventory cap in lots. Reaching it pulls the side that would add to it.
    max_position_lots: int = 10
    #: Rupees of skew applied per lot of inventory, as a multiple of one tick.
    skew_ticks_per_lot: float = 0.5
    #: Refuse to quote when the book's own spread is tighter than our edge: we
    #: would have to cross to be competitive, which is no longer market making.
    min_book_spread_ticks: float = 1.0
    #: Refuse to quote an option priced below this. At a Rs 0.05 tick, a Rs 0.30
    #: option cannot support a 2-tick half spread without the cost floor eating
    #: it, and its book is dominated by tick-level noise.
    min_premium: float = 1.0


@dataclass(slots=True)
class Quote:
    """One side of a two-sided quote, or a deliberate stand-down."""

    side: str  # "bid" | "ask"
    price: float | None  # None = not quoting this side
    quantity: int  # units, a multiple of the lot size
    reason: str = ""  # why we stood down, when price is None


def round_to_tick(price: float, tick: float, *, side: str) -> float:
    """Round a quote to a tradeable price, always away from the market.

    A bid rounds *down* and an ask rounds *up*. Rounding to nearest would let the
    simulator quote a price a fraction of a tick better than we could really have
    achieved, and that fraction is pure invented edge. Over ~30,000 quotes a day
    it would matter.
    """
    if not np.isfinite(price) or tick <= 0:
        return float("nan")
    n = price / tick
    # A hair of tolerance so a price already on the tick grid is not pushed off it
    # by floating-point representation (0.05 is not exact in binary).
    if side == "bid":
        return float(np.floor(n + 1e-9) * tick)
    return float(np.ceil(n - 1e-9) * tick)


def theo(
    micro: float,
    parity_adjustment: float = 0.0,
) -> float:
    """Theoretical option value: the option's own microprice, parity-adjusted.

    Deriving an option value from a forward requires a volatility model, and any
    such model becomes the dominant source of error at a 200 ms cadence -- a
    mis-specified smile would produce systematic, one-sided quoting that looks
    like alpha until it is not. The parity-anchored alternative is to take the
    option's *own* book microprice as the value, and correct it only by the amount
    that the strike's call and put jointly disagree with the cross-strike forward.

    That correction is model-free (it is parity again) and small, so the quote is
    anchored on the option's own liquidity while still being pulled toward the
    consensus forward when its own book drifts.
    """
    return micro + parity_adjustment


def skew(position_lots: float, params: QuoteParams, tick: float) -> float:
    """Rupees to shift both quotes by, given inventory.

    Long inventory returns a *positive* number, which is subtracted from both
    sides: our ask becomes cheaper (likelier to be hit, flattening us) and our
    bid becomes lower (less likely to be hit, so we stop adding). The relation is
    linear in inventory, which is the standard first-order control and is what the
    Avellaneda-Stoikov reservation price reduces to for small inventory.
    """
    return position_lots * params.skew_ticks_per_lot * tick


def make_quotes(
    *,
    micro: float,
    book_bid: float,
    book_ask: float,
    tick: float,
    lot_size: int,
    freeze_qty: int,
    position_lots: float,
    params: QuoteParams,
    parity_adjustment: float = 0.0,
    stale: bool = False,
) -> tuple[Quote, Quote]:
    """Build the two-sided quote, or stand down with a stated reason.

    Every stand-down carries a reason string so the run report can attribute
    inactivity to a cause -- a strategy that quoted 3% of the day because its
    premium floor was too high looks identical in PnL to one that found no edge,
    and the two demand opposite fixes.
    """
    def down(reason: str) -> tuple[Quote, Quote]:
        return (
            Quote("bid", None, 0, reason),
            Quote("ask", None, 0, reason),
        )

    if stale:
        return down("stale_book")
    if not (np.isfinite(micro) and np.isfinite(book_bid) and np.isfinite(book_ask)):
        return down("no_two_sided_book")
    if book_bid >= book_ask:
        return down("crossed_book")
    if micro < params.min_premium:
        return down("premium_below_floor")
    book_spread = book_ask - book_bid
    if book_spread < params.min_book_spread_ticks * tick - 1e-9:
        return down("book_tighter_than_edge")

    value = theo(micro, parity_adjustment)
    edge = params.half_spread_ticks * tick
    shift = skew(position_lots, params, tick)

    size = params.size_lots * lot_size
    if freeze_qty > 0:
        size = min(size, (freeze_qty // lot_size) * lot_size)

    bid_px = round_to_tick(value - edge - shift, tick, side="bid")
    ask_px = round_to_tick(value + edge - shift, tick, side="ask")

    bid = Quote("bid", bid_px, size)
    ask = Quote("ask", ask_px, size)

    # Never quote a price that would cross the book -- that is an aggressive order,
    # which the fill model does not simulate and which this project does not do.
    if bid_px >= book_ask:
        bid = Quote("bid", None, 0, "would_cross_ask")
    if ask_px <= book_bid:
        ask = Quote("ask", None, 0, "would_cross_bid")

    # Inventory caps: pull only the side that would make the position worse.
    if position_lots >= params.max_position_lots:
        bid = Quote("bid", None, 0, "long_cap")
    if position_lots <= -params.max_position_lots:
        ask = Quote("ask", None, 0, "short_cap")

    return bid, ask
