"""Tests for the spread-capture / inventory split of gross PnL.

This decomposition is the number the report's central claim rests on: that the
one profitable configuration found across two days earned its profit from
directional inventory rather than from quoting. A bug that shifted PnL from the
inventory term to the spread term would invert that conclusion, so the identity
is pinned here rather than trusted.

The identity being tested is exact, not approximate:

    gross = sum_i s_i * (M_T - P_i)
          = sum_i s_i * (M_i - P_i)   +   sum_i s_i * (M_T - M_i)

so every test asserts the residual is zero to floating-point tolerance, and the
constructed cases isolate one term at a time.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from bnfmm.sim.backtest import Position, RunResult, decompose_pnl
from bnfmm.sim.fills.depletion import Fill


def _fill(side: str, price: float, quantity: int = 30, ns: int = 0) -> Fill:
    return Fill(
        security_id=59088,
        side=side,
        price=float(price),
        quantity=quantity,
        recv_wall_ns=ns,
        swept=False,
        queue_ahead=0,
        cum_volume=quantity,
    )


def _result(fills, marks, *, final_mark: float) -> RunResult:
    """A RunResult with PnL computed the same way ``run_day`` computes it."""
    pos = Position()
    for f in fills:
        pos.apply(f.side, f.price, f.quantity)
    r = RunResult(
        security_id=59088,
        trading_date=date(2026, 8, 24),
        snapshots=len(fills) + 1,
        quoted_snapshots=len(fills),
        fills=list(fills),
        fill_marks=list(marks),
        realised=pos.realised,
        final_units=pos.units,
        final_mark=final_mark,
    )
    r.unrealised = pos.unrealised(final_mark)
    return r


def test_round_trip_at_a_flat_mark_is_all_spread_capture() -> None:
    """Buy the bid, sell the ask, mark unchanged: pure liquidity provision."""
    fills = [_fill("bid", 99.0), _fill("ask", 101.0)]
    r = _result(fills, [100.0, 100.0], final_mark=100.0)

    d = decompose_pnl(r)

    assert d["spread_capture"] == pytest.approx(60.0)  # 30 * (1.0 + 1.0)
    assert d["inventory_pnl"] == pytest.approx(0.0)
    assert d["residual"] == pytest.approx(0.0, abs=1e-9)


def test_buying_at_the_mark_and_holding_is_all_inventory_pnl() -> None:
    """Filled exactly at fair value, so any profit is the position moving."""
    fills = [_fill("bid", 100.0)]
    r = _result(fills, [100.0], final_mark=105.0)

    d = decompose_pnl(r)

    assert d["spread_capture"] == pytest.approx(0.0)
    assert d["inventory_pnl"] == pytest.approx(150.0)  # 30 * 5.0
    assert d["inventory_share_pct"] == pytest.approx(100.0)
    assert d["residual"] == pytest.approx(0.0, abs=1e-9)


def test_the_two_terms_sum_to_gross_pnl_with_mixed_flow() -> None:
    """The identity holds when both terms are non-zero and signs are mixed."""
    fills = [
        _fill("bid", 99.5, 30),
        _fill("ask", 101.5, 60),
        _fill("bid", 100.5, 30),
    ]
    marks = [100.0, 101.0, 100.25]
    r = _result(fills, marks, final_mark=102.0)

    d = decompose_pnl(r)

    assert d["spread_capture"] + d["inventory_pnl"] == pytest.approx(r.gross_pnl)
    assert d["residual"] == pytest.approx(0.0, abs=1e-9)


def test_a_short_position_into_a_rising_mark_loses_on_inventory() -> None:
    """Sign convention: short inventory and a rising mark is a loss, not a gain."""
    fills = [_fill("ask", 100.0)]
    r = _result(fills, [100.0], final_mark=110.0)

    d = decompose_pnl(r)

    assert d["spread_capture"] == pytest.approx(0.0)
    assert d["inventory_pnl"] == pytest.approx(-300.0)  # -30 * 10.0
    assert d["residual"] == pytest.approx(0.0, abs=1e-9)


def test_profitable_quoting_can_coexist_with_a_losing_day() -> None:
    """The case that motivated this module.

    Both fills captured spread, but the position carried between them lost more
    than the spread earned. Reporting only gross PnL would call this a failed
    strategy; reporting only spread capture would call it a success. The split is
    what distinguishes them, and it is the exact shape of the 2026-08-24 result.
    """
    fills = [_fill("bid", 99.0), _fill("bid", 98.0)]
    r = _result(fills, [100.0, 99.0], final_mark=95.0)

    d = decompose_pnl(r)

    assert d["spread_capture"] > 0  # we bought below the prevailing mid twice
    assert d["inventory_pnl"] < 0  # and the mid then fell hard
    assert r.gross_pnl < 0
    assert d["residual"] == pytest.approx(0.0, abs=1e-9)


def test_no_fills_reports_nothing_rather_than_zero() -> None:
    """A day with no fills is unmeasurable, which is not the same as break-even."""
    r = _result([], [], final_mark=100.0)

    d = decompose_pnl(r)

    assert d == {"fills": 0}
    assert "spread_capture" not in d


def test_a_non_finite_final_mark_is_reported_unmeasurable() -> None:
    """A dead book at the close must not silently book the position at NaN."""
    fills = [_fill("bid", 99.0)]
    r = _result(fills, [100.0], final_mark=float("nan"))

    d = decompose_pnl(r)

    assert d["measurable"] == 0
    assert "spread_capture" not in d


def test_decomposition_matches_a_brute_force_sum() -> None:
    """Cross-check the vectorised implementation against the definition."""
    rng = np.random.default_rng(20260824)
    sides = rng.choice(["bid", "ask"], size=50)
    prices = 100.0 + rng.normal(0, 1.0, size=50)
    marks = 100.0 + rng.normal(0, 1.0, size=50)
    fills = [_fill(s, float(p)) for s, p in zip(sides, prices)]
    final_mark = 101.25
    r = _result(fills, [float(m) for m in marks], final_mark=final_mark)

    d = decompose_pnl(r)

    capture = sum(
        (30 if s == "bid" else -30) * (m - p)
        for s, p, m in zip(sides, prices, marks)
    )
    inventory = sum(
        (30 if s == "bid" else -30) * (final_mark - m)
        for s, m in zip(sides, marks)
    )
    assert d["spread_capture"] == pytest.approx(capture)
    assert d["inventory_pnl"] == pytest.approx(inventory)
    assert d["residual"] == pytest.approx(0.0, abs=1e-6)


def test_a_dead_closing_book_marks_at_the_last_finite_price() -> None:
    """An open position at a NaN close must not be booked at zero.

    ``Position.unrealised`` returns 0.0 for a non-finite mark, so a book whose
    final snapshots are one-sided -- routine on expiry day, as liquidity leaves
    into the 15:30 close -- would silently value the whole position at zero. On
    2026-08-25 the 57500 PE ended long 150 units with exactly this NaN mark while
    the forward sat below the strike, so the position had real intrinsic value and
    the reported loss was understated. ``run_day`` falls back to the last finite
    microprice and records how stale it was.
    """
    from bnfmm.book.reconstruct import BookSeries
    from bnfmm.book.tape import SELL, Tape
    from bnfmm.sim.backtest import run_day
    from bnfmm.strategy.quoter import QuoteParams

    n = 60
    step = 200_000_000  # the feed's ~200 ms snapshot cadence
    ns = np.arange(n, dtype=np.int64) * step
    bid_px = np.full((n, 1), 99.0)
    ask_px = np.full((n, 1), 101.0)
    # A near-empty touch, so our quote is at the front of the queue and the
    # depletion model can actually fill it within the run.
    bid_qty = np.full((n, 1), 1, dtype=np.int64)
    ask_qty = np.full((n, 1), 1, dtype=np.int64)

    # The ask side dies for the last three snapshots: a one-sided closing book.
    ask_px[-3:, 0] = np.nan
    ask_qty[-3:, 0] = 0

    book = BookSeries(
        security_id=59091,
        trading_date=date(2026, 8, 25),
        recv_wall_ns=ns,
        bid_px=bid_px,
        bid_qty=bid_qty,
        ask_px=ask_px,
        ask_qty=ask_qty,
        flags=np.zeros(n, dtype=bool),
    )

    # Sellers hitting our bid all day, so we finish long and must carry a position
    # into the dead close. The prints are at 99.0, at or below our bid, and their
    # aggressor is SELL -- which is what fills a resting buy order.
    tape = Tape(
        security_id=59091,
        trading_date=date(2026, 8, 25),
        recv_wall_ns=ns[1:],
        price=np.full(n - 1, 99.0),
        quantity=np.full(n - 1, 30, dtype=np.int64),
        aggressor=np.full(n - 1, SELL, dtype=np.int8),
        last_trade_epoch=ns[1:] // 1_000_000_000,
        rewinds=0,
        baseline_volume=0,
    )

    r = run_day(
        book=book,
        tape=tape,
        # A 20-tick half-spread puts our bid at 99.00, on the touch, so the 99.00
        # prints are eligible against it. At 2 ticks the quote would rest at 99.90
        # -- inside the spread and above every print -- and nothing would fill.
        params=QuoteParams(half_spread_ticks=20.0, max_position_lots=50),
        tick=0.05,
        lot_size=30,
        freeze_qty=601,
    )

    assert r.final_units != 0, "test needs an open position at the close"
    assert np.isfinite(r.final_mark), "a dead close must fall back, not stay NaN"
    # Three dead snapshots back, at the 200 ms cadence.
    assert r.final_mark_stale_ns == 3 * step
    assert r.unrealised != 0.0
