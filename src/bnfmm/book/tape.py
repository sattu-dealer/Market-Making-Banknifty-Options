"""Reconstruct a trade tape from the quote feed's cumulative volume.

WHY THIS EXISTS
    Dhan gives no trade-by-trade stream on either socket. The 20-level depth feed
    carries no volume at all, and the general feed's Full packet carries only a
    *cumulative* day volume plus the last traded price. So the tape has to be
    differenced out of the volume counter:

        traded units in (t-1, t]  =  volume_t - volume_{t-1}
        a price for those units   =  ltp_t          (the *last* print, not all)

    This is the single most important input to the fill simulator after the book
    itself: a passive order only fills when someone trades through it, so without
    volume there is nothing to deplete a queue with.

WHAT IS LOST, STATED PLAINLY
    The differencing is lossy in three specific ways, and every one of them is a
    limitation of the *data*, not of this code:

    1.  Several trades can occur between two packets (measured ~1.66 Full packets
        per second per instrument on 2026-08-24, versus a 200 ms depth cadence).
        We learn their total size but only the last one's price. So a burst that
        swept three levels is indistinguishable from one trade at the final price.
        Consequence: the simulator treats the interval's volume as executing at
        ``ltp_t``, which *understates* how deep a sweep went. That is the
        conservative direction for a passive quote resting away from the touch,
        and the optimistic direction for one at the touch -- so it is reported as
        an assumption, not buried.

    2.  Aggressor side is not published. It is inferred (see ``classify``) by the
        Lee-Ready quote rule against the prevailing depth book, falling back to a
        tick test. Published accuracy of this family of rules on liquid markets is
        high but not perfect, and misclassification moves volume from one side of
        the book to the other.

    3.  ``last_trade_epoch`` has one-second resolution, so it cannot order trades
        within a second. This module therefore keys everything off the capture's
        own ``recv_wall_ns``, which is nanosecond and monotone per socket, and
        uses the exchange clock only as a cross-check.

CUMULATIVE COUNTERS RESET AND REWIND
    ``volume`` is a day counter, so the first packet of a session carries the
    volume already traded before the capture started -- differencing that against
    zero would invent a trade of the whole morning's volume. The first observation
    is therefore consumed as a baseline and never emitted.

    Counters can also go *backwards*, when the server replays a stale packet after
    a fresher one -- the stale packet carries an older ``last_trade_epoch`` too, so
    it is a replay, not a correction. Differencing packet to packet would drop the
    negative step and then count the climb back to the true level as *new* volume:
    volume that already traded, booked a second time. So traded quantity is the
    increment of the counter's **running maximum**, and a packet whose counter is
    below that maximum contributes nothing. Measured before this rule existed
    (``reports/data_quality.md``): phantom volume was <=0.01% of units on every
    day except 2026-09-04, where a replay burst made it 5.8%.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from ..data import store

#: Aggressor classification codes.
BUY = 1  # trade initiated by a buyer -- lifts the ask, hits resting sell orders
SELL = -1  # trade initiated by a seller -- hits the bid, hits resting buy orders
UNKNOWN = 0


@dataclass(frozen=True, slots=True)
class Tape:
    """Inferred trades for one instrument on one day.

    One entry per quote packet that showed a positive volume increment. ``price``
    is the last print in the interval, not a volume-weighted average, because a
    VWAP is not recoverable from the feed.
    """

    security_id: int
    trading_date: date
    recv_wall_ns: np.ndarray  # int64, when we learned of the trade(s)
    price: np.ndarray  # float64, ltp at that packet
    quantity: np.ndarray  # int64, units traded since the previous packet
    aggressor: np.ndarray  # int8, BUY / SELL / UNKNOWN
    last_trade_epoch: np.ndarray  # int64, exchange seconds; cross-check only
    rewinds: int  # count of packets whose counter went backwards (replays)
    baseline_volume: int  # volume already traded before capture began

    def __len__(self) -> int:
        return len(self.recv_wall_ns)

    @property
    def signed_quantity(self) -> np.ndarray:
        """Quantity signed by aggressor: positive buys, negative sells."""
        return self.quantity * self.aggressor

    def volume_by_side(self) -> tuple[int, int, int]:
        """``(buy_units, sell_units, unclassified_units)``."""
        return (
            int(self.quantity[self.aggressor == BUY].sum()),
            int(self.quantity[self.aggressor == SELL].sum()),
            int(self.quantity[self.aggressor == UNKNOWN].sum()),
        )


def classify(
    price: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
) -> np.ndarray:
    """Infer aggressor side by the Lee-Ready rule, with a tick-test fallback.

    The quote rule: a print above the prevailing mid was buyer-initiated, below it
    seller-initiated. Prints exactly at the mid carry no information, so they fall
    back to the tick test -- compare with the previous *different* price, and call
    an uptick a buy. Prints with no prevailing two-sided quote stay ``UNKNOWN``
    rather than being guessed at; the simulator declines to fill against volume it
    cannot attribute, which is the conservative choice.

    ``bid`` and ``ask`` must be the book *as of just before* the print, carried
    forward from the depth feed. Using the post-trade book would leak the future
    into the classification.
    """
    out = np.full(len(price), UNKNOWN, dtype=np.int8)
    with np.errstate(invalid="ignore"):
        mid = (bid + ask) / 2.0
        have = np.isfinite(mid) & np.isfinite(price)
        out[have & (price > mid)] = BUY
        out[have & (price < mid)] = SELL

        # Tick test for at-the-mid prints: carry the last strictly different price
        # forward and compare. A leading run with no predecessor stays UNKNOWN.
        at_mid = have & (price == mid)
        if at_mid.any():
            changed = np.r_[True, price[1:] != price[:-1]]
            # index of the most recent price change strictly before each row
            change_idx = np.maximum.accumulate(np.where(changed, np.arange(len(price)), -1))
            prev_idx = np.where(change_idx == np.arange(len(price)), -1, change_idx)
            # for rows that *are* a change point, the previous distinct price is
            # the change point before them
            roll = np.r_[-1, change_idx[:-1]]
            prev_idx = np.where(prev_idx < 0, roll, prev_idx)
            ok = at_mid & (prev_idx >= 0)
            idx = np.flatnonzero(ok)
            if len(idx):
                prev_px = price[prev_idx[idx]]
                out[idx] = np.where(
                    price[idx] > prev_px, BUY, np.where(price[idx] < prev_px, SELL, UNKNOWN)
                ).astype(np.int8)
    return out


def volume_increments(volume: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """Traded units per packet from a cumulative counter: ``(rows, units, rewinds)``.

    ``rows`` indexes the packets that revealed new volume; ``units`` is how much.
    The first packet is the baseline and is never emitted. Increments are taken on
    the counter's running maximum, so a replayed stale packet and the climb back
    from it contribute nothing -- see the module docstring.
    """
    vol = np.asarray(volume, dtype=np.int64)
    if len(vol) < 2:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), 0
    rewinds = int((np.diff(vol) < 0).sum())
    dvol = np.diff(np.maximum.accumulate(vol))
    # diff position i describes the interval ending at row i+1.
    keep = np.flatnonzero(dvol > 0) + 1
    return keep, dvol[keep - 1], rewinds


def load_tape(
    root: str,
    trading_date: date,
    security_id: int,
    *,
    bid: np.ndarray | None = None,
    ask: np.ndarray | None = None,
    book_wall_ns: np.ndarray | None = None,
) -> Tape:
    """Difference the quote feed's volume counter into a trade tape.

    If a book is supplied (``bid``/``ask``/``book_wall_ns`` from a
    :class:`~bnfmm.book.reconstruct.BookSeries`) the aggressor side is classified
    against the last book snapshot strictly *before* each print. Without a book
    the classification falls back to the 5-level quote block carried in the Full
    packet itself, which is the same exchange data one cadence coarser.
    """
    t = store.read_capture(root, "quotes", dates=[trading_date], security_ids=[security_id])
    if t.num_rows == 0:
        z = np.zeros(0)
        return Tape(security_id, trading_date, z.astype("int64"), z, z.astype("int64"),
                    z.astype("int8"), z.astype("int64"), 0, 0)

    wall = np.asarray(t.column("recv_wall_ns"), dtype=np.int64)
    vol = np.asarray(t.column("volume"), dtype=np.int64)
    ltp = np.asarray(t.column("ltp"), dtype=np.float64)
    lte = np.asarray(t.column("last_trade_epoch"), dtype=np.int64)

    keep, qty, rewinds = volume_increments(vol)

    price = ltp[keep]
    if bid is not None and ask is not None and book_wall_ns is not None and len(book_wall_ns):
        # Last depth snapshot strictly before the print. 'left' - 1 excludes a
        # snapshot stamped at the same nanosecond, which we cannot order against
        # the print and so must not treat as prior knowledge.
        j = np.searchsorted(book_wall_ns, wall[keep], side="left") - 1
        ok = j >= 0
        b = np.full(len(keep), np.nan)
        a = np.full(len(keep), np.nan)
        b[ok] = bid[j[ok]]
        a[ok] = ask[j[ok]]
    else:
        b = _first_level(t, "bid_price")[keep]
        a = _first_level(t, "ask_price")[keep]

    return Tape(
        security_id=security_id,
        trading_date=trading_date,
        recv_wall_ns=wall[keep],
        price=price,
        quantity=qty,
        aggressor=classify(price, b, a),
        last_trade_epoch=lte[keep],
        rewinds=rewinds,
        baseline_volume=int(vol[0]),
    )


def _first_level(t, column: str) -> np.ndarray:
    """Level-1 price from a list column, ``NaN`` where the side was empty."""
    arr = t.column(column).combine_chunks()
    if hasattr(arr, "num_chunks"):
        arr = arr.chunk(0) if arr.num_chunks else None
    if arr is None:
        return np.full(t.num_rows, np.nan)
    offsets = np.asarray(arr.offsets, dtype=np.int64)
    values = np.asarray(arr.values, dtype=np.float64)
    lens = np.diff(offsets)
    out = np.full(len(lens), np.nan)
    ok = lens > 0
    out[ok] = values[offsets[:-1][ok]]
    # The 5-level block pads empty levels with zero rather than omitting them.
    out[out <= 0] = np.nan
    return out
