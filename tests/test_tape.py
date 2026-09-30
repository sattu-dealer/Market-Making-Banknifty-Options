"""The trade tape is derived, not captured; these pin the two inferential steps."""

from __future__ import annotations

import numpy as np

from bnfmm.book.tape import BUY, SELL, UNKNOWN, classify, volume_increments


def test_first_packet_is_a_baseline_not_a_trade():
    rows, units, rw = volume_increments(np.array([5000, 5000, 5030]))
    assert rows.tolist() == [2] and units.tolist() == [30] and rw == 0


def test_a_replayed_stale_packet_adds_no_volume():
    # 100 -> 130 -> (stale replay 110) -> 130 -> 160: only 60 units really traded.
    rows, units, rw = volume_increments(np.array([100, 130, 110, 130, 160]))
    assert units.sum() == 60
    assert rows.tolist() == [1, 4] and units.tolist() == [30, 30]
    assert rw == 1


def test_recovery_past_the_old_high_counts_only_the_excess():
    rows, units, _ = volume_increments(np.array([100, 130, 110, 150]))
    assert units.tolist() == [30, 20]


def test_total_equals_counter_range_whatever_the_replays():
    rng = np.random.default_rng(0)
    true = np.cumsum(rng.integers(0, 5, 500)) * 30
    noisy = true.copy()
    idx = rng.choice(np.arange(10, 500), 40, replace=False)
    noisy[idx] = true[idx - rng.integers(1, 10, 40)]  # stale replays
    _, units, _ = volume_increments(noisy)
    assert units.sum() == true[-1] - true[0]


def test_short_series():
    assert volume_increments(np.array([7]))[1].size == 0


def test_lee_ready_quote_rule():
    px = np.array([101.0, 99.0, 100.0])
    bid = np.array([99.0, 99.0, 99.0])
    ask = np.array([101.0, 101.0, 101.0])
    out = classify(px, bid, ask)
    assert out[0] == BUY and out[1] == SELL


def test_at_mid_falls_back_to_tick_test():
    px = np.array([99.0, 100.0, 101.0, 100.0])
    bid = np.full(4, 99.5)
    ask = np.full(4, 100.5)
    out = classify(px, bid, ask)
    assert out[1] == BUY  # uptick from 99
    assert out[3] == SELL  # downtick from 101


def test_no_prevailing_book_is_unknown_not_guessed():
    out = classify(np.array([100.0]), np.array([np.nan]), np.array([101.0]))
    assert out[0] == UNKNOWN
