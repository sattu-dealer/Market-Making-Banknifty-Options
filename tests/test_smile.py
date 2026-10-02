"""Smile fit: exact on a true polynomial, honest leave-one-out on an outlier."""

from __future__ import annotations

import numpy as np
import pytest

from bnfmm.fairvalue.smile import fit_smile_loo


def test_recovers_an_exact_quadratic_in_and_out_of_sample():
    x = np.linspace(-0.05, 0.05, 11)[None, :]
    iv = 0.15 + 0.3 * x + 2.0 * x**2
    fit, loo = fit_smile_loo(x, iv, np.ones_like(x))
    assert fit == pytest.approx(iv, abs=1e-12)
    assert loo == pytest.approx(iv, abs=1e-10)


def test_leave_one_out_exposes_an_outlier_the_in_sample_fit_hides():
    x = np.linspace(-0.05, 0.05, 11)[None, :]
    iv = 0.15 + 2.0 * x**2
    iv[0, 5] += 0.02  # one strike two vol points rich
    fit, loo = fit_smile_loo(x, iv, np.ones_like(x))
    in_res = iv[0, 5] - fit[0, 5]
    loo_res = iv[0, 5] - loo[0, 5]
    assert loo_res == pytest.approx(0.02, abs=1e-10)  # exactly the injected error
    assert 0 < in_res < loo_res  # in-sample shrinks it toward zero


def test_brute_force_leave_one_out_agrees_with_the_hat_identity():
    rng = np.random.default_rng(1)
    x = np.sort(rng.uniform(-0.06, 0.06, 13))[None, :]
    iv = 0.14 + 0.2 * x + 1.5 * x**2 + rng.normal(0, 0.002, x.shape)
    w = rng.uniform(0.5, 3.0, x.shape)
    _, loo = fit_smile_loo(x, iv, w)
    for i in range(x.shape[1]):
        m = np.arange(x.shape[1]) != i
        c = np.polyfit(x[0, m], iv[0, m], 2, w=np.sqrt(w[0, m]))
        assert loo[0, i] == pytest.approx(np.polyval(c, x[0, i]), abs=1e-10)


def test_rows_with_too_few_strikes_stay_nan():
    x = np.array([[-0.01, 0.0, 0.01, np.nan]])
    iv = np.array([[0.2, 0.19, 0.2, np.nan]])
    fit, loo = fit_smile_loo(x, iv, np.ones_like(x))
    assert np.isnan(fit).all() and np.isnan(loo).all()
