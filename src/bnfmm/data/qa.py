"""Data-quality checks on captured sessions -- BRIEFING §14 Phase 1c, §19.6.

WHY THIS EXISTS
    Every conclusion in this repo is computed on top of the captured books and
    the tape differenced from the quote feed. Three failures have already been
    found by hand that a check would have caught on day one: the index spot was
    silently never captured (§9.4), a whole session was lost to a bad token while
    depth looked healthy (§13.12), and five runs had no manifest (§13.11). This
    module turns "look at the data" into functions with numbers.

    Everything here is pure: arrays in, plain dicts out. The driver that walks
    the corpus and writes the report is ``scripts/qa_report.py``.

WHAT IS CHECKED, AND WHY EACH MATTERS TO A MARKET MAKER
    * coverage and gaps -- a fill cannot be simulated across a hole, and gaps
      inflate the inventory term (§19.2). A gap longer than ``max_gap_s`` ends a
      segment (§5.5); the day's coverage is the fraction of the continuous
      session that lies inside segments.
    * book sanity -- crossed/locked, one-sided, off-tick, non-positive and
      non-monotone ladders. A quoter that trusts a garbage touch manufactures
      fills against it.
    * tape sanity -- cumulative-volume rewinds (which the tape drops), exchange
      lag (``recv - last_trade_epoch``), out-of-order arrival, and the wall- vs
      monotonic-clock disagreement that distinguishes suspend from dropout.
    * cross-source agreement -- the quote feed carries its own 5-level book, so
      the depth feed's touch can be checked against an independent decoder on a
      different socket. Disagreement means one of them is wrong.
    * presence -- every subscribed id must have produced rows (§13.2's missing
      check, the one that would have caught §9.4).

    Nothing here repairs data. §19.7: fix the strategy's response to missing
    data, never the data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))
NS = 1_000_000_000
IST_OFFSET_S = 19_800

#: Continuous-session bounds. Pre-open (09:00-09:15) is a separate call auction
#: and is not part of the market a passive quoter can make.
SESSION_START = time(9, 15)
SESSION_END = time(15, 30)


def session_bounds_ns(day: date, start: time = SESSION_START, end: time = SESSION_END) -> tuple[int, int]:
    """Epoch-ns bounds of the continuous session on ``day``, IST."""
    a = datetime.combine(day, start, IST)
    b = datetime.combine(day, end, IST)
    return int(a.timestamp() * NS), int(b.timestamp() * NS)


def segments(ts_ns: np.ndarray, max_gap_s: float) -> list[tuple[int, int]]:
    """Split sorted timestamps into maximal runs with no gap above ``max_gap_s``."""
    ts = np.asarray(ts_ns, dtype=np.int64)
    if len(ts) == 0:
        return []
    cut = np.flatnonzero(np.diff(ts) > max_gap_s * NS)
    starts = np.r_[0, cut + 1]
    ends = np.r_[cut, len(ts) - 1]
    return [(int(ts[s]), int(ts[e])) for s, e in zip(starts, ends)]


def coverage(
    ts_ns: np.ndarray,
    window: tuple[int, int],
    *,
    max_gap_s: float = 30.0,
    small_gap_s: float = 2.0,
) -> dict:
    """Coverage of ``window`` by observations, and the gaps that break it.

    Only observations inside the window count. The time from the window's start
    to the first observation, and from the last to the window's end, are gaps
    like any other -- a session that starts late has lost its open.
    """
    lo, hi = window
    ts = np.asarray(ts_ns, dtype=np.int64)
    ts = np.sort(ts[(ts >= lo) & (ts <= hi)])
    span_s = (hi - lo) / NS
    if len(ts) == 0:
        return {"n": 0, "coverage": 0.0, "first": None, "last": None,
                "gaps_small": 0, "gaps_large": 0, "gap_s_total": span_s,
                "largest_gap_s": span_s, "segments": 0, "large_gaps": [(lo, hi)]}
    edges = np.r_[lo, ts, hi]
    d = np.diff(edges) / NS
    big = d > max_gap_s
    large = [(int(edges[i]), int(edges[i + 1])) for i in np.flatnonzero(big)]
    covered = span_s - float(d[big].sum())
    return {
        "n": int(len(ts)),
        "coverage": covered / span_s,
        "first": int(ts[0]),
        "last": int(ts[-1]),
        "gaps_small": int(((d > small_gap_s) & ~big).sum()),
        "gaps_large": int(big.sum()),
        "gap_s_total": float(d[d > small_gap_s].sum()),
        "largest_gap_s": float(d.max()),
        "segments": len(segments(ts, max_gap_s)),
        "large_gaps": large,
    }


def cadence(ts_ns: np.ndarray) -> dict:
    """Inter-arrival percentiles in ms, over intervals of at most 5 s."""
    d = np.diff(np.asarray(ts_ns, dtype=np.int64)) / 1e6
    d = d[(d > 0) & (d <= 5000)]
    if len(d) == 0:
        return {"p05_ms": None, "p50_ms": None, "p95_ms": None}
    p = np.percentile(d, [5, 50, 95])
    return {"p05_ms": float(p[0]), "p50_ms": float(p[1]), "p95_ms": float(p[2])}


def _off_tick(px: np.ndarray, tick: float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        r = px / tick
        return np.isfinite(px) & (np.abs(r - np.round(r)) > 1e-6)


def book_checks(
    bid_px: np.ndarray,
    bid_qty: np.ndarray,
    ask_px: np.ndarray,
    ask_qty: np.ndarray,
    *,
    tick: float,
    flags: np.ndarray | None = None,
) -> dict:
    """Sanity of a two-sided ladder series; arrays are ``(n, levels)``, NaN-padded."""
    n = len(bid_px)
    if n == 0:
        return {"snapshots": 0}
    bb, ba = bid_px[:, 0], ask_px[:, 0]
    fb, fa = np.isfinite(bb), np.isfinite(ba)
    two = fb & fa
    with np.errstate(invalid="ignore"):
        crossed = two & (bb > ba)
        locked = two & (bb == ba)
        spread_ticks = np.where(two & ~crossed & ~locked, (ba - bb) / tick, np.nan)
        # Ladders must be strictly monotone away from the touch.
        bid_bad = np.any(np.diff(bid_px, axis=1) >= 0, axis=1)
        ask_bad = np.any(np.diff(ask_px, axis=1) <= 0, axis=1)
    nonpos = np.any((bid_px <= 0) | (ask_px <= 0), axis=1)
    zero_qty = np.any((np.isfinite(bid_px) & (bid_qty <= 0)) |
                      (np.isfinite(ask_px) & (ask_qty <= 0)), axis=1)
    off = np.any(_off_tick(bid_px, tick), axis=1) | np.any(_off_tick(ask_px, tick), axis=1)
    fin = spread_ticks[np.isfinite(spread_ticks)]
    return {
        "snapshots": n,
        "two_sided_pct": 100 * float(two.mean()),
        "one_sided_pct": 100 * float((fb ^ fa).mean()),
        "empty_pct": 100 * float((~fb & ~fa).mean()),
        "crossed_pct": 100 * float(crossed.mean()),
        "locked_pct": 100 * float(locked.mean()),
        "nonmonotone_pct": 100 * float((bid_bad | ask_bad).mean()),
        "nonpositive_px": int(nonpos.sum()),
        "zero_qty_level": int(zero_qty.sum()),
        "off_tick": int(off.sum()),
        "suspect_flag_pct": 100 * float(np.asarray(flags).mean()) if flags is not None else None,
        "spread_ticks_p50": float(np.median(fin)) if len(fin) else None,
        "spread_ticks_p90": float(np.percentile(fin, 90)) if len(fin) else None,
    }


def tape_checks(
    recv_wall_ns: np.ndarray,
    recv_mono_ns: np.ndarray,
    volume: np.ndarray,
    ltp: np.ndarray,
    last_trade_epoch: np.ndarray,
    *,
    tick: float,
) -> dict:
    """Sanity of one instrument's quote-feed series, in arrival order."""
    w = np.asarray(recv_wall_ns, dtype=np.int64)
    m = np.asarray(recv_mono_ns, dtype=np.int64)
    v = np.asarray(volume, dtype=np.int64)
    n = len(w)
    if n == 0:
        return {"packets": 0}
    dv = np.diff(v)
    lte = np.asarray(last_trade_epoch, dtype=np.int64)
    moved = np.r_[False, dv > 0]
    lag_s = (w[moved] / NS) - lte[moved]
    lag_s = lag_s[np.isfinite(lag_s) & (lte[moved] > 0)]
    # Dhan stamps last_trade_epoch as IST wall-clock seconds, not UTC epoch, so
    # the raw difference sits near -19,800 s (-5h30m). Detect that rather than
    # assume it, and report the lag after the correction.
    ist_offset = bool(len(lag_s)) and abs(float(np.median(lag_s)) + IST_OFFSET_S) < 3600
    if ist_offset:
        lag_s = lag_s + IST_OFFSET_S
    # Wall and monotonic clocks advance together unless the host suspended or
    # NTP stepped; their per-interval disagreement is what classifies a gap.
    skew_ms = np.abs(np.diff(w) - np.diff(m)) / 1e6
    return {
        "packets": n,
        "volume_first": int(v[0]),
        "volume_last": int(v[-1]),
        "rewinds": int((dv < 0).sum()),
        "rewind_units": int(-dv[dv < 0].sum()),
        "trade_packets": int((dv > 0).sum()),
        "out_of_order": int((np.diff(w) < 0).sum()),
        "ltp_off_tick": int(_off_tick(np.asarray(ltp, dtype=float), tick).sum()),
        "ltp_nonpositive": int((np.asarray(ltp) <= 0).sum()),
        "exch_epoch_is_ist": ist_offset,
        "exch_lag_s_p50": float(np.median(lag_s)) if len(lag_s) else None,
        "exch_lag_s_p99": float(np.percentile(lag_s, 99)) if len(lag_s) else None,
        "clock_skew_ms_max": float(skew_ms.max()) if len(skew_ms) else 0.0,
        "clock_skew_events_gt1s": int((skew_ms > 1000).sum()),
    }


