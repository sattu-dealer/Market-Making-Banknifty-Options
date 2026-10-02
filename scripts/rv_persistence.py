"""Go / no-go for strategy #1: do smile mispricings outlive our latency?

USAGE
    python scripts/rv_persistence.py            # the develop days of protocol_rv.yaml

WHAT IT MEASURES (criteria pre-registered in config/frozen/protocol_rv.yaml)
    Every second of the continuous session, on the front-month depth band:

    1. The parity forward F (fairvalue.parity), carried forward causally.
    2. Implied vol of each strike's out-of-the-money option from its mid; a
       quadratic smile in ln(K/F) weighted by (vega / half-spread)^2.
    3. Each option's leave-one-out smile value, and its residual
       r = mid - smile_value (positive = rich).

    Then, pooled over options:
      * persistence: autocorrelation of r at 1 / 5 / 30 / 300 s lags;
      * exploitability: for |r| > half the book spread, the correction toward the
        smile  -sign(r) * (mid(t+d+tau) - mid(t+d)) / mid(t)  in bp, for entry
        delay d = 0 and d = 1 s, tau in 5 / 30 / 300 s.

    Pure measurement on recorded books: no orders, no fills, no strategy.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.analysis.holdout import load_protocol, log_config  # noqa: E402
from bnfmm.book.reconstruct import align, load_book  # noqa: E402
from bnfmm.data.qa import session_bounds_ns  # noqa: E402
from bnfmm.fairvalue import black76  # noqa: E402
from bnfmm.fairvalue.parity import discount, implied_forward, year_fraction  # noqa: E402
from bnfmm.fairvalue.smile import fit_smile_loo  # noqa: E402

ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"
PROTO_PATH = _REPO_ROOT / "config" / "frozen" / "protocol_rv.yaml"
OUT = _REPO_ROOT / "reports" / "rv"
NS = 1_000_000_000


def options(day: date, expiry: date) -> dict[float, dict[str, dict]]:
    out: dict[float, dict[str, dict]] = {}
    for p in sorted((SESSIONS / f"date={day}").glob("*-contracts.json")):
        for c in json.loads(p.read_text())["channels"]["depth"]["contracts"]:
            if c["instrument"] == "OPTIDX" and c["expiry"] == str(expiry):
                out.setdefault(c["strike"], {})[c["option_type"]] = c
    return {k: v for k, v in sorted(out.items()) if len(v) == 2}


def at(src_t, src_v, dst_t, max_age_ns=NS):
    """LOCF of src onto dst, NaN where the last observation is older than max_age."""
    j = np.searchsorted(src_t, dst_t, side="right") - 1
    out = np.full(len(dst_t), np.nan)
    ok = j >= 0
    ok[ok] &= (dst_t[ok] - src_t[j[ok]]) <= max_age_ns
    out[ok] = src_v[j[ok]]
    return out


def residuals(day: date, expiry: date, degree: int = 2) -> dict:
    opts = options(day, expiry)
    ks = np.array(sorted(opts))
    books = {r: [load_book(str(ROOT), day, opts[k][r]["security_id"], levels=1) for k in ks]
             for r in ("CE", "PE")}
    l1 = {b.security_id: b for r in books for b in books[r]}
    grid, idx = align(l1)
    fwd = implied_forward(grid, {k: (books["CE"][i], books["PE"][i]) for i, k in enumerate(ks)},
                          idx, expiry)
    lo, hi = session_bounds_ns(day)
    g = np.arange(lo, hi + 1, NS, dtype=np.int64)
    okf = np.isfinite(fwd.forward)
    F = at(grid[okf], fwd.forward[okf], g)
    T = year_fraction(g, expiry)
    D = discount(g, expiry)

    mid = {r: np.stack([at(b.recv_wall_ns, b.mid, g) for b in books[r]], 1) for r in books}
    spr = {r: np.stack([at(b.recv_wall_ns, b.spread, g) for b in books[r]], 1) for r in books}
    Kg = np.broadcast_to(ks, mid["CE"].shape)
    Fg, Tg, Dg = (np.broadcast_to(a[:, None], Kg.shape) for a in (F, T, D))
    otm_call = Kg >= Fg
    m_otm = np.where(otm_call, mid["CE"], mid["PE"])
    s_otm = np.where(otm_call, spr["CE"], spr["PE"])
    iv = black76.implied_vol(m_otm, Fg, Kg, Tg, Dg, otm_call)
    vega = black76.vega(Fg, Kg, Tg, np.where(np.isfinite(iv), iv, 0.2), Dg)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.square(vega / np.maximum(s_otm / 2, 1e-6))
        x = np.log(Kg / Fg)
    _, iv_loo = fit_smile_loo(x, iv, w, degree=degree)
    out = {"t": g, "F": F, "strikes": ks}
    for r, is_call in (("CE", True), ("PE", False)):
        val = black76.price(Fg, Kg, Tg, iv_loo, Dg, is_call)
        out[r] = {"mid": mid[r], "spread": spr[r], "res": mid[r] - val}
    return out


def autocorr(x: np.ndarray, lag: int) -> float:
    a, b = x[:-lag], x[lag:]
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 30:
        return np.nan
    a, b = a[ok] - a[ok].mean(), b[ok] - b[ok].mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 0 else np.nan


def main() -> int:
    proto = load_protocol(PROTO_PATH)
    gng = __import__("yaml").safe_load(PROTO_PATH.read_text())["go_no_go"]
    n = log_config(proto, {"script": "rv_persistence.py", "config": {"degree": 2, **gng},
                           "days": [str(d) for d in proto.develop]})
    print(f"protocol {proto.sha256[:12]}; {n} distinct configurations logged under it")
    lags = [1, 5, 30, 300]
    taus = gng["horizons_s"]
    delays = [0, int(gng["entry_delay_s"])]
    ac = {L: [] for L in lags}
    corr = {(d, tau): [] for d in delays for tau in taus}
    mag = []
    for day in proto.develop:
        r = residuals(day, proto.expiry_for(day))
        for leg in ("CE", "PE"):
            res, mid, spr = r[leg]["res"], r[leg]["mid"], r[leg]["spread"]
            for j in range(res.shape[1]):
                x = res[:, j]
                if np.isfinite(x).sum() < 1000:
                    continue
                w = np.isfinite(x).sum()
                for L in lags:
                    a = autocorr(x, L)
                    if np.isfinite(a):
                        ac[L].append((a, w))
                with np.errstate(invalid="ignore", divide="ignore"):
                    rbp = x / mid[:, j] * 1e4
                    hbp = spr[:, j] / 2 / mid[:, j] * 1e4
                okm = np.isfinite(rbp) & np.isfinite(hbp)
                mag.append((np.median(np.abs(rbp[okm])), np.median(hbp[okm]), okm.sum()))
                sig = okm & (np.abs(x) > spr[:, j] / 2)
                idx = np.flatnonzero(sig)
                n_t = len(x)
                for d in delays:
                    for tau in taus:
                        a_, b_ = idx + d, idx + d + tau
                        keep = b_ < n_t
                        i0, ia, ib = idx[keep], a_[keep], b_[keep]
                        c = -np.sign(x[i0]) * (mid[ib, j] - mid[ia, j]) / mid[i0, j] * 1e4
                        corr[(d, tau)].append(c[np.isfinite(c)])
    print("\nResidual size (median over options): |residual| %.1f bp vs half-spread %.1f bp"
          % (np.average([m[0] for m in mag], weights=[m[2] for m in mag]),
             np.average([m[1] for m in mag], weights=[m[2] for m in mag])))
    print("\nPersistence -- autocorrelation of the residual (observation-weighted mean):")
    acm = {}
    for L in lags:
        v = np.array(ac[L])
        acm[L] = float(np.average(v[:, 0], weights=v[:, 1]))
        print(f"   lag {L:>4} s : {acm[L]:+.3f}")
    print("\nExploitability -- correction toward the smile when |residual| > half-spread (bp):")
    best_delayed = -np.inf
    for d in delays:
        for tau in taus:
            c = np.concatenate(corr[(d, tau)])
            m, se = c.mean(), c.std(ddof=1) / np.sqrt(len(c))
            if d == delays[1]:
                best_delayed = max(best_delayed, m)
            print(f"   entry delay {d} s, horizon {tau:>3} s : {m:+7.2f} bp  (se {se:.2f}, n {len(c):,})")
    c1 = acm[5] >= gng["min_autocorr_5s"]
    c2 = best_delayed > gng["per_side_cost_bp"]
    print(f"\nGO/NO-GO (pre-registered): persistence {acm[5]:.3f} >= {gng['min_autocorr_5s']} -> "
          f"{'PASS' if c1 else 'FAIL'};  best delayed correction {best_delayed:+.2f} bp > "
          f"{gng['per_side_cost_bp']} bp -> {'PASS' if c2 else 'FAIL'}")
    print("=>", "GO" if (c1 and c2) else "NO-GO")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "go_no_go.json").write_text(json.dumps({
        "autocorr": acm, "go": bool(c1 and c2),
        "correction_bp": {f"d{d}_tau{tau}": float(np.concatenate(corr[(d, tau)]).mean())
                          for d in delays for tau in taus}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
