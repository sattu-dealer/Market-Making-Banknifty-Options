"""Data-quality checks: each one must fire on the defect it exists to catch."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from bnfmm.data import qa

NS = qa.NS
DAY = date(2026, 8, 27)
LO, HI = qa.session_bounds_ns(DAY)


def test_session_bounds_are_ist():
    assert HI - LO == int((6 * 3600 + 15 * 60) * NS)
    assert qa.fmt_ist(LO) == "09:15:00"


def test_full_coverage_with_small_jitter():
    ts = np.arange(LO, HI + 1, 200_000_000)
    c = qa.coverage(ts, (LO, HI))
    assert c["coverage"] == pytest.approx(1.0)
    assert c["gaps_large"] == 0 and c["segments"] == 1


def test_a_long_gap_ends_a_segment_and_costs_coverage():
    ts = np.r_[np.arange(LO, LO + 3600 * NS, NS), np.arange(LO + 3700 * NS, HI + 1, NS)]
    c = qa.coverage(ts, (LO, HI), max_gap_s=30)
    assert c["gaps_large"] == 1 and c["segments"] == 2
    assert c["largest_gap_s"] == pytest.approx(101)
    assert c["coverage"] == pytest.approx(1 - 101 / 22500)


def test_a_late_start_is_a_gap():
    ts = np.arange(LO + 600 * NS, HI + 1, NS)
    c = qa.coverage(ts, (LO, HI))
    assert c["large_gaps"][0] == (LO, LO + 600 * NS)
    assert c["coverage"] == pytest.approx(1 - 600 / 22500)


def test_no_data_is_zero_coverage():
    c = qa.coverage(np.zeros(0, dtype=np.int64), (LO, HI))
    assert c["coverage"] == 0 and c["segments"] == 0


def test_observations_outside_the_window_are_ignored():
    ts = np.r_[LO - 60 * NS, np.arange(LO, HI + 1, NS), HI + 60 * NS]
    assert qa.coverage(ts, (LO, HI))["n"] == 22501


def _book(rows):
    """rows of (bid, ask) touch prices; one level each side, qty 30."""
    b = np.array([[r[0]] for r in rows], dtype=float)
    a = np.array([[r[1]] for r in rows], dtype=float)
    q = np.where(np.isfinite(b), 30, 0)
    qa_ = np.where(np.isfinite(a), 30, 0)
    return b, q, a, qa_


def test_book_checks_flag_each_defect():
    b, bq, a, aq = _book([(100.0, 100.5), (100.5, 100.0), (100.0, 100.0),
                          (np.nan, 100.0), (100.02, 100.5), (-1.0, 100.0)])
    r = qa.book_checks(b, bq, a, aq, tick=0.05)
    assert r["snapshots"] == 6
    assert r["crossed_pct"] == pytest.approx(100 / 6)
    assert r["locked_pct"] == pytest.approx(100 / 6)
    assert r["one_sided_pct"] == pytest.approx(100 / 6)
    assert r["off_tick"] == 1
    assert r["nonpositive_px"] == 1
    assert r["spread_ticks_p50"] == pytest.approx(10)


def test_non_monotone_ladder_detected():
    b = np.array([[100.0, 100.5]])  # a deeper bid priced above the touch
    a = np.array([[101.0, 101.5]])
    q = np.full((1, 2), 30)
    assert qa.book_checks(b, q, a, q, tick=0.05)["nonmonotone_pct"] == 100


def test_zero_quantity_at_a_priced_level():
    b = np.array([[100.0]])
    a = np.array([[101.0]])
    r = qa.book_checks(b, np.array([[0]]), a, np.array([[30]]), tick=0.05)
    assert r["zero_qty_level"] == 1


def test_tape_checks_rewinds_and_ist_epoch():
    w = LO + np.arange(6) * NS
    m = 1_000 + np.arange(6) * NS
    vol = np.array([100, 130, 160, 150, 190, 190])
    lte = (w // NS) + qa.IST_OFFSET_S - 1  # IST wall seconds, 1 s behind
    r = qa.tape_checks(w, m, vol, np.full(6, 200.05), lte, tick=0.05)
    assert r["rewinds"] == 1 and r["rewind_units"] == 10
    assert r["trade_packets"] == 3
    assert r["exch_epoch_is_ist"] is True
    assert r["exch_lag_s_p50"] == pytest.approx(1.0, abs=0.01)
    assert r["clock_skew_events_gt1s"] == 0


def test_tape_checks_see_a_suspend():
    w = LO + np.array([0, 1, 100, 101]) * NS  # wall jumps 99 s
    m = np.array([0, 1, 2, 3]) * NS  # monotonic did not advance: suspended
    r = qa.tape_checks(w, m, np.array([1, 2, 3, 4]), np.full(4, 1.0),
                       np.zeros(4, dtype=np.int64), tick=0.05)
    assert r["clock_skew_events_gt1s"] == 1


def test_touch_agreement_matches_and_ages_out():
    dn = np.array([0, 10, 20]) * NS
    db = np.array([100.0, 100.5, 101.0])
    da = db + 0.5
    qn = np.array([0.5, 10.2, 15.0, 20.1]) * NS  # 15.0 is 5 s after a depth row: too old
    qb = np.array([100.0, 100.5, 100.5, 101.25])
    r = qa.touch_agreement(dn, db, da, qn.astype(np.int64), qb, qb + 0.5, tick=0.05)
    assert r["compared"] == 3
    assert r["exact_pct"] == pytest.approx(200 / 3)


def test_presence_reports_the_index_gap():
    p = qa.presence([25, 13, 1, 2], [1, 2])
    assert p["missing"] == [13, 25] and p["present"] == 2


@pytest.mark.parametrize("d,f,ok", [(0.9, 0.9, True), (0.1, 0.9, False), (0.9, 0.0, False)])
def test_inclusion_rule(d, f, ok):
    assert qa.include_day(d, f, min_depth=0.25, min_feed=0.25)[0] is ok
