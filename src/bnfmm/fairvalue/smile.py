"""A cross-strike implied-volatility smile, fitted per instant, evaluated leave-one-out.

WHY THIS EXISTS
    BRIEFING §10.2 planned a fourth fair-value layer -- price options in IV space off
    a smooth curve fitted across the strike ladder -- and skipped it, because a
    mis-specified smile produces one-sided quoting that looks like alpha. The classic
    market maker then turned out to be latency-bound (DECISIONS #23): its edge
    decayed inside the ~0.5 s it takes a non-co-located participant to act. The
    rv-market-maker branch asks whether *cross-strike* mispricings -- one option rich
    or cheap relative to the smile its neighbours imply -- last long enough to trade
    with that latency. This module measures the smile; it does not trade.

THE FIT
    At each instant, for each strike, implied vol is solved from the out-of-the-money
    option's mid (OTM options are the liquid ones; parity makes the ITM partner
    redundant). A low-order polynomial in log-moneyness x = ln(K / F) is fitted by
    weighted least squares, each strike weighted by the inverse square of its IV
    uncertainty, (half-spread / vega)^2 -- a wide book barely moves the curve.

LEAVE-ONE-OUT, BECAUSE SELF-REFERENCE IS THE TRAP
    A strike's in-sample residual is shrunk toward zero by its own influence on the
    fit, most for the heavily weighted near-the-money strikes. The leave-one-out
    prediction removes that with the standard hat-matrix identity

        y_loo_i = y_i - r_i / (1 - h_ii)

    so each strike is valued by the smile the *other* strikes imply.
"""

from __future__ import annotations

import numpy as np


def fit_smile_loo(
    x: np.ndarray,
    iv: np.ndarray,
    weight: np.ndarray,
    *,
    degree: int = 2,
    min_points: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Fit ``iv ~ poly(x)`` row by row; return (in-sample fit, leave-one-out fit).

    All arrays are ``(T, K)``; NaN marks a strike with no usable IV at that instant.
    Rows with fewer than ``min_points`` usable strikes (default ``degree + 3``) are
    left NaN: a curve through barely more points than parameters has no
    leave-one-out meaning.
    """
    x, iv, weight = (np.asarray(a, dtype=float) for a in (x, iv, weight))
    T, K = iv.shape
    need = degree + 3 if min_points is None else min_points
    fit = np.full((T, K), np.nan)
    loo = np.full((T, K), np.nan)
    p = degree + 1
    for t in range(T):
        ok = np.isfinite(x[t]) & np.isfinite(iv[t]) & np.isfinite(weight[t]) & (weight[t] > 0)
        n = int(ok.sum())
        if n < need:
            continue
        X = np.vander(x[t, ok], p)
        sw = np.sqrt(weight[t, ok])
        Xw = X * sw[:, None]
        yw = iv[t, ok] * sw
        # Hat matrix diagonal from a thin QR of the weighted design.
        q, r = np.linalg.qr(Xw)
        if np.min(np.abs(np.diag(r))) < 1e-12:
            continue
        beta = np.linalg.solve(r, q.T @ yw)
        yhat = X @ beta
        h = np.sum(q * q, axis=1)
        resid = iv[t, ok] - yhat
        with np.errstate(divide="ignore", invalid="ignore"):
            yloo = iv[t, ok] - resid / (1.0 - h)
        yloo[h > 1 - 1e-9] = np.nan
        fit[t, ok] = yhat
        loo[t, ok] = yloo
    return fit, loo
