"""Go / no-go for strategy #2: does patient, far-from-fair liquidity get paid?

USAGE
    python scripts/rv_patient.py           # develop days, criteria from protocol_rv_patient.yaml

WHAT IT MEASURES
    For every option in the front-month depth band and every classified print:
    a resting bid at fair*(1-delta) and ask at fair*(1+delta), with fair the book
    mid ENTRY_DELAY before the print (the order was placed then, so nothing races).
    A SELL print at or below the bid fills the bid at the bid -- never better --
    and symmetrically for asks. After a fill, that side has no order until a new
    one is placed ENTRY_DELAY later, so one dislocation cannot fill repeatedly.

    markout(tau) = side * (mid(t+tau) - fill) / fill, in bp, against the pre-
    registered bar (round-trip cost, t >= 3 with minute-clustered errors, >= 50
    fills/day). Also reported, as a sensitivity only: the strict-fill variant (the
    print must trade *through* our price, not merely at it).

    Pure measurement on recorded books and the derived tape: no strategy object,
    no portfolio, no costs beyond the bar itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.analysis.holdout import load_protocol, log_config  # noqa: E402
from bnfmm.book.reconstruct import load_book  # noqa: E402
from bnfmm.book.tape import BUY, SELL, load_tape  # noqa: E402

ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"
CFG = _REPO_ROOT / "config" / "frozen" / "protocol_rv_patient.yaml"
NS = 1_000_000_000


def lookup(t_src, v, t, max_age_ns):
    j = np.searchsorted(t_src, t, side="right") - 1
    out = np.full(len(t), np.nan)
    ok = j >= 0
    ok[ok] &= (t[ok] - t_src[j[ok]]) <= max_age_ns
    out[ok] = v[j[ok]]
    return out


def option_ids(day, expiry):
    ids = {}
    for p in sorted((SESSIONS / f"date={day}").glob("*-contracts.json")):
        for c in json.loads(p.read_text())["channels"]["depth"]["contracts"]:
            if c["instrument"] == "OPTIDX" and c["expiry"] == str(expiry):
                ids[c["security_id"]] = c
    return ids


def fills_for(t, price, aggr, fair, delta, delay_ns, strict):
    """Indices of prints that fill a resting order, honouring the re-placement delay."""
    bid = fair * (1 - delta)
    ask = fair * (1 + delta)
    if strict:
        hit_b = (aggr == SELL) & (price < bid - 1e-9)
        hit_a = (aggr == BUY) & (price > ask + 1e-9)
    else:
        hit_b = (aggr == SELL) & (price <= bid + 1e-9)
        hit_a = (aggr == BUY) & (price >= ask - 1e-9)
    out = []
    for hit, side, level in ((hit_b, 1, bid), (hit_a, -1, ask)):
        ready = -np.inf
        for i in np.flatnonzero(hit & np.isfinite(level)):
            if t[i] >= ready:
                out.append((i, side, level[i]))
                ready = t[i] + delay_ns
    return out


def main() -> int:
    cfg = yaml.safe_load(CFG.read_text())
    proto = load_protocol(_REPO_ROOT / cfg["days_from"])
    n = log_config(proto, {"script": "rv_patient.py", "config": cfg,
                           "days": [str(d) for d in proto.develop]})
    print(f"rv protocol {proto.sha256[:12]}; config log now holds {n} distinct configurations")
    delay = int(cfg["entry_delay_s"] * NS)
    rows = {(s, d, tau): [] for s in (False, True) for d in cfg["delta_bp"] for tau in cfg["horizons_s"]}
    for day in proto.develop:
        expiry = proto.expiry_for(day)
        for sid in sorted(option_ids(day, expiry)):
            b = load_book(str(ROOT), day, sid, levels=1)
            if len(b) < 1000:
                continue
            tp = load_tape(str(ROOT), day, sid, bid=b.best_bid, ask=b.best_ask,
                           book_wall_ns=b.recv_wall_ns)
            if not len(tp):
                continue
            fair = lookup(b.recv_wall_ns, b.mid, tp.recv_wall_ns - delay, NS)
            mids_later = {tau: lookup(b.recv_wall_ns, b.mid, tp.recv_wall_ns + tau * NS, NS)
                          for tau in cfg["horizons_s"]}
            minute = (tp.recv_wall_ns // (60 * NS)).astype(np.int64)
            for strict in (False, True):
                for d in cfg["delta_bp"]:
                    fl = fills_for(tp.recv_wall_ns, tp.price, tp.aggressor, fair, d / 1e4, delay, strict)
                    for tau in cfg["horizons_s"]:
                        for i, side, px in fl:
                            m = mids_later[tau][i]
                            if np.isfinite(m):
                                rows[(strict, d, tau)].append(
                                    (side * (m - px) / px * 1e4, day.toordinal() * 10**7 + minute[i] % 10**7))
    ndays = len(proto.develop)
    bar = cfg["round_trip_cost_bp"]
    go = False
    print(f"\nMarkout of patient fills, bp (bar: > {bar} bp, t >= {cfg['min_t_stat']}, "
          f">= {cfg['min_fills_per_day']} fills/day)")
    for strict in (False, True):
        print("\n" + ("STRICT (trade-through) -- sensitivity only" if strict else "PRE-REGISTERED (at-or-through)"))
        print(f"   {'delta':>6} {'tau':>5} {'fills/day':>10} {'mean':>8} {'cl.se':>7} {'t vs bar':>9}")
        for d in cfg["delta_bp"]:
            for tau in cfg["horizons_s"]:
                a = np.array(rows[(strict, d, tau)])
                if len(a) < 2:
                    print(f"   {d:>6} {tau:>5} {len(a)/ndays:>10.1f}      --")
                    continue
                x, cl = a[:, 0], a[:, 1].astype(np.int64)
                mean = x.mean()
                _, inv = np.unique(cl, return_inverse=True)
                s = np.bincount(inv, weights=x - mean)
                se = np.sqrt((s * s).sum()) / len(x)
                t = (mean - bar) / se if se > 0 else np.nan
                ok = mean > bar and t >= cfg["min_t_stat"] and len(x) / ndays >= cfg["min_fills_per_day"]
                if not strict:
                    go |= ok
                print(f"   {d:>6} {tau:>5} {len(x)/ndays:>10.1f} {mean:>+8.2f} {se:>7.2f} {t:>+9.2f}"
                      f"{'   <- passes' if ok else ''}")
    print("\n=>", "GO" if go else "NO-GO", "(pre-registered rule)")
    out = _REPO_ROOT / "reports" / "rv"
    out.mkdir(parents=True, exist_ok=True)
    (out / "patient_go_no_go.json").write_text(json.dumps({"go": bool(go)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
