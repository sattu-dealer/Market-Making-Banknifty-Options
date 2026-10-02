"""The portfolio market maker: queue priority, risk limits, no look-ahead, exact accounting."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from bnfmm.book.reconstruct import BookSeries
from bnfmm.book.tape import BUY, SELL, Tape
from bnfmm.sim.portfolio import Leg, PortfolioParams, run_portfolio, summarise

NS = 1_000_000_000
T0 = 1_787_900_000 * NS
DAY = date(2026, 8, 28)


def _book(n, bid=100.0, ask=102.0, qty=300, step_ns=NS // 5, gap_at=None):
    t = T0 + np.arange(n, dtype=np.int64) * step_ns
    if gap_at is not None:
        t[gap_at:] += 10 * NS
    bp = np.full((n, 2), [bid, bid - 0.05])
    ap = np.full((n, 2), [ask, ask + 0.05])
    q = np.full((n, 2), qty, dtype=np.int64)
    return BookSeries(1, DAY, t, bp, q, ap, q.copy(), np.zeros(n, bool))


def _tape(times, prices, qtys, aggr):
    n = len(times)
    return Tape(1, DAY, np.asarray(times, dtype=np.int64), np.asarray(prices, float),
                np.asarray(qtys, dtype=np.int64), np.asarray(aggr, dtype=np.int8),
                np.zeros(n, dtype=np.int64), 0, 0)


def _leg(book, tape, fv=101.0, delta=0.5, vega=10.0, call=True):
    n = len(book)
    return Leg(security_id=1, name="T", strike=100.0, is_call=call, tick=0.05, lot_size=30,
               freeze_qty=601, book=book, tape=tape, fair_value=np.full(n, fv),
               delta=np.full(n, delta), vega=np.full(n, vega))


P = PortfolioParams(min_premium=1.0, leg_skew_ticks=0.0, delta_skew=0.0, cost_split="symmetric")


def test_quotes_improve_the_touch_by_one_tick_and_never_cross():
    b = _book(5)
    res = run_portfolio([_leg(b, _tape([], [], [], []))], P)
    assert res.quoted_events > 0
    assert "would_cross" not in res.stand_down


def test_an_unchanged_quote_keeps_its_queue_place():
    # Bid is at 100.05 (improving 100.00): queue ahead 0. A sell print at 100.00
    # in interval 3 must fill the order placed at snapshot 1, not a fresh one.
    b = _book(6)
    tape = _tape([b.recv_wall_ns[3] - 1], [100.0], [30], [SELL])
    res = run_portfolio([_leg(b, tape)], P)
    assert len(res.fills) == 1 and res.fills[0].side == "bid"
    assert res.fills[0].price == pytest.approx(100.05)


def test_queue_ahead_is_honoured_when_joining():
    # join-only: bid rests at 100.00 behind 300 displayed units; 200 units of
    # selling cannot reach it, 150 more can.
    p = PortfolioParams(min_premium=1.0, improve=False, leg_skew_ticks=0.0, delta_skew=0.0,
                        cost_split="symmetric")
    b = _book(8)
    tape = _tape([b.recv_wall_ns[2] - 1, b.recv_wall_ns[5] - 1], [100.0, 100.0], [200, 150], [SELL, SELL])
    res = run_portfolio([_leg(b, tape)], p)
    assert [f.qty for f in res.fills] == [30]
    assert res.fills[0].t == b.recv_wall_ns[5]


def test_no_fill_across_a_feed_gap():
    b = _book(6, gap_at=3)
    tape = _tape([b.recv_wall_ns[3] - 1], [100.0], [500], [SELL])
    res = run_portfolio([_leg(b, tape)], P)
    assert res.fills == []
    assert res.stand_down.get("stale_book", 0) >= 1


def test_delta_limit_pulls_only_the_side_that_adds_risk():
    # Keep selling into our bid: position grows long delta until the limit bites.
    b = _book(40)
    times = b.recv_wall_ns[1:] - 1
    tape = _tape(times, np.full(len(times), 100.0), np.full(len(times), 30), np.full(len(times), SELL))
    p = PortfolioParams(min_premium=1.0, leg_skew_ticks=0.0, delta_skew=0.0, leg_cap_lots=100,
                        delta_limit_lots=2.0, cost_split="symmetric")
    res = run_portfolio([_leg(b, tape, delta=0.5)], p)
    # 0.5 delta per unit, limit 2 lots of underlying = 60 units of delta = 120 option units.
    assert res.positions[0].units == 120
    assert res.stand_down.get("delta_limit", 0) > 0


def test_leg_cap():
    b = _book(40)
    times = b.recv_wall_ns[1:] - 1
    tape = _tape(times, np.full(len(times), 100.0), np.full(len(times), 30), np.full(len(times), SELL))
    p = PortfolioParams(min_premium=1.0, leg_skew_ticks=0.0, delta_skew=0.0, leg_cap_lots=3,
                        cost_split="symmetric")
    res = run_portfolio([_leg(b, tape)], p)
    assert res.positions[0].units == 90


def test_sell_side_costs_several_times_the_buy_side():
    # Why cost_split exists: sell-side STT makes the per-side gates asymmetric.
    from bnfmm.sim.costs import CostModel
    from bnfmm.sim.portfolio import _side_cost_rates
    rb, rs = _side_cost_rates(CostModel.for_profile("member"))
    assert rs > 3 * rb > 0


def test_pnl_identity_is_exact():
    b = _book(30)
    t = b.recv_wall_ns
    tape = _tape([t[2] - 1, t[6] - 1, t[10] - 1, t[14] - 1], [100.0, 102.0, 100.0, 102.0],
                 [30, 30, 30, 30], [SELL, BUY, SELL, BUY])
    res = run_portfolio([_leg(b, tape)], P)
    s = summarise(res)
    assert s["fills"] == 4
    assert s["spread_capture"] + s["inventory_pnl"] == pytest.approx(s["gross_micro"], abs=1e-6)


def test_fills_are_settled_before_requoting():
    # A fill recorded at snapshot k carries the mark prevailing at k-1: the
    # decision that placed the order could not have seen the interval it filled in.
    b = _book(6)
    tape = _tape([b.recv_wall_ns[3] - 1], [100.0], [30], [SELL])
    res = run_portfolio([_leg(b, tape)], P)
    assert res.fills[0].t == b.recv_wall_ns[3]


def test_event_driven_feed_uses_socket_staleness_not_leg_silence():
    # A quiet leg (10 s between packets) on a live socket must still fill; the
    # same interval flagged stale by the socket must not.
    b = _book(6, gap_at=3)
    tape = _tape([b.recv_wall_ns[3] - 1], [100.0], [30], [SELL])
    live = _leg(b, tape)
    live.stale = np.zeros(len(b), dtype=bool)
    assert len(run_portfolio([live], P).fills) == 1
    dead = _leg(b, tape)
    dead.stale = np.zeros(len(b), dtype=bool)
    dead.stale[3] = True
    res = run_portfolio([dead], P)
    assert res.fills == [] and res.stand_down.get("stale_book", 0) >= 1
