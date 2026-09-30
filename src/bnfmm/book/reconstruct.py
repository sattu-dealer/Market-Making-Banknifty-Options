"""Turn the per-side depth rows on disk into two-sided book snapshots.

WHY THIS EXISTS
    The capture writes one Parquet row per *side*: Dhan's 20-level feed sends the
    bid ladder (code 41) and the ask ladder (code 51) as two separate frames, so
    ``store.DEPTH_TABLE`` has a ``side`` column and a book is never a single row.
    Everything downstream -- microprice, parity, the fill simulator -- needs the
    two ladders together, aligned in time. This module is the only place that
    pairing happens, so the pairing rule is stated once and tested once.

THE PAIRING RULE
    Both frames for an instrument arrive in the *same* ``recv()``, so they share
    ``recv_wall_ns`` exactly (the timestamp is stamped per read, not per frame).
    Grouping by ``recv_wall_ns`` and taking the last bid and last ask in each
    group therefore reconstructs exactly the snapshots the exchange pushed, with
    no interpolation and no tolerance window to tune. Measured on 2026-08-24
    security 59096: 91,872 bid rows + 91,872 ask rows collapse to 91,872 groups.

WHAT THE FEED IS, AND WHY IT MATTERS HERE
    Measured on the captured bytes: the 20-depth feed pushes a *complete snapshot
    of every subscribed instrument* every ~200 ms (median 201 ms, p05 157 ms,
    p95 442 ms), not an event-driven delta stream. A snapshot is a photograph of
    the book, so between two photographs an unknown number of adds, cancels and
    trades collapse into one net difference. This module deliberately does not
    pretend otherwise: it hands downstream code the photographs and the observed
    net change, and ``sim.fills`` is where the consequences are modelled and the
    assumptions are written down.

ONE-SIDED AND CROSSED BOOKS ARE REAL
    A padded (all-zero) ladder means the side was genuinely empty. The decoder
    strips padding, so an empty side arrives as a zero-length list and shows up
    here as ``NaN`` best price -- not as a price of zero, which would silently
    poison every mid and microprice that touched it. Crossed and locked books
    also occur (0.014% of ATM option snapshots on 2026-08-24, but 2.2% of front
    *future* snapshots on the same day). They are flagged, never repaired.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pyarrow as pa

from ..data import store

#: The 20-level feed's fixed ladder depth. Ladders shorter than this have had
#: exchange padding stripped by the decoder, so the length *is* the real depth.
MAX_LEVELS = 20


def _padded(col: pa.ChunkedArray, width: int, fill: float, dtype: str) -> np.ndarray:
    """A ragged list column as a dense ``(rows, width)`` array padded with ``fill``.

    The decoder strips exchange padding, so ladders are usually 20 long but are
    shorter whenever the book was genuinely thin -- 1,642 of 187,820 rows for the
    ATM call on expiry day. A dense array is what the vectorised consumers want,
    and ``fill`` (``NaN`` for prices, 0 for quantities) keeps "no level here"
    distinguishable from "a level priced at zero".
    """
    arr = col.combine_chunks()
    if isinstance(arr, pa.ChunkedArray):  # a zero-chunk column stays chunked
        arr = arr.chunk(0) if arr.num_chunks else pa.array([], type=pa.list_(pa.float64()))
    offsets = np.asarray(arr.offsets, dtype=np.int64)
    values = np.asarray(arr.values)
    lens = np.minimum(np.diff(offsets), width)
    out = np.full((len(lens), width), fill, dtype=dtype)
    if lens.sum():
        rows = np.repeat(np.arange(len(lens)), lens)
        starts = offsets[:-1]
        cols = np.arange(lens.sum()) - np.repeat(np.cumsum(lens) - lens, lens)
        take = np.repeat(starts, lens) + cols
        out[rows, cols] = values[take]
    return out


@dataclass(frozen=True, slots=True)
class BookSeries:
    """Every two-sided snapshot for one instrument on one day, column-major.

    Column-major because every consumer is vectorised: the fill simulator walks
    ``bid_px[:, 0]`` and the parity solver walks whole days at a time. Row-major
    dataclasses per snapshot would allocate ~92,000 objects per instrument per
    day for no benefit.

    Prices are ``NaN`` where no level existed; quantities are 0 there. ``bid_px``
    is descending and ``ask_px`` ascending, as the exchange sends them.
    """

    security_id: int
    trading_date: date
    recv_wall_ns: np.ndarray  # int64, one per snapshot
    bid_px: np.ndarray  # float64 (n, MAX_LEVELS), NaN-padded
    bid_qty: np.ndarray  # int64   (n, MAX_LEVELS), 0-padded
    ask_px: np.ndarray
    ask_qty: np.ndarray
    flags: np.ndarray  # bool (n,) True where either side was flagged suspect

    def __len__(self) -> int:
        return len(self.recv_wall_ns)

    @property
    def best_bid(self) -> np.ndarray:
        return self.bid_px[:, 0]

    @property
    def best_ask(self) -> np.ndarray:
        return self.ask_px[:, 0]

    @property
    def best_bid_qty(self) -> np.ndarray:
        return self.bid_qty[:, 0]

    @property
    def best_ask_qty(self) -> np.ndarray:
        return self.ask_qty[:, 0]

    @property
    def spread(self) -> np.ndarray:
        """Best ask minus best bid. ``NaN`` if either side was empty."""
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> np.ndarray:
        """The arithmetic mid. ``NaN`` if either side was empty.

        Kept only as a baseline to measure the microprice against; it is a poor
        fair value when the touch is lopsided, which on these books it usually is.
        """
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def two_sided(self) -> np.ndarray:
        """True where both sides had at least one level."""
        return np.isfinite(self.best_bid) & np.isfinite(self.best_ask)

    @property
    def crossed(self) -> np.ndarray:
        """True where best bid >= best ask -- locked or crossed, never repaired."""
        with np.errstate(invalid="ignore"):
            return self.two_sided & (self.best_bid >= self.best_ask)

    def usable(self) -> np.ndarray:
        """The snapshots a strategy may quote against: two-sided and not crossed."""
        return self.two_sided & ~self.crossed


def load_book(
    root: str,
    trading_date: date,
    security_id: int,
    *,
    table: str = "depth",
    levels: int = MAX_LEVELS,
) -> BookSeries:
    """Read one instrument's day of depth rows and pair them into snapshots.

    ``levels`` truncates the retained ladder depth. A full 20-level day for one
    instrument is ~59 MB of dense arrays, so a whole-expiry study (46 option books
    plus futures) needs 2.7 GB at full depth but only ~140 MB at ``levels=1``.
    Consumers that only read the touch -- the parity solver, most fair-value work
    -- should say so. The truncation happens after decoding, so the *reported*
    ladder depth in the source data is unaffected.
    """
    t = store.read_capture(root, table, dates=[trading_date], security_ids=[security_id])
    width = max(1, min(levels, MAX_LEVELS))
    if t.num_rows == 0:
        empty_f = np.zeros((0, width), dtype="float64")
        empty_i = np.zeros((0, width), dtype="int64")
        return BookSeries(
            security_id=security_id,
            trading_date=trading_date,
            recv_wall_ns=np.zeros(0, dtype="int64"),
            bid_px=empty_f,
            bid_qty=empty_i,
            ask_px=empty_f.copy(),
            ask_qty=empty_i.copy(),
            flags=np.zeros(0, dtype=bool),
        )

    # ``read_capture`` already sorts by (recv_wall_ns, recv_seq), so "the last row
    # of a group" is well defined and reproducible.
    wall = np.asarray(t.column("recv_wall_ns"), dtype=np.int64)
    side = np.asarray(t.column("side"))
    px = _padded(t.column("price"), MAX_LEVELS, np.nan, "float64")[:, :width]
    qty = _padded(t.column("quantity"), MAX_LEVELS, 0, "int64")[:, :width]
    suspect = (
        np.asarray(t.column("interleaved_padding")).astype(bool)
        | ~np.asarray(t.column("is_sorted")).astype(bool)
        | (np.asarray(t.column("anomalous_rows"), dtype=np.int64) > 0)
    )

    # Group boundaries: rows sharing recv_wall_ns came from one read() and so
    # belong to one exchange push.
    starts = np.flatnonzero(np.r_[True, wall[1:] != wall[:-1]])
    n = len(starts)
    ends = np.r_[starts[1:], len(wall)]

    bid_px = np.full((n, width), np.nan)
    ask_px = np.full((n, width), np.nan)
    bid_qty = np.zeros((n, width), dtype="int64")
    ask_qty = np.zeros((n, width), dtype="int64")
    flags = np.zeros(n, dtype=bool)

    is_bid = side == "bid"
    # Last bid row and last ask row within each group. Walking the (small) groups
    # is clearer than an argmax trick and this runs once per instrument-day.
    for g in range(n):
        lo, hi = starts[g], ends[g]
        seg_bid = np.flatnonzero(is_bid[lo:hi])
        seg_ask = np.flatnonzero(~is_bid[lo:hi])
        if len(seg_bid):
            i = lo + seg_bid[-1]
            bid_px[g] = px[i]
            bid_qty[g] = qty[i]
            flags[g] |= suspect[i]
        if len(seg_ask):
            j = lo + seg_ask[-1]
            ask_px[g] = px[j]
            ask_qty[g] = qty[j]
            flags[g] |= suspect[j]

    return BookSeries(
        security_id=security_id,
        trading_date=trading_date,
        recv_wall_ns=wall[starts],
        bid_px=bid_px,
        bid_qty=bid_qty,
        ask_px=ask_px,
        ask_qty=ask_qty,
        flags=flags,
    )


def load_books(
    root: str,
    trading_date: date,
    security_ids: Sequence[int],
) -> dict[int, BookSeries]:
    """``load_book`` for several instruments, keyed by ``security_id``."""
    return {sid: load_book(root, trading_date, sid) for sid in security_ids}


def align(books: dict[int, BookSeries]) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    """A common snapshot clock across instruments, plus per-instrument row indices.

    The feed pushes every subscribed instrument in one payload, so the instruments
    *mostly* share timestamps -- but not exactly: a payload can be split across
    two ``recv()`` calls, and an instrument can be missing from a push. So rather
    than assume a shared grid, this builds the union of timestamps and, for each
    instrument, the index of its most recent snapshot at or before each grid
    point (-1 before its first snapshot). That is a last-observation-carried-
    forward join, which is the only causally honest choice: at grid time ``T`` a
    strategy could not have seen a snapshot stamped after ``T``.
    """
    if not books:
        return np.zeros(0, dtype="int64"), {}
    grid = np.unique(np.concatenate([b.recv_wall_ns for b in books.values()]))
    idx = {}
    for sid, b in books.items():
        # searchsorted 'right' - 1 gives the last snapshot at or before each point.
        idx[sid] = np.searchsorted(b.recv_wall_ns, grid, side="right") - 1
    return grid, idx


def iter_snapshots(book: BookSeries) -> Iterator[tuple[int, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Yield ``(recv_wall_ns, bid_px, bid_qty, ask_px, ask_qty)`` per snapshot.

    Row-major escape hatch for tests and ad-hoc inspection; the simulator uses the
    column arrays directly.
    """
    for i in range(len(book)):
        yield (
            int(book.recv_wall_ns[i]),
            book.bid_px[i],
            book.bid_qty[i],
            book.ask_px[i],
            book.ask_qty[i],
        )
