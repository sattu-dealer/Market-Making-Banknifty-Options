"""The merged book must be causal and must never let stale depth overwrite fresh quotes."""

from __future__ import annotations

from datetime import date

import numpy as np

from bnfmm.book.merge import merge_books
from bnfmm.book.reconstruct import BookSeries

MS = 1_000_000


def _depth(times_ms, bids):
    n = len(times_ms)
    b = np.array(bids, dtype=float).reshape(n, 1)
    return BookSeries(1, date(2026, 8, 27), np.array(times_ms, dtype=np.int64) * MS,
                      b, np.full((n, 1), 30), b + 1.0, np.full((n, 1), 30), np.zeros(n, bool))


def _q(times_ms, bids):
    n = len(times_ms)
    b = np.array(bids, dtype=float).reshape(n, 1)
    return (np.array(times_ms, dtype=np.int64) * MS, b, np.full((n, 1), 60), b + 1.0,
            np.full((n, 1), 60))


def test_stale_depth_never_overwrites_a_fresher_quote():
    # Quote at 1000 shows 101. Depth received at 1100 describes the market at
    # 1100-225=875, i.e. older: the book must stay at 101.
    m = merge_books(_depth([900, 1100], [100.0, 100.0]), *_q([1000], [101.0]), lag_ns=225 * MS)
    assert m.recv_wall_ns.tolist() == [900 * MS, 1000 * MS, 1100 * MS]
    assert m.best_bid.tolist() == [100.0, 101.0, 101.0]


def test_depth_newer_than_the_last_quote_takes_over():
    m = merge_books(_depth([1300], [102.0]), *_q([1000], [101.0]), lag_ns=225 * MS)
    assert m.best_bid.tolist() == [101.0, 102.0]


def test_nothing_is_used_before_it_is_received():
    m = merge_books(_depth([500], [100.0]), *_q([2000], [105.0]), lag_ns=225 * MS)
    assert m.best_bid[m.recv_wall_ns < 2000 * MS].max() == 100.0


def test_quote_rows_are_padded_as_unknown_not_zero():
    d = _depth([100], [100.0])
    wide = BookSeries(1, d.trading_date, d.recv_wall_ns, np.array([[100.0, 99.95]]),
                      np.array([[30, 30]]), np.array([[101.0, 101.05]]), np.array([[30, 30]]),
                      np.zeros(1, bool))
    m = merge_books(wide, *_q([1000], [101.0]), lag_ns=0)
    assert np.isnan(m.bid_px[1, 1]) and m.bid_qty[1, 1] == 0


def test_same_receive_time_collapses_to_one_row():
    m = merge_books(_depth([1000], [100.0]), *_q([1000], [101.0]), lag_ns=225 * MS)
    assert len(m) == 1 and m.best_bid[0] == 101.0
