"""Imply the forward from the option books by put-call parity.

THE PROBLEM THIS SOLVES
    To quote a BANKNIFTY option you need the forward for its expiry. Three obvious
    sources are all unavailable or unusable on the captured days:

    1.  *The index spot.* Not captured. Security ids 25 (NIFTY BANK) and 13
        (NIFTY 50) were subscribed on the general feed, but with request code 21
        (Full), and Dhan does not serve Full packets for the ``IDX_I`` segment --
        an index has no book and no open interest. The subscriptions were silently
        ignored: all 7,356,944 quote rows on 2026-08-24 are code 8 on segment 2,
        and zero rows exist for either index. So ``F = S + basis`` has no ``S``.

    2.  *The front future's last traded price.* Rejected on principle -- a print is
        at the bid or the ask, so it carries the full bid-ask bounce (see
        ``microprice``).

    3.  *The front future's book.* Measured unusable on 2026-08-24, the day before
        the August expiry: median spread 41 ticks (Rs 8.20), 95th percentile 121
        ticks, and 2.2% of snapshots locked, crossed or outright one-sided as
        liquidity had already rolled to September. A mid drawn from a one-sided
        book is not a wide estimate, it is a wrong one.

THE MODEL-FREE ANSWER
    For European options on the same forward and expiry, put-call parity is an
    identity, not a model -- no volatility, no distribution, no Black-Scholes:

        C(K) - P(K) = D * (F - K)        =>        F = K + (C(K) - P(K)) / D

    with ``D = exp(-r*tau)`` the discount factor to expiry. BANKNIFTY options are
    European and cash-settled, so this holds exactly.

    Applied to *tradeable* prices it gives a two-sided bound rather than a point,
    because buying the synthetic forward means paying the call's ask and receiving
    the put's bid:

        F >= K + (C_bid - P_ask) / D          (else sell synthetic, arbitrage)
        F <= K + (C_ask - P_bid) / D          (else buy synthetic, arbitrage)

    Every strike produces such an interval and the true forward lies in *all* of
    them, so the intersection

        [ max_K F_lo(K),  min_K F_hi(K) ]

    is a valid bound that is far tighter than any single strike's -- 23 strikes
    were captured per expiry, and the binding constraints come from whichever
    strikes happen to be tightest at that instant. This is the estimator used
    here. It touches no trade prints, needs no volatility surface, and degrades
    gracefully: if some strikes are stale or one-sided they simply widen their own
    interval and stop binding, instead of corrupting a weighted average.

    Within the intersection a point estimate is still needed to quote against.
    That is a precision-weighted average of the per-strike microprice-implied
    forwards, weighted by ``1 / (spread_C + spread_P)^2`` so that tight, liquid,
    near-the-money strikes dominate, then clipped into the intersection.

WHEN THE INTERSECTION IS EMPTY
    A crossed intersection (``F_lo > F_hi``) means two strikes disagree beyond
    what any single forward can satisfy. On real data that is almost never true
    arbitrage; it is staleness -- one strike's snapshot is older than another's, so
    they describe different instants. Those samples are flagged
    (``arb_violation``) and the point estimate falls back to the weighted average
    without clipping. They are never silently dropped, because their *rate* is a
    direct measure of how much cross-strike staleness the 200 ms cadence induces,
    which is exactly the kind of thing that belongs in the writeup.
"""

from __future__ import annotations

import warnings

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from ..book.reconstruct import BookSeries
from .microprice import microprice

IST = timezone(timedelta(hours=5, minutes=30))

#: Annual risk-free rate used only for the parity discount factor. Over the one-
#: and zero-day horizons here the discount is 0.9998 and 1.0000, so the estimate
#: is insensitive to this; it is carried for correctness, not for accuracy.
DEFAULT_RATE = 0.065

#: Indian index options expire at the 15:30 IST close on expiry day.
EXPIRY_TIME = time(15, 30)


def year_fraction(now_ns: np.ndarray, expiry: date) -> np.ndarray:
    """Time to expiry in years (ACT/365), floored at zero."""
    end = datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST).timestamp() * 1e9
    return np.maximum((end - now_ns) / 1e9, 0.0) / (365.0 * 24 * 3600)


def discount(now_ns: np.ndarray, expiry: date, rate: float = DEFAULT_RATE) -> np.ndarray:
    return np.exp(-rate * year_fraction(now_ns, expiry))


