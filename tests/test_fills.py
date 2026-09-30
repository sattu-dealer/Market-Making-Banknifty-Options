"""Tests for the snapshot-to-snapshot fill simulator.

``BRIEFING.md`` §4.4 singles out the fill simulator as the component that must be
tested, and rightly: it is the one place where an optimistic assumption silently
converts into profit. Every test here pins a *specific* modelling decision that
the module's docstring claims, so that changing the behaviour breaks a test with
a name that says what was changed.
"""

from __future__ import annotations

import numpy as np
import pytest

from bnfmm.book.tape import BUY, SELL, UNKNOWN
from bnfmm.sim.fills.depletion import (
    DEFAULT_MAX_STALE_NS,
    DepletionSimulator,
    eligible_volume,
    queue_ahead,
)

# A simple two-sided book: bids 100.0/99.5/99.0, asks 101.0/101.5/102.0.
BID_PX = np.array([100.0, 99.5, 99.0])
BID_QTY = np.array([50, 60, 70])
ASK_PX = np.array([101.0, 101.5, 102.0])
ASK_QTY = np.array([40, 30, 20])

BOOK = dict(bid_px=BID_PX, bid_qty=BID_QTY, ask_px=ASK_PX, ask_qty=ASK_QTY)
NS = 1_000_000_000


def _sim(**kw) -> DepletionSimulator:
    return DepletionSimulator(**kw)


def _place(sim, side="bid", price=100.0, quantity=30, now_ns=NS):
    return sim.place(
        security_id=59096, side=side, price=price, quantity=quantity, now_ns=now_ns, **BOOK
    )


def _step(sim, price, qty, aggressor, *, now_ns=NS + 200_000_000, prev_ns=NS,
          best_bid=100.0, best_ask=101.0):
    return sim.step(
        now_ns=now_ns,
        prev_ns=prev_ns,
        tape_price=np.array([price], dtype=float),
        tape_qty=np.array([qty], dtype=np.int64),
        tape_aggressor=np.array([aggressor], dtype=np.int8),
        best_bid=best_bid,
        best_ask=best_ask,
    )


# -- queue accounting ---------------------------------------------------------


def test_queue_ahead_counts_better_prices_and_own_level() -> None:
    """Our order joins the back of its own level, so that level counts as ahead."""
    assert queue_ahead(100.0, "bid", **BOOK) == 50
    assert queue_ahead(99.5, "bid", **BOOK) == 110  # 50 + 60
    assert queue_ahead(99.0, "bid", **BOOK) == 180  # 50 + 60 + 70
    assert queue_ahead(101.0, "ask", **BOOK) == 40
    assert queue_ahead(101.5, "ask", **BOOK) == 70


def test_queue_ahead_ignores_levels_worse_than_our_price() -> None:
    """A bid below ours does not execute before ours, so it must not count."""
    # A bid at 100.5 is better than every displayed bid: nothing is ahead.
    assert queue_ahead(100.5, "bid", **BOOK) == 0
    # An ask at 100.5 undercuts every displayed ask (101.0+), so likewise nothing
    # is ahead of it -- "better" for an ask means *lower*.
    assert queue_ahead(100.5, "ask", **BOOK) == 0
    # An ask above all of them is behind all of them.
    assert queue_ahead(102.5, "ask", **BOOK) == 90


def test_queue_ahead_skips_padded_levels() -> None:
    """NaN prices mark levels the exchange never sent; they hold no orders."""
    bp = np.array([100.0, np.nan, np.nan])
    bq = np.array([50, 0, 0])
    assert queue_ahead(100.0, "bid", bp, bq, ASK_PX, ASK_QTY) == 50


def test_optimistic_queue_rule_discounts_our_own_level() -> None:
    """``queue_at_price_ahead=False`` puts us at the front of our level.

    This is the sensitivity knob: it is *not* the default, because our order
    arrives after the snapshot and cannot really be ahead of what the snapshot
    already displayed. It exists to measure how much the headline result depends
    on that choice.
    """
    pess = _sim(queue_at_price_ahead=True)
    opt = _sim(queue_at_price_ahead=False)
    assert _place(pess, price=99.5).queue_ahead == 110
    assert _place(opt, price=99.5).queue_ahead == 50  # 110 - 60 at our own level


# -- eligibility --------------------------------------------------------------