def touch_agreement(
    depth_ns: np.ndarray,
    depth_bid: np.ndarray,
    depth_ask: np.ndarray,
    quote_ns: np.ndarray,
    quote_bid: np.ndarray,
    quote_ask: np.ndarray,
    *,
    tick: float,
    max_age_s: float = 1.0,
) -> dict:
    """How often the quote feed's level-1 agrees with the depth feed's.

    For each quote packet, compare with the latest depth snapshot at or before
    it, if that snapshot is at most ``max_age_s`` old. Both books are sampled at
    different instants from different sockets, so exact agreement is not
    expected on every row; a systematic disagreement is what this looks for.
    """
    dn = np.asarray(depth_ns, dtype=np.int64)
    qn = np.asarray(quote_ns, dtype=np.int64)
    if len(dn) == 0 or len(qn) == 0:
        return {"compared": 0}
    j = np.searchsorted(dn, qn, side="right") - 1
    ok = (j >= 0)
    ok[ok] &= (qn[ok] - dn[j[ok]]) <= max_age_s * NS
    qb, qa = np.asarray(quote_bid, dtype=float), np.asarray(quote_ask, dtype=float)
    ok &= np.isfinite(qb) & np.isfinite(qa)
    idx = np.flatnonzero(ok)
    if len(idx) == 0:
        return {"compared": 0}
    db, da = depth_bid[j[idx]], depth_ask[j[idx]]
    ok2 = np.isfinite(db) & np.isfinite(da)
    idx, db, da = idx[ok2], db[ok2], da[ok2]
    if len(idx) == 0:
        return {"compared": 0}
    bdiff = np.abs(qb[idx] - db) / tick
    adiff = np.abs(qa[idx] - da) / tick
    both = (bdiff < 0.5) & (adiff < 0.5)
    return {
        "compared": int(len(idx)),
        "exact_pct": 100 * float(both.mean()),
        "within_2ticks_pct": 100 * float(((bdiff <= 2) & (adiff <= 2)).mean()),
        "mid_diff_ticks_p99": float(np.percentile(np.abs((qb[idx] + qa[idx]) - (db + da)) / 2 / tick, 99)),
    }


def presence(subscribed: Iterable[int], present: Iterable[int]) -> dict:
    """Subscribed ids that produced no rows, and ids that appeared unsubscribed."""
    sub, got = set(subscribed), set(present)
    return {"subscribed": len(sub), "present": len(sub & got),
            "missing": sorted(sub - got), "unexpected": sorted(got - sub)}


def include_day(depth_cov: float, feed_cov: float, *, min_depth: float, min_feed: float) -> tuple[bool, str]:
    """The frozen inclusion rule (config/frozen/protocol.yaml), applied mechanically."""
    reasons = []
    if depth_cov < min_depth:
        reasons.append(f"depth coverage {depth_cov:.0%} < {min_depth:.0%}")
    if feed_cov < min_feed:
        reasons.append(f"feed coverage {feed_cov:.0%} < {min_feed:.0%}")
    return (not reasons, "; ".join(reasons) or "meets inclusion rule")


def fmt_ist(ns: int | None) -> str:
    if ns is None:
        return "--"
    return datetime.fromtimestamp(ns / NS, IST).strftime("%H:%M:%S")


def median_of(rows: Sequence[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return float(np.median(vals)) if vals else None