@dataclass(frozen=True, slots=True)
class ImpliedForward:
    """The parity-implied forward on a common clock, with its arbitrage bounds.

    ``forward`` is a *precision-weighted microprice estimate*, not the model-free
    bound. The distinction is load-bearing and comes from measurement: on
    2026-08-24 the per-strike synthetic-forward microprice was internally
    consistent to a weighted std of ~1.2 rupees across 18 strikes, but the
    per-strike no-arbitrage interval was only ~2.5 rupees wide -- so the strict
    *intersection* of all 18 intervals was empty 95% of the time. That is not
    executable arbitrage: it is cross-strike microprice dispersion
    (queue-imbalance signal differs by strike) exceeding raw spread, and it is
    real information, not noise. So this class reports both:

      * ``forward`` / ``dispersion`` -- where fair value sits, and how tight the
        near-the-money strikes pin it.
      * ``lo`` / ``hi`` -- a *robust* band: the median of the per-strike lower and
        upper bounds. This brackets ``forward`` and is never empty.
      * ``int_lo`` / ``int_hi`` / ``arb_violation`` -- the strict intersection
        over all strikes, which is the genuine no-arbitrage check. A negative
        width means the strike ladder is more dispersed than its own spreads; the
        *rate* of that is a direct measure of cross-strike staleness at a 200 ms
        cadence and belongs in the writeup, not silently smoothed away.

    Median values are computed across strikes, so a few stale or one-sided far-OTM
    strikes widen their own interval without dragging the whole day down.
    """

    recv_wall_ns: np.ndarray  # int64 (n,)
    forward: np.ndarray  # float64 (n,) point estimate, NaN where unresolvable
    dispersion: np.ndarray  # float64 (n,) weighted std of per-strike estimates
    lo: np.ndarray  # float64 (n,) median lower bound
    hi: np.ndarray  # float64 (n,) median upper bound
    n_strikes: np.ndarray  # int32 (n,) strikes contributing a usable pair
    arb_violation: np.ndarray  # bool (n,) strict intersection was empty
    int_lo: np.ndarray  # float64 (n,) intersection lower bound (may exceed int_hi)
    int_hi: np.ndarray  # float64 (n,)
    binding_lo: np.ndarray  # float64 (n,) strike setting the intersection lower
    binding_hi: np.ndarray  # float64 (n,)

    def __len__(self) -> int:
        return len(self.recv_wall_ns)

    @property
    def width(self) -> np.ndarray:
        """Width of the robust band, ``hi - lo`` -- the honest error bar."""
        return self.hi - self.lo

    @property
    def int_width(self) -> np.ndarray:
        """Width of the strict intersection; negative where it is empty."""
        return self.int_hi - self.int_lo

    def summary(self) -> dict[str, float]:
        ok = np.isfinite(self.forward)
        return {
            "samples": int(len(self.forward)),
            "resolved": int(ok.sum()),
            "resolved_pct": float(100.0 * ok.mean()) if len(ok) else 0.0,
            "median_strikes": float(np.median(self.n_strikes[ok])) if ok.any() else 0.0,
            "median_dispersion": float(np.median(self.dispersion[ok])) if ok.any() else float("nan"),
            "median_width": float(np.median(self.width[ok])) if ok.any() else float("nan"),
            "p95_width": float(np.percentile(self.width[ok], 95)) if ok.any() else float("nan"),
            "arb_violation_pct": float(100.0 * self.arb_violation.mean()) if len(ok) else 0.0,
        }


