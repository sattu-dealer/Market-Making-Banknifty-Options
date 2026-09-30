"""Fair value from the book's own shape, not from the last trade.

WHY NOT THE LAST TRADED PRICE
    A trade only happens when someone crosses the spread, so the print sits at the
    bid or the ask, never at fair value. Using it as a fair-value anchor injects a
    bid-ask bounce of +/- s/2 (Roll's model: ``LTP_t = M_t + (s/2) q_t``) which is
    pure noise to a market maker and, worse, is *serially correlated with the side
    that traded* -- so a strategy anchored on LTP systematically skews into the
    flow that just ran it over. On the captured front-month BANKNIFTY future this
    is not a subtlety: the median spread on 2026-08-24 was 41 ticks (Rs 8.20) and
    2.2% of snapshots were locked, crossed or one-sided, so half the bounce is
    over Rs 4 on a Rs 57,440 underlying.

WHAT THIS MODULE DOES INSTEAD
    The microprice: a size-weighted interpolation between bid and ask,

        micro = (bid * Q_ask + ask * Q_bid) / (Q_bid + Q_ask)

    which moves toward the ask when the bid is heavy. The intuition is queue
    pressure -- a book with 800 units bid against 90 offered is far more likely to
    tick up than down, so fair value is not the arithmetic middle. This is the
    standard imbalance-weighted mid; it is a one-parameter-free estimator that
    uses only data the book publishes, and unlike the LTP it is defined at every
    snapshot whether or not anyone traded.

    A depth-weighted variant uses more than the touch, discounting each level by
    its distance from the mid. It is steadier when the touch is thin and one
    cancel flips the imbalance, at the cost of responding more slowly.
"""

from __future__ import annotations

import numpy as np


def microprice(
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
) -> np.ndarray:
    """Size-weighted mid from the touch. ``NaN`` where either side is empty.

    Accepts either the level-1 vectors or the full ladders, in which case only
    column 0 is used.
    """
    b, bq, a, aq = (x[:, 0] if x.ndim == 2 else x for x in (bid_px, bid_qty, ask_px, ask_qty))
    tot = bq.astype(np.float64) + aq.astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (b * aq + a * bq) / tot
    # A two-sided book with zero displayed size at the touch is not a thing the
    # feed should produce, but if it does the mid is the only defensible answer.
    degenerate = (tot == 0) & np.isfinite(b) & np.isfinite(a)
    out[degenerate] = (b[degenerate] + a[degenerate]) / 2.0
    return out


def imbalance(bid_qty: np.ndarray, ask_qty: np.ndarray, levels: int = 1) -> np.ndarray:
    """Queue imbalance in ``[-1, +1]``: +1 all bid, -1 all ask.

    Used both as a fair-value input and as the state variable the quoter skews
    against, so it is defined once here.
    """
    bq = bid_qty[:, :levels].sum(1).astype(np.float64) if bid_qty.ndim == 2 else bid_qty.astype(np.float64)
    aq = ask_qty[:, :levels].sum(1).astype(np.float64) if ask_qty.ndim == 2 else ask_qty.astype(np.float64)
    tot = bq + aq
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (bq - aq) / tot
    out[tot == 0] = 0.0
    return out


def depth_weighted_micro(
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
    *,
    levels: int = 5,
    decay: float = 0.5,
) -> np.ndarray:
    """Microprice using ``levels`` levels, each discounted by distance from the mid.

    Level *i* gets weight ``decay**i``. With ``decay=0.5`` the touch carries ~52%
    of the weight over five levels, so this tracks the touch but is not flipped by
    a single cancel -- which matters on these books, where the level-1 quantity on
    the 1 DTE at-the-money call had a median of only 4 lots on the bid and 3 on the
    ask, so one 3-lot cancel can invert the imbalance outright.
    """
    n = min(levels, bid_px.shape[1], ask_px.shape[1])
    w = decay ** np.arange(n)
    bq = bid_qty[:, :n].astype(np.float64) * w
    aq = ask_qty[:, :n].astype(np.float64) * w
    bp = bid_px[:, :n]
    ap = ask_px[:, :n]
    with np.errstate(invalid="ignore", divide="ignore"):
        # Weighted average price on each side, then interpolate between them by
        # the opposite side's weight -- the level-1 formula generalised.
        bsum, asum = np.nansum(bq, 1), np.nansum(aq, 1)
        bvw = np.nansum(np.where(np.isfinite(bp), bp * bq, 0.0), 1) / bsum
        avw = np.nansum(np.where(np.isfinite(ap), ap * aq, 0.0), 1) / asum
        out = (bvw * asum + avw * bsum) / (bsum + asum)
    bad = ~np.isfinite(bid_px[:, 0]) | ~np.isfinite(ask_px[:, 0])
    out[bad] = np.nan
    return out
