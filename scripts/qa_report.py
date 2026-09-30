"""Data-quality sanity check across every captured session.

USAGE
    python scripts/qa_report.py                    # every captured day
    python scripts/qa_report.py --date 2026-08-27
    python scripts/qa_report.py --workers 8

WRITES
    reports/qa/<date>.json          every per-instrument number
    reports/data_quality.md         the summary, one table per concern

Reads Parquet only. No API call, no entitlement, no order endpoint. The checks
themselves live in ``bnfmm.data.qa`` and are unit-tested there; this script only
walks the corpus. See that module's docstring for why each check exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.analysis.holdout import load_protocol  # noqa: E402
from bnfmm.book.reconstruct import load_book  # noqa: E402
from bnfmm.data import qa, store  # noqa: E402
from bnfmm.fairvalue.parity import discount  # noqa: E402

ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"
OUT_DIR = _REPO_ROOT / "reports" / "qa"
REPORT = _REPO_ROOT / "reports" / "data_quality.md"


def contracts_for(day: date) -> tuple[dict[int, dict], set[int], set[int]]:
    """Union of every run's sidecar on ``day``: id -> contract, depth ids, feed ids."""
    by_id: dict[int, dict] = {}
    depth: set[int] = set()
    feed: set[int] = set()
    for p in sorted((SESSIONS / f"date={day}").glob("*-contracts.json")):
        side = json.loads(p.read_text())
        for chan, body in side["channels"].items():
            for c in body["contracts"]:
                by_id[c["security_id"]] = c
                (depth if chan == "depth" else feed).add(c["security_id"])
    return by_id, depth, feed


def runs_for(day: date) -> list[str]:
    return sorted(p.name.split("-contracts")[0]
                  for p in (SESSIONS / f"date={day}").glob("*-contracts.json"))


def present_ids(table: str, day: date) -> set[int]:
    base = ROOT / table / f"date={day}"
    return {int(d.name.split("=", 1)[1]) for d in base.glob("security_id=*")
            if any(d.glob("*.parquet"))}


def check_instrument(day: date, sid: int, contract: dict, window: tuple[int, int],
                     max_gap_s: float) -> dict:
    """Every per-instrument check for one depth-channel id on one day."""
    tick = float(contract.get("tick_size") or 0.05)
    out: dict = {"security_id": sid, "name": contract.get("display_name"),
                 "instrument": contract.get("instrument"), "strike": contract.get("strike"),
                 "option_type": contract.get("option_type"), "expiry": contract.get("expiry"),
                 "tick": tick}

    book = load_book(str(ROOT), day, sid)
    t = book.recv_wall_ns
    inwin = (t >= window[0]) & (t <= window[1])
    out["depth_cov"] = qa.coverage(t, window, max_gap_s=max_gap_s)
    out["depth_cadence"] = qa.cadence(t[inwin])
    out["book"] = qa.book_checks(book.bid_px[inwin], book.bid_qty[inwin],
                                 book.ask_px[inwin], book.ask_qty[inwin],
                                 tick=tick, flags=book.flags[inwin])

    raw = store.read_capture(str(ROOT), "depth", dates=[day], security_ids=[sid])
    if raw.num_rows:
        w = np.asarray(raw.column("recv_wall_ns"), dtype=np.int64)
        s = np.asarray(raw.column("side")) == "bid"
        key = w * 2 + s
        out["depth_duplicate_rows"] = int(len(key) - len(np.unique(key)))
        out["depth_rows"] = int(raw.num_rows)
    del raw

    q = store.read_capture(str(ROOT), "quotes", dates=[day], security_ids=[sid])
    if q.num_rows:
        qw = np.asarray(q.column("recv_wall_ns"), dtype=np.int64)
        out["feed_cov"] = qa.coverage(qw, window, max_gap_s=max_gap_s)
        out["tape"] = qa.tape_checks(
            qw, np.asarray(q.column("recv_mono_ns"), dtype=np.int64),
            np.asarray(q.column("volume"), dtype=np.int64),
            np.asarray(q.column("ltp"), dtype=float),
            np.asarray(q.column("last_trade_epoch"), dtype=np.int64), tick=tick)
        qb = _level1(q.column("bid_price"))
        qa_ = _level1(q.column("ask_price"))
        out["touch_agreement"] = qa.touch_agreement(
            book.recv_wall_ns, book.best_bid, book.best_ask, qw, qb, qa_, tick=tick)
    else:
        out["feed_cov"] = qa.coverage(np.zeros(0, dtype=np.int64), window, max_gap_s=max_gap_s)
        out["tape"] = {"packets": 0}
        out["touch_agreement"] = {"compared": 0}
        qw = np.zeros(0, dtype=np.int64)
    # Clocks for the day-level union, sampled inside the window; popped before
    # the JSON is written.
    out["_depth_ts"] = t[inwin]
    out["_feed_ts"] = qw[(qw >= window[0]) & (qw <= window[1])]
    return out


