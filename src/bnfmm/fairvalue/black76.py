"""Black-76 pricing, implied volatility and delta -- for risk, never for price.

WHY THIS EXISTS, AND WHAT IT IS NOT USED FOR
    ``strategy.quoter`` deliberately prices options without a volatility model
    (parity only), because a mis-specified smile produces one-sided quoting that
    looks like alpha. That decision stands. This module exists for a different job:
    **measuring directional risk.** A portfolio of calls and puts across strikes
    has one net exposure to the forward, and the only way to add a 0.9-delta ITM
    call to a 0.3-delta OTM put is in delta units. Phase 1 controlled inventory per
    leg, so short calls and long puts -- the same directional bet twice -- were
    never netted, and 58% of the one profitable run was that bet (BRIEFING §17.4).

    Delta is read off the market: implied volatility is solved from the option's
    own fair value, then delta follows from it. A wrong vol moves a delta by a few
    hundredths; it cannot move a price, because no price here comes from this
    module.

CONVENTIONS
    Black-76 on the forward ``F`` with discount ``D = exp(-rT)``. Indian index
    options are European and cash-settled, so this is the right model family.
    Delta is ``dV/dF`` -- sensitivity to the forward, which is what parity-implied
    forward moves are measured in. All functions are vectorised over numpy arrays.
"""

from __future__ import annotations

import numpy as np
from scipy.special import ndtr

#: Floor on time to expiry, in years (about 5 minutes). At T -> 0 an ATM
#: option's vol is unbounded in 1/sqrt(T) and delta becomes a step; the floor
#: keeps expiry-day deltas finite without pretending they are smooth.
MIN_T = 5.0 / (365 * 24 * 60)

VOL_LO, VOL_HI = 1e-4, 5.0


def _d1(f, k, t, vol):
    s = vol * np.sqrt(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (np.log(f / k) + 0.5 * s * s) / s


def price(f, k, t, vol, d, is_call) -> np.ndarray:
    """Black-76 option value."""
    f, k, t, vol, d = (np.asarray(x, dtype=float) for x in (f, k, t, vol, d))
    t = np.maximum(t, MIN_T)
    d1 = _d1(f, k, t, vol)
    d2 = d1 - vol * np.sqrt(t)
    call = d * (f * ndtr(d1) - k * ndtr(d2))
    put = d * (k * ndtr(-d2) - f * ndtr(-d1))
    return np.where(is_call, call, put)


def delta(f, k, t, vol, d, is_call) -> np.ndarray:
    """dV/dF: call ``D*N(d1)``, put ``-D*N(-d1)``."""
    f, k, t, vol, d = (np.asarray(x, dtype=float) for x in (f, k, t, vol, d))
    t = np.maximum(t, MIN_T)
    d1 = _d1(f, k, t, vol)
    return np.where(is_call, d * ndtr(d1), -d * ndtr(-d1))


def vega(f, k, t, vol, d) -> np.ndarray:
    """dV/dsigma per 1.00 of vol (divide by 100 for per vol point). Same for C and P."""
    f, k, t, vol, d = (np.asarray(x, dtype=float) for x in (f, k, t, vol, d))
    t = np.maximum(t, MIN_T)
    d1 = _d1(f, k, t, vol)
    return d * f * np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi) * np.sqrt(t)


def implied_vol(value, f, k, t, d, is_call, *, iters: int = 60) -> np.ndarray:
    """Vol that reproduces ``value``, by bisection; NaN where no vol can.

    Bisection rather than Newton because it cannot diverge on deep-ITM or
    near-expiry options where vega vanishes, and 60 halvings of [1e-4, 5] is far
    below any precision that matters for a delta. A value at or below intrinsic
    (possible on a stale or one-sided book) has no implied vol and returns NaN;
    callers fall back to the last good delta.
    """
    value, f, k, t, d = np.broadcast_arrays(
        *(np.asarray(x, dtype=float) for x in (value, f, k, t, d))
    )
    is_call = np.broadcast_to(np.asarray(is_call, dtype=bool), value.shape)
    t = np.maximum(t, MIN_T)
    intrinsic = d * np.where(is_call, np.maximum(f - k, 0.0), np.maximum(k - f, 0.0))
    upper = price(f, k, t, np.full_like(value, VOL_HI), d, is_call)
    ok = np.isfinite(value) & np.isfinite(f) & (value > intrinsic) & (value < upper)
    lo = np.full(value.shape, VOL_LO)
    hi = np.full(value.shape, VOL_HI)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        above = price(f, k, t, mid, d, is_call) > value
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)
