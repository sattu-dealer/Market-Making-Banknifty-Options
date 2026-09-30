"""Black-76: textbook values, parity, round trips, and graceful failure."""

from __future__ import annotations

import numpy as np
import pytest

from bnfmm.fairvalue import black76 as b

F, K, T, V, D = 100.0, 100.0, 0.5, 0.2, np.exp(-0.05 * 0.5)


def test_atm_textbook_value():
    # ATM Black-76: D * F * (2 N(s/2) - 1), s = vol * sqrt(T)
    from scipy.stats import norm
    s = V * np.sqrt(T)
    expected = D * F * (2 * norm.cdf(s / 2) - 1)
    assert b.price(F, K, T, V, D, True) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("k", [80.0, 100.0, 125.0])
def test_put_call_parity(k):
    c = b.price(F, k, T, V, D, True)
    p = b.price(F, k, T, V, D, False)
    assert c - p == pytest.approx(D * (F - k), abs=1e-10)


@pytest.mark.parametrize("k", [80.0, 100.0, 125.0])
def test_delta_parity_and_finite_difference(k):
    dc = b.delta(F, k, T, V, D, True)
    dp = b.delta(F, k, T, V, D, False)
    assert dc - dp == pytest.approx(D, abs=1e-12)
    h = 1e-4
    fd = (b.price(F + h, k, T, V, D, True) - b.price(F - h, k, T, V, D, True)) / (2 * h)
    assert dc == pytest.approx(fd, rel=1e-6)


def test_implied_vol_round_trip_vectorised():
    ks = np.array([70.0, 90, 100, 110, 140])
    vols = np.array([0.35, 0.22, 0.2, 0.18, 0.3])
    calls = np.array([True, False, True, False, True])
    px = b.price(F, ks, T, vols, D, calls)
    assert b.implied_vol(px, F, ks, T, D, calls) == pytest.approx(vols, abs=1e-8)


def test_value_at_or_below_intrinsic_has_no_vol():
    intrinsic = D * (F - 80.0)
    out = b.implied_vol(np.array([intrinsic, intrinsic - 1, np.nan]), F, 80.0, T, D, True)
    assert np.isnan(out).all()


def test_expiry_day_is_finite():
    d = b.delta(F, 100.5, 0.0, 0.3, 1.0, True)
    assert np.isfinite(d) and 0 < d < 1


def test_vega_matches_finite_difference_and_is_the_same_for_puts():
    h = 1e-5
    fd = (b.price(F, 110.0, T, V + h, D, True) - b.price(F, 110.0, T, V - h, D, True)) / (2 * h)
    assert b.vega(F, 110.0, T, V, D) == pytest.approx(fd, rel=1e-6)
    fdp = (b.price(F, 110.0, T, V + h, D, False) - b.price(F, 110.0, T, V - h, D, False)) / (2 * h)
    assert fdp == pytest.approx(fd, rel=1e-6)