def _level1(col) -> np.ndarray:
    arr = col.combine_chunks()
    offsets = np.asarray(arr.offsets, dtype=np.int64)
    values = np.asarray(arr.values, dtype=np.float64)
    lens = np.diff(offsets)
    out = np.full(len(lens), np.nan)
    ok = lens > 0
    out[ok] = values[offsets[:-1][ok]]
    out[out <= 0] = np.nan  # the 5-level block zero-pads empty levels
    return out


def parity_vs_future(day: date, contracts: dict[int, dict], depth_ids: set[int],
                     expiry: date, window: tuple[int, int]) -> dict:
    """Cross-source check: ATM put-call parity forward against the future's mid.

    Two independent instruments must describe one forward. A persistent offset
    far outside the future's own spread would mean one source is mislabelled or
    misdecoded.
    """
    fut = [c for i, c in contracts.items() if i in depth_ids and c["instrument"] == "FUTIDX"
           and c["expiry"] == str(expiry)]
    if not fut:
        return {"available": False, "reason": f"no depth-captured future expiring {expiry}"}
    fb = load_book(str(ROOT), day, fut[0]["security_id"], levels=1)
    pairs: dict[float, dict[str, int]] = {}
    for i, c in contracts.items():
        if i in depth_ids and c["instrument"] == "OPTIDX" and c["expiry"] == str(expiry):
            pairs.setdefault(c["strike"], {})[c["option_type"]] = i
    pairs = {k: v for k, v in pairs.items() if len(v) == 2}
    if not pairs:
        return {"available": False, "reason": "no complete CE/PE pair"}
    fmid = np.nanmedian(fb.mid[(fb.recv_wall_ns >= window[0]) & (fb.recv_wall_ns <= window[1])])
    k = min(pairs, key=lambda s: abs(s - fmid))
    cb = load_book(str(ROOT), day, pairs[k]["CE"], levels=1)
    pb = load_book(str(ROOT), day, pairs[k]["PE"], levels=1)
    grid = cb.recv_wall_ns[(cb.recv_wall_ns >= window[0]) & (cb.recv_wall_ns <= window[1])]
    grid = grid[::25]  # ~5 s sampling is plenty for a level comparison
    def at(b):
        j = np.searchsorted(b.recv_wall_ns, grid, side="right") - 1
        m = np.where(j >= 0, b.mid[np.maximum(j, 0)], np.nan)
        s = np.where(j >= 0, b.spread[np.maximum(j, 0)], np.nan)
        return m, s
    cm, _ = at(cb)
    pm, _ = at(pb)
    fm, fs = at(fb)
    d = discount(grid, expiry)
    fwd = k + (cm - pm) / d
    diff = fwd - fm
    ok = np.isfinite(diff)
    if not ok.any():
        return {"available": False, "reason": "no overlapping two-sided samples"}
    return {
        "available": True, "strike": k, "future": fut[0]["display_name"],
        "samples": int(ok.sum()),
        "diff_pts_p50": float(np.median(diff[ok])),
        "absdiff_pts_p50": float(np.median(np.abs(diff[ok]))),
        "absdiff_pts_p95": float(np.percentile(np.abs(diff[ok]), 95)),
        "future_spread_pts_p50": float(np.nanmedian(fs)),
    }


