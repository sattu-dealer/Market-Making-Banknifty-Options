"""The freshest causally-known book, from the depth feed and the quote feed together.

WHY THIS EXISTS
    ``reports/data_integrity.md`` finding 2: the 20-level depth feed delivers each
    book state ~225 ms *after* the quote feed (Full packet, 5-level block) delivers
    the same state. Measured by shifting one clock against the other: agreement
    peaks at a depth delay of 200-250 ms on every day, hour and instrument. A
    quoter driven only by the depth feed therefore reacts a quarter-second late to
    the trades that fill it -- and Phase 1 measured adverse selection at full size
    within one second (BRIEFING §17.5).

    A live market maker would subscribe to both feeds and act on whichever showed
    the newer market. This module builds that book, and only that book.

THE RULE, AND WHY IT IS CAUSAL
    Every row -- a depth snapshot or a quote packet -- has two times:

        recv_ns   when we received it (the only time a strategy can act on it)
        state_ns  the market instant it describes: recv_ns for the quote feed,
                  recv_ns - DEPTH_LAG_NS for the depth feed

    Rows are ordered by ``recv_ns``. The book at a row is the content of whichever
    row *received so far* has the greatest ``state_ns``. So a depth snapshot that
    arrives after a fresher quote packet never overwrites it, and nothing is ever
    used before it was received. The one assumption is the lag constant, which is
    a property of the vendor, measured on the development days and fixed here.

    What this does NOT do: shift any timestamp. Fills, tape and quotes all stay
    on receive time. The merge only changes *which received information* the
    quoter looks at.

DEPTH OF THE MERGED BOOK
    Quote-feed rows carry five levels; depth rows carry twenty. Levels 6-20 of a
    quote-feed row are NaN (unknown), not zero. Everything that consumes this
    book reads the touch or the few levels at-or-better than a quote near it.
"""

from __future__ import annotations

import numpy as np

from ..data import store
from .reconstruct import MAX_LEVELS, BookSeries, _padded

#: Depth-feed delivery delay relative to the quote feed. Measured 200-250 ms on
#: every development day (2026-08-24/25/27/28), every hour, every instrument.
DEPTH_LAG_NS = 225_000_000


def merge_books(
    depth: BookSeries,
    q_recv_ns: np.ndarray,
    q_bid_px: np.ndarray,
    q_bid_qty: np.ndarray,
    q_ask_px: np.ndarray,
    q_ask_qty: np.ndarray,
    *,
    lag_ns: int = DEPTH_LAG_NS,
) -> BookSeries:
    """Merge a depth series with quote-feed ladders; see the module docstring."""
    width = depth.bid_px.shape[1]
    nq = len(q_recv_ns)
    qw = min(width, q_bid_px.shape[1]) if nq else 0

    def widen(a, fill):
        out = np.full((nq, width), fill, dtype=a.dtype if nq else float)
        if nq:
            out[:, :qw] = a[:, :qw]
        return out

    recv = np.r_[depth.recv_wall_ns, np.asarray(q_recv_ns, dtype=np.int64)]
    state = np.r_[depth.recv_wall_ns - lag_ns, np.asarray(q_recv_ns, dtype=np.int64)]
    bid_px = np.vstack([depth.bid_px, widen(q_bid_px, np.nan)])
    ask_px = np.vstack([depth.ask_px, widen(q_ask_px, np.nan)])
    bid_qty = np.vstack([depth.bid_qty, widen(np.asarray(q_bid_qty, dtype=np.int64), 0)])
    ask_qty = np.vstack([depth.ask_qty, widen(np.asarray(q_ask_qty, dtype=np.int64), 0)])
    flags = np.r_[depth.flags, np.zeros(nq, dtype=bool)]

    # Receive order; ties keep depth before quote (either is fine: same instant).
    order = np.lexsort((state, recv))
    recv, state = recv[order], state[order]
    bid_px, ask_px, bid_qty, ask_qty, flags = (x[order] for x in (bid_px, ask_px, bid_qty, ask_qty, flags))

    # The row supplying the book at each position: the freshest state received so far.
    best = np.maximum.accumulate(state)
    is_new = state >= best  # this row is (tied for) the freshest yet
    src = np.maximum.accumulate(np.where(is_new, np.arange(len(state)), -1))

    # One output row per distinct receive time, carrying the freshest book.
    last = np.r_[recv[1:] != recv[:-1], True]
    src = src[last]
    return BookSeries(
        security_id=depth.security_id,
        trading_date=depth.trading_date,
        recv_wall_ns=recv[last],
        bid_px=bid_px[src],
        bid_qty=bid_qty[src],
        ask_px=ask_px[src],
        ask_qty=ask_qty[src],
        flags=flags[src],
    )


def load_quote_ladders(root: str, trading_date, security_id: int, width: int = 5):
    """The quote feed's own 5-level book: ``(recv_ns, bid_px, bid_qty, ask_px, ask_qty)``."""
    t = store.read_capture(root, "quotes", dates=[trading_date], security_ids=[security_id])
    if t.num_rows == 0:
        z = np.zeros((0, width))
        return np.zeros(0, dtype=np.int64), z, z.astype(np.int64), z.copy(), z.astype(np.int64)
    w = min(width, MAX_LEVELS)
    bp = _padded(t.column("bid_price"), w, np.nan, "float64")
    ap = _padded(t.column("ask_price"), w, np.nan, "float64")
    bq = _padded(t.column("bid_quantity"), w, 0, "int64")
    aq = _padded(t.column("ask_quantity"), w, 0, "int64")
    # The 5-level block zero-pads empty levels rather than omitting them.
    bq[~(bp > 0)] = 0
    aq[~(ap > 0)] = 0
    bp[~(bp > 0)] = np.nan
    ap[~(ap > 0)] = np.nan
    return np.asarray(t.column("recv_wall_ns"), dtype=np.int64), bp, bq, ap, aq


def load_merged_book(root: str, trading_date, security_id: int, *, levels: int = MAX_LEVELS,
                     lag_ns: int = DEPTH_LAG_NS) -> BookSeries:
    from .reconstruct import load_book

    depth = load_book(root, trading_date, security_id, levels=levels)
    return merge_books(depth, *load_quote_ladders(root, trading_date, security_id), lag_ns=lag_ns)