def test_only_opposite_aggressor_fills_us() -> None:
    """A resting bid is filled by a seller, never by a buyer."""
    px = np.array([100.0, 100.0])
    qty = np.array([10, 10])
    agg = np.array([SELL, BUY], dtype=np.int8)
    assert eligible_volume(100.0, "bid", px, qty, agg).tolist() == [10, 0]
    assert eligible_volume(100.0, "ask", px, qty, agg).tolist() == [0, 10]


def test_unclassified_volume_is_excluded() -> None:
    """Volume we cannot attribute is dropped, which understates fills by design."""
    px = np.array([100.0])
    qty = np.array([99])
    agg = np.array([UNKNOWN], dtype=np.int8)
    assert eligible_volume(100.0, "bid", px, qty, agg).sum() == 0


def test_prints_away_from_our_price_do_not_fill_us() -> None:
    """A seller printing above our bid did not reach us."""
    px = np.array([100.5])
    qty = np.array([100])
    agg = np.array([SELL], dtype=np.int8)
    assert eligible_volume(100.0, "bid", px, qty, agg).sum() == 0
    # ...but a print at or below our bid does.
    assert eligible_volume(100.5, "bid", px, qty, agg).sum() == 100


# -- depletion ----------------------------------------------------------------


def test_no_fill_until_queue_ahead_is_exhausted() -> None:
    """The core rule: volume must first consume everyone in front of us."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)  # 50 units ahead
    assert _step(sim, 100.0, 40, SELL) == []  # 40 < 50, still queued
    fills = _step(sim, 100.0, 20, SELL, now_ns=NS + 400_000_000, prev_ns=NS + 200_000_000)
    # cumulative 60 vs 50 ahead -> 10 units through to us
    assert len(fills) == 1
    assert fills[0].quantity == 10
    assert fills[0].price == 100.0
    assert not fills[0].swept


def test_fill_is_capped_at_order_size() -> None:
    """Enormous volume cannot fill more than we actually quoted."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    fills = _step(sim, 100.0, 100_000, SELL)
    assert sum(f.quantity for f in fills) == 30
    assert sim.resting == []  # fully filled, no longer live


def test_partial_fills_accumulate_without_double_counting() -> None:
    """Two partial fills must sum to the order size, not exceed it."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    f1 = _step(sim, 100.0, 60, SELL)  # 60 - 50 ahead = 10
    f2 = _step(sim, 100.0, 15, SELL, now_ns=NS + 400_000_000, prev_ns=NS + 200_000_000)
    assert [f.quantity for f in f1] == [10]
    assert [f.quantity for f in f2] == [15]
    assert sim.stats.filled_units == 25
    assert sim.resting[0].remaining == 5


def test_cancels_ahead_do_not_advance_the_queue() -> None:
    """Depletion counts traded volume only.

    The queue is fixed at placement and depleted by prints. A snapshot showing the
    level shrink without any print is a cancel, and a cancel executes nothing, so
    it must not move us toward a fill. (Real cancels *do* improve queue position;
    refusing to model that is a stated understatement.)
    """
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    # A step with no tape at all, and a book whose bid vanished.
    fills = sim.step(
        now_ns=NS + 200_000_000,
        prev_ns=NS,
        tape_price=np.zeros(0),
        tape_qty=np.zeros(0, dtype=np.int64),
        tape_aggressor=np.zeros(0, dtype=np.int8),
        best_bid=99.5,  # our level is gone from the touch
        best_ask=101.0,
    )
    assert fills == []
    assert sim.stats.filled_units == 0


# -- sweeps -------------------------------------------------------------------


def test_sweep_fills_remainder_when_level_trades_through() -> None:
    """If the book's best bid ends below our price, our level was taken out.

    The queue ahead (50) must first be cleared by observed volume, so the 200-unit
    print both clears it and triggers the sweep.
    """
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    fills = _step(sim, 99.5, 200, SELL, best_bid=99.0)
    assert len(fills) == 1
    assert fills[0].swept
    assert fills[0].quantity == 30
    assert fills[0].price == 100.0  # filled at our own limit, never better


def test_sweep_never_applies_to_a_quote_inside_the_spread() -> None:
    """The regression that mattered most: book-improving quotes must not be swept.

    A bid at 100.5 is inside the spread, so the displayed touch (100.0) is *always*
    below it and the naive trade-through test is permanently true. Left unguarded
    that filled 84% of all orders in full off a single eligible print and drove the
    measured effective half-spread negative. With ``queue_ahead == 0`` the sweep is
    refused and ordinary depletion governs: one unit of volume, one unit of fill.
    """
    sim = _sim()
    order = _place(sim, price=100.5, quantity=30)
    assert order.queue_ahead == 0  # alone at a level the book never displayed
    fills = _step(sim, 100.0, 1, SELL, best_bid=100.0)
    assert [f.quantity for f in fills] == [1]
    assert not fills[0].swept
    assert sim.stats.swept_fills == 0


def test_sweep_requires_the_queue_ahead_to_have_cleared() -> None:
    """A sweep may only accelerate the tail of an already-proven fill.

    40 units of volume cannot clear the 50 units ahead of us, so however far the
    touch has moved, we have not been reached.
    """
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    fills = _step(sim, 99.5, 40, SELL, best_bid=99.0)
    assert fills == []
    assert sim.stats.swept_fills == 0


def test_sweep_without_volume_is_a_cancel_not_a_fill() -> None:
    """A level that vanishes with no prints was pulled, not executed."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    fills = sim.step(
        now_ns=NS + 200_000_000,
        prev_ns=NS,
        tape_price=np.zeros(0),
        tape_qty=np.zeros(0, dtype=np.int64),
        tape_aggressor=np.zeros(0, dtype=np.int8),
        best_bid=99.0,  # swept away...
        best_ask=101.0,
    )
    assert fills == []  # ...but nothing traded, so we were not filled
    assert sim.stats.swept_fills == 0