def run_day(day: date, workers: int, max_gap_s: float) -> dict:
    proto = load_protocol()
    window = qa.session_bounds_ns(day)
    contracts, depth_ids, feed_ids = contracts_for(day)
    pres = {
        "depth": qa.presence(depth_ids, present_ids("depth", day)),
        "feed": qa.presence(feed_ids, present_ids("quotes", day)),
    }
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(check_instrument, day, sid, contracts[sid], window, max_gap_s)
                for sid in sorted(depth_ids) if sid not in pres["depth"]["missing"]]
        inst = [f.result() for f in futs]

    # Day-level coverage on the union of the depth ids' clocks: the market maker
    # needs *a* live book, and one instrument's quiet spell is not a feed gap.
    empty = np.zeros(0, dtype=np.int64)
    depth_ts = np.unique(np.concatenate([i.pop("_depth_ts") for i in inst] or [empty]))
    feed_ts = np.unique(np.concatenate([i.pop("_feed_ts") for i in inst] or [empty]))
    dcov = qa.coverage(depth_ts, window, max_gap_s=max_gap_s)
    fcov = qa.coverage(feed_ts, window, max_gap_s=max_gap_s)
    included, why = qa.include_day(dcov["coverage"], fcov["coverage"],
                                   min_depth=proto.inclusion.min_depth_coverage,
                                   min_feed=proto.inclusion.min_feed_coverage)
    expiry = proto.expiry_for(day)
    return {
        "date": str(day),
        "expiry": str(expiry),
        "dte": (expiry - day).days,
        "split": ("develop" if day in proto.develop else
                  "holdout" if day in proto.holdout else "excluded"),
        "runs": runs_for(day),
        "presence": pres,
        "depth_cov": dcov,
        "feed_cov": fcov,
        "included": included,
        "inclusion_reason": why,
        "parity_vs_future": parity_vs_future(day, contracts, depth_ids, expiry, window),
        "instruments": inst,
    }


def _pct(x):
    return "--" if x is None else f"{x:.1f}%"