def implied_forward(
    grid: np.ndarray,
    pairs: dict[float, tuple[BookSeries, BookSeries]],
    index: dict[int, np.ndarray],
    expiry: date,
    *,
    rate: float = DEFAULT_RATE,
    min_strikes: int = 2,
) -> ImpliedForward:
    """Solve parity across strikes on a common clock.

    ``pairs`` maps strike -> (call book, put book); ``index`` maps each book's
    ``security_id`` to its row index at each grid point, as returned by
    ``book.reconstruct.align`` (last observation at or before the grid time, -1
    before the instrument's first snapshot). Both books of a strike must be live
    at a grid point for that strike to contribute.

    Aggregation is a two-pass compose: build the per-strike matrices, then take
    the precision-weighted microprice as the point estimate and report the
    intersection and the median band as diagnostics. The precision weight is
    ``1 / (spread_C + spread_P)^2`` -- parity's error is the sum of the two
    half-spreads, so a tight near-the-money pair pins the forward far harder than
    a wide one. The far-OTM wings on these books carry spreads of up to ~10
    rupees (mostly the deep-ITM put), against ~1 rupee at the touch, so weighting
    by inverse square makes them effectively vanish from the point estimate while
    leaving their own bounds to widen the diagnostic band.
    """
    n = len(grid)
    d = discount(grid, expiry, rate)

    # Per-strike results, NaN outside each strike's live window. One row per
    # strike: 92k grid points x 18 strikes x 8 B x 3 arrays ~ 40 MB per day,
    # which buys an exact median and an exact weighted variance in one pass.
    ks = sorted(pairs)
    lo_m = np.full((len(ks), n), np.nan)
    hi_m = np.full((len(ks), n), np.nan)
    mid_m = np.full((len(ks), n), np.nan)
    w_m = np.zeros((len(ks), n))

    for r, strike in enumerate(ks):
        call, put = pairs[strike]
        ic, ip = index[call.security_id], index[put.security_id]
        live = (ic >= 0) & (ip >= 0)
        if not live.any():
            continue
        cb, ca = _touch(call, ic, live)
        pb, pa = _touch(put, ip, live)
        cbq, caq = _touch_qty(call, ic, live)
        pbq, paq = _touch_qty(put, ip, live)

        ok = np.isfinite(cb) & np.isfinite(ca) & np.isfinite(pb) & np.isfinite(pa)
        # A crossed option book cannot bound anything; exclude it at this strike.
        ok &= (ca >= cb) & (pa >= pb)
        if not ok.any():
            continue

        lo_m[r, ok] = (strike + (cb - pa) / d)[ok]
        hi_m[r, ok] = (strike + (ca - pb) / d)[ok]

        c_mic = microprice(cb, cbq, ca, caq)
        p_mic = microprice(pb, pbq, pa, paq)
        f_mid = strike + (c_mic - p_mic) / d
        combined = (ca - cb) + (pa - pb)
        with np.errstate(divide="ignore", invalid="ignore"):
            w = 1.0 / np.square(np.maximum(combined, 1e-6))
        good = ok & np.isfinite(f_mid) & np.isfinite(w)
        mid_m[r, good] = f_mid[good]
        w_m[r, good] = w[good]

    count = np.isfinite(mid_m).sum(0).astype(np.int32)
    enough = count >= min_strikes

    # Precision-weighted point estimate and its weighted standard deviation.
    with np.errstate(invalid="ignore", divide="ignore"):
        wsum = w_m.sum(0)
        point = np.nansum(np.where(np.isfinite(mid_m), w_m * mid_m, 0.0), 0) / wsum
        var = np.nansum(
            np.where(np.isfinite(mid_m), w_m * np.square(mid_m - point), 0.0), 0
        ) / wsum
    dispersion = np.sqrt(np.maximum(var, 0.0))
    point[~enough] = np.nan
    dispersion[~enough] = np.nan

    # Strict intersection: the tightest lower and upper bound over all strikes.
    # An all-NaN column (no strike live) is expected at session edges, so the
    # all-NaN warnings from nanmax/nanmin/nanmedian are suppressed rather than
    # avoided by a mask -- the result is NaN, which `enough` already gates.
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        int_lo = np.nanmax(lo_m, 0) if len(ks) else np.full(n, np.nan)
        int_hi = np.nanmin(hi_m, 0) if len(ks) else np.full(n, np.nan)
        arg_lo = np.nanargmax(np.where(np.isfinite(lo_m), lo_m, -np.inf), 0)
        arg_hi = np.nanargmin(np.where(np.isfinite(hi_m), hi_m, np.inf), 0)
    karr = np.asarray(ks, dtype=np.float64)
    binding_lo = np.where(enough, karr[arg_lo], np.nan)
    binding_hi = np.where(enough, karr[arg_hi], np.nan)

    # Robust band: median across strikes of the per-strike bounds. Never empty,
    # so it is what a quoter may safely use as an error bar.
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med_lo = np.nanmedian(lo_m, 0)
        med_hi = np.nanmedian(hi_m, 0)
    med_lo[~enough] = np.nan
    med_hi[~enough] = np.nan

    violation = enough & np.isfinite(int_lo) & np.isfinite(int_hi) & (int_lo > int_hi)
    return ImpliedForward(
        recv_wall_ns=grid,
        forward=point,
        dispersion=dispersion,
        lo=med_lo,
        hi=med_hi,
        n_strikes=count,
        arb_violation=violation,
        int_lo=np.where(enough, int_lo, np.nan),
        int_hi=np.where(enough, int_hi, np.nan),
        binding_lo=binding_lo,
        binding_hi=binding_hi,
    )


def _touch(book: BookSeries, idx: np.ndarray, live: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Best bid and ask at grid points, ``NaN`` where the book was not live."""
    n = len(idx)
    b = np.full(n, np.nan)
    a = np.full(n, np.nan)
    j = idx[live]
    b[live] = book.bid_px[j, 0]
    a[live] = book.ask_px[j, 0]
    return b, a


def _touch_qty(book: BookSeries, idx: np.ndarray, live: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(idx)
    bq = np.zeros(n)
    aq = np.zeros(n)
    j = idx[live]
    bq[live] = book.bid_qty[j, 0]
    aq[live] = book.ask_qty[j, 0]
    return bq, aq