def test_sweep_can_be_disabled() -> None:
    sim = _sim(allow_sweep_fill=False)
    _place(sim, price=100.0, quantity=30)
    fills = _step(sim, 99.5, 200, SELL, best_bid=99.0)
    # Without the sweep rule only ordinary depletion applies. The print at 99.5
    # is below our 100.0 bid so it is eligible: 200 - 50 ahead caps at 30.
    assert sum(f.quantity for f in fills) == 30
    assert all(not f.swept for f in fills)


# -- staleness ----------------------------------------------------------------


def test_stale_interval_refuses_to_fill_and_counts_what_it_refused() -> None:
    """A fill inferred across a data gap is fiction; the refusal must be visible."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    fills = _step(
        sim, 100.0, 10_000, SELL, now_ns=NS + 90 * 1_000_000_000, prev_ns=NS
    )
    assert fills == []
    assert sim.stats.intervals_stale == 1
    assert sim.stats.stale_units_skipped == 10_000
    assert sim.stats.filled_units == 0


def test_normal_cadence_is_not_stale() -> None:
    """The measured p95 inter-snapshot gap (~442 ms) must not trip the guard."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    _step(sim, 100.0, 100, SELL, now_ns=NS + 442_000_000, prev_ns=NS)
    assert sim.stats.intervals_stale == 0
    assert sim.stats.filled_units == 30
    assert DEFAULT_MAX_STALE_NS > 442_000_000


# -- bookkeeping --------------------------------------------------------------


def test_ask_side_mirrors_bid_side() -> None:
    """The two sides must be symmetric; an asymmetry here would bias PnL."""
    sim = _sim()
    _place(sim, side="ask", price=101.0, quantity=30)  # 40 units ahead
    assert _step(sim, 101.0, 30, BUY) == []
    fills = _step(sim, 101.0, 30, BUY, now_ns=NS + 400_000_000, prev_ns=NS + 200_000_000)
    assert [f.quantity for f in fills] == [20]  # 60 cum - 40 ahead
    assert fills[0].side == "ask"


def test_cancel_all_removes_resting_orders() -> None:
    """A market maker re-quotes each cycle; stale orders must not linger."""
    sim = _sim()
    _place(sim)
    assert len(sim.resting) == 1
    sim.cancel_all()
    assert sim.resting == []
    assert _step(sim, 100.0, 10_000, SELL) == []


def test_stats_report_fill_ratio_against_eligible_volume() -> None:
    sim = _sim()
    _place(sim, price=100.0, quantity=30)
    _step(sim, 100.0, 100, SELL)
    d = sim.stats.as_dict()
    assert d["eligible_units"] == 100
    assert d["filled_units"] == 30
    assert d["fill_ratio_pct"] == pytest.approx(30.0)


def test_no_lookahead_state_is_carried_between_orders() -> None:
    """Two orders at different prices must deplete independently."""
    sim = _sim()
    _place(sim, price=100.0, quantity=30)  # 50 ahead
    _place(sim, price=99.5, quantity=30)  # 110 ahead
    fills = _step(sim, 99.5, 120, SELL)
    by_price = {f.price: f.quantity for f in fills}
    assert by_price[100.0] == 30  # 120 - 50 = 70, capped at 30
    assert by_price[99.5] == 10  # 120 - 110