def write_report(days: list[dict]) -> None:
    L: list[str] = []
    L += ["# Data quality report", "",
          "Generated by `scripts/qa_report.py` from `bnfmm.data.qa`. Tier A (real captured) data "
          "only. Window: continuous session 09:15:00-15:30:00 IST. A gap longer than 30 s ends a "
          "segment (BRIEFING §5.5). Per-instrument numbers are in `reports/qa/<date>.json`.", ""]

    L += ["## 1. Session coverage and the frozen inclusion rule", "",
          "| Date | Split | DTE | Runs | Depth first→last | Depth cov | Feed cov | Gaps >30 s | "
          "Gap s >2 s | Largest | Included |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in days:
        dc, fc = d["depth_cov"], d["feed_cov"]
        L.append(f"| {d['date']} | {d['split']} | {d['dte']} | {len(d['runs'])} | "
                 f"{qa.fmt_ist(dc['first'])}→{qa.fmt_ist(dc['last'])} | {dc['coverage']:.1%} | "
                 f"{fc['coverage']:.1%} | {dc['gaps_large']} | {dc['gap_s_total']:.0f} | "
                 f"{dc['largest_gap_s']:.0f} s | {'yes' if d['included'] else '**no** — ' + d['inclusion_reason']} |")
    L += ["", "Large depth gaps (> 30 s), IST:", ""]
    for d in days:
        g = d["depth_cov"]["large_gaps"]
        if g:
            L.append(f"* **{d['date']}**: " + ", ".join(
                f"{qa.fmt_ist(a)}–{qa.fmt_ist(b)} ({(b - a) / 1e9:.0f} s)" for a, b in g))
    L.append("")

    L += ["## 2. Presence — every subscribed id produced rows", "",
          "| Date | Depth present | Feed present | Feed ids missing |", "|---|---|---|---|"]
    for d in days:
        p = d["presence"]
        miss = p["feed"]["missing"]
        L.append(f"| {d['date']} | {p['depth']['present']}/{p['depth']['subscribed']} | "
                 f"{p['feed']['present']}/{p['feed']['subscribed']} | "
                 f"{', '.join(map(str, miss[:8]))}{' …' if len(miss) > 8 else ''} |")
    L.append("")

    L += ["## 3. Book integrity (depth feed, options only, medians across the day's instruments "
          "unless stated)", "",
          "| Date | Two-sided | One-sided | Crossed | Locked | Non-monotone | Off-tick rows (sum) | "
          "Non-positive px (sum) | Suspect flag | Dup rows (sum) | Spread p50 (ticks) | Cadence p50/p95 ms |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in days:
        o = [i for i in d["instruments"] if i["instrument"] == "OPTIDX" and i["book"].get("snapshots")]
        b = [i["book"] for i in o]
        L.append(
            f"| {d['date']} | {_pct(qa.median_of(b, 'two_sided_pct'))} | "
            f"{_pct(qa.median_of(b, 'one_sided_pct'))} | {_pct(qa.median_of(b, 'crossed_pct'))} | "
            f"{_pct(qa.median_of(b, 'locked_pct'))} | {_pct(qa.median_of(b, 'nonmonotone_pct'))} | "
            f"{sum(x['off_tick'] for x in b)} | {sum(x['nonpositive_px'] for x in b)} | "
            f"{_pct(qa.median_of(b, 'suspect_flag_pct'))} | "
            f"{sum(i.get('depth_duplicate_rows', 0) for i in o)} | "
            f"{qa.median_of(b, 'spread_ticks_p50') or float('nan'):.1f} | "
            f"{qa.median_of([i['depth_cadence'] for i in o], 'p50_ms') or float('nan'):.0f}/"
            f"{qa.median_of([i['depth_cadence'] for i in o], 'p95_ms') or float('nan'):.0f} |")
    L.append("")

    L += ["## 4. Tape integrity (quote feed, the depth-channel options)", "",
          "| Date | Packets (sum) | Volume rewinds (sum) | Rewound units | Out-of-order | "
          "Exch lag p50 / p99 s | Clock skew events >1 s (sum) | Max skew ms |",
          "|---|---|---|---|---|---|---|---|"]
    for d in days:
        t = [i["tape"] for i in d["instruments"] if i["instrument"] == "OPTIDX" and i["tape"].get("packets")]
        mx = max((x["clock_skew_ms_max"] for x in t), default=0)
        L.append(f"| {d['date']} | {sum(x['packets'] for x in t):,} | "
                 f"{sum(x['rewinds'] for x in t)} | {sum(x['rewind_units'] for x in t):,} | "
                 f"{sum(x['out_of_order'] for x in t)} | "
                 f"{qa.median_of(t, 'exch_lag_s_p50') or float('nan'):.2f} / "
                 f"{qa.median_of(t, 'exch_lag_s_p99') or float('nan'):.2f} | "
                 f"{sum(x['clock_skew_events_gt1s'] for x in t)} | {mx:,.0f} |")
    L.append("")

    L += ["## 5. Cross-source agreement", "",
          "Depth feed (20-level socket) vs quote feed (Full packet 5-level block, separate socket), "
          "level-1 bid and ask, depth snapshot at most 1 s older than the quote packet.", "",
          "| Date | Compared | Exact match (median) | Within 2 ticks (median) | Worst instrument exact | "
          "ATM parity fwd − future mid p50 (pts) | |diff| p95 | Future spread p50 |",
          "|---|---|---|---|---|---|---|---|"]
    for d in days:
        ta = [i["touch_agreement"] for i in d["instruments"] if i["touch_agreement"].get("compared")]
        worst = min((x["exact_pct"] for x in ta), default=None)
        pv = d["parity_vs_future"]
        pvs = (f"{pv['diff_pts_p50']:+.1f} | {pv['absdiff_pts_p95']:.1f} | {pv['future_spread_pts_p50']:.1f}"
               if pv.get("available") else f"n/a ({pv.get('reason')}) | | ")
        L.append(f"| {d['date']} | {sum(x['compared'] for x in ta):,} | "
                 f"{_pct(qa.median_of(ta, 'exact_pct'))} | {_pct(qa.median_of(ta, 'within_2ticks_pct'))} | "
                 f"{_pct(worst)} | {pvs} |")
    L.append("")
    REPORT.write_text("\n".join(L) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", action="append")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    proto = load_protocol()
    days = sorted(set(proto.develop) | set(proto.holdout) | set(proto.excluded))
    if args.date:
        days = [date.fromisoformat(d) for d in args.date]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for d in days:
        print(f"{d} ...", flush=True)
        r = run_day(d, args.workers, proto.inclusion.max_gap_s)
        (OUT_DIR / f"{d}.json").write_text(json.dumps(r, indent=1, default=str))
        results.append(r)
    if not args.date:
        write_report(results)
        print(f"wrote {REPORT.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
