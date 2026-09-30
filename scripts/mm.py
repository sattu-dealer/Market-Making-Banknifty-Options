"""Run the portfolio market maker over captured days.

USAGE
    python scripts/mm.py                                  # develop days
    python scripts/mm.py --date 2026-08-27 --buffer 2
    python scripts/mm.py --date 2026-08-31 --unlock-holdout "REASON"

WHAT IT DOES
    For each day: reconstruct every option book in the depth band, imply the
    forward by put-call parity across the ladder, build each quoted option's fair
    value and delta *causally* (last observation carried forward only), difference
    the quote feed into a trade tape, and run ``sim.portfolio.run_portfolio`` --
    one inventory, one portfolio delta, orders that keep their queue place.

    Results are reported for both cost profiles (member, Dhan), under three
    closing marks (own microprice, fair value, liquidation at the touch), with the
    spread-capture / inventory split and the beta / R^2 of PnL on the forward --
    the tests that say whether the result is market making or a directional bet.

    The develop/holdout split in ``config/frozen/protocol.yaml`` is enforced, and
    every configuration is logged. Reads Parquet only; no order endpoint exists.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.analysis.holdout import load_protocol, log_config, require_access  # noqa: E402
from bnfmm.book.merge import load_merged_book  # noqa: E402
from bnfmm.book.reconstruct import align, load_book  # noqa: E402
from bnfmm.book.tape import load_tape  # noqa: E402
from bnfmm.data.qa import fmt_ist, session_bounds_ns  # noqa: E402
from bnfmm.fairvalue import black76  # noqa: E402
from bnfmm.fairvalue.microprice import microprice  # noqa: E402
from bnfmm.fairvalue.parity import discount, implied_forward, year_fraction  # noqa: E402
from bnfmm.sim.portfolio import Leg, PortfolioParams, run_portfolio, summarise  # noqa: E402

ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"
OUT = _REPO_ROOT / "reports" / "mm"
PROTOCOL = load_protocol()


def depth_options(day: date, expiry: date) -> dict[float, dict[str, dict]]:
    """strike -> {'CE': contract, 'PE': contract} for the front series in the depth band."""
    out: dict[float, dict[str, dict]] = {}
    for p in sorted((SESSIONS / f"date={day}").glob("*-contracts.json")):
        for c in json.loads(p.read_text())["channels"]["depth"]["contracts"]:
            if c["instrument"] == "OPTIDX" and c["expiry"] == str(expiry):
                out.setdefault(c["strike"], {})[c["option_type"]] = c
    return {k: v for k, v in out.items() if len(v) == 2}


def locf(src_t: np.ndarray, src_v: np.ndarray, dst_t: np.ndarray) -> np.ndarray:
    """Value of ``src`` at each ``dst`` time, last observation at or before it."""
    j = np.searchsorted(src_t, dst_t, side="right") - 1
    out = np.full(len(dst_t), np.nan)
    ok = j >= 0
    out[ok] = src_v[j[ok]]
    return out


def fair_value(own, other, is_call: bool, strike: float, fwd_t, fwd, expiry, mode: str):
    """Quote anchor on ``own``'s clock.

    ``own``    -- the option's own microprice.
    ``parity`` -- the other leg's microprice plus D(F - K): model-free, and far
                 tighter than a wide ITM book, because the OTM partner is liquid.
    ``blend``  -- precision-weighted: each estimate weighted by the inverse square
                 of the half-spread it is read from. A wide ITM book defers to its
                 tight partner; a tight OTM book mostly keeps its own price.
    """
    t = own.recv_wall_ns
    m_own = microprice(own.bid_px[:, :1], own.bid_qty[:, :1], own.ask_px[:, :1], own.ask_qty[:, :1])
    if mode == "own":
        return m_own
    m_oth_all = microprice(other.bid_px, other.bid_qty, other.ask_px, other.ask_qty)
    m_oth = locf(other.recv_wall_ns, m_oth_all, t)
    s_oth = locf(other.recv_wall_ns, other.spread, t)
    f = locf(fwd_t, fwd, t)
    d = discount(t, expiry)
    v_par = m_oth + (1.0 if is_call else -1.0) * d * (f - strike)
    if mode == "parity":
        return v_par
    s_own = own.spread
    with np.errstate(divide="ignore", invalid="ignore"):
        w_own = 1.0 / np.square(np.maximum(s_own, 1e-3) / 2)
        w_par = 1.0 / np.square(np.maximum(s_oth, 1e-3) / 2)
    out = (w_own * m_own + w_par * v_par) / (w_own + w_par)
    out = np.where(np.isfinite(v_par), out, m_own)
    return out


def leg_greeks(own, fv, is_call: bool, strike: float, fwd_t, fwd, expiry, every: int = 25):
    """Black-76 delta and vega (per vol point) from implied vol, every ``every`` snapshots, LOCF."""
    t = own.recv_wall_ns
    idx = np.arange(0, len(t), every)
    ts = t[idx]
    f = locf(fwd_t, fwd, ts)
    tt = year_fraction(ts, expiry)
    d = discount(ts, expiry)
    vol = black76.implied_vol(fv[idx], f, strike, tt, d, is_call)
    dl = black76.delta(f, strike, tt, vol, d, is_call)
    # No implied vol (value at intrinsic on a stale book): deep ITM is ~ +/-D,
    # far OTM is ~0. Never guess a middle value.
    itm = (f > strike) if is_call else (f < strike)
    fallback = np.where(itm, d * (1.0 if is_call else -1.0), 0.0)
    dl = np.where(np.isfinite(dl), dl, np.where(np.isfinite(f), fallback, np.nan))
    # A value with no implied vol is at intrinsic: no optionality, so no vega.
    vg = np.where(np.isfinite(vol), black76.vega(f, strike, tt, vol, d) / 100.0,
                  np.where(np.isfinite(f), 0.0, np.nan))
    return locf(ts, dl, t), locf(ts, vg, t)


def build_day(day: date, args) -> dict:
    expiry = PROTOCOL.expiry_for(day)
    opts = depth_options(day, expiry)
    ks = sorted(opts)
    t0 = time.time()
    loader = load_merged_book if args.book == "merged" else load_book
    l1 = {}
    for k in ks:
        for r in ("CE", "PE"):
            l1[opts[k][r]["security_id"]] = loader(str(ROOT), day, opts[k][r]["security_id"], levels=1)
    grid, idx = align(l1)
    fwd = implied_forward(grid, {k: (l1[opts[k]["CE"]["security_id"]], l1[opts[k]["PE"]["security_id"]])
                                 for k in ks}, idx, expiry)
    ok = np.isfinite(fwd.forward)
    fwd_t, fwd_v = grid[ok], fwd.forward[ok]
    atm = float(np.median(fwd_v))

    chosen = [k for k in ks if abs(k - atm) <= args.band * 100 + 1e-9]
    legs: list[Leg] = []
    for k in chosen:
        for r in ("CE", "PE"):
            if args.leg != "both" and r != args.leg.upper():
                continue
            c = opts[k][r]
            other = l1[opts[k]["PE" if r == "CE" else "CE"]["security_id"]]
            book = loader(str(ROOT), day, c["security_id"], levels=args.levels)
            tape = load_tape(str(ROOT), day, c["security_id"], bid=book.best_bid,
                             ask=book.best_ask, book_wall_ns=book.recv_wall_ns)
            fv = fair_value(book, other, r == "CE", k, fwd_t, fwd_v, expiry, args.fv)
            dl, vg = leg_greeks(book, fv, r == "CE", k, fwd_t, fwd_v, expiry)
            legs.append(Leg(security_id=c["security_id"], name=c["display_name"], strike=k,
                            is_call=r == "CE", tick=c["tick_size"], lot_size=c["lot_size"],
                            freeze_qty=c["freeze_qty"], book=book, tape=tape,
                            fair_value=fv, delta=dl, vega=vg))
    return {"day": day, "expiry": expiry, "legs": legs, "fwd_t": fwd_t, "fwd": fwd_v,
            "atm": atm, "load_s": time.time() - t0}


def dump_fills(path: Path, res, built) -> None:
    """Every fill with what is needed to study it: marks now and 300 s later, cost."""
    from bnfmm.sim.costs import CostModel, Product
    m = CostModel.for_profile("member")
    f = res.fills
    if not f:
        return
    leg = np.array([x.leg for x in f])
    t = np.array([x.t for x in f], dtype=np.int64)
    later = np.full(len(f), np.nan)
    for li in np.unique(leg):
        b = res.legs[li].book
        mic = microprice(b.bid_px[:, :1], b.bid_qty[:, :1], b.ask_px[:, :1], b.ask_qty[:, :1])
        j = np.searchsorted(b.recv_wall_ns, t[leg == li] + 300 * 10**9, side="right") - 1
        later[leg == li] = mic[np.clip(j, 0, len(b) - 1)]
    np.savez_compressed(
        path, t=t, leg=leg, side=np.array([1 if x.side == "bid" else -1 for x in f]),
        price=np.array([x.price for x in f]), qty=np.array([x.qty for x in f]),
        mark=np.array([x.mark_micro for x in f]), later300=later,
        cost=np.array([m.fill_cost(Product.OPTIONS, "buy" if x.side == "bid" else "sell",
                                   x.price, x.qty).total for x in f]),
        strike=np.array([res.legs[x.leg].strike for x in f]),
        is_call=np.array([res.legs[x.leg].is_call for x in f]),
        delta=np.array([x.delta for x in f]), fwd_atm=built["atm"],
        spread=np.array([res.legs[x.leg].book.spread[np.searchsorted(res.legs[x.leg].book.recv_wall_ns, x.t) - 1] for x in f]),
    )


def params_from(args) -> PortfolioParams:
    return PortfolioParams(
        size_lots=args.lots, buffer_ticks=args.buffer, cost_split=args.cost_split, improve=not args.join_only,
        max_behind_ticks=args.max_behind, leg_cap_lots=args.leg_cap,
        delta_limit_lots=args.delta_limit, delta_skew=args.delta_skew,
        leg_skew_ticks=args.leg_skew, vega_skew=args.vega_skew,
        vega_limit_lots=args.vega_limit, min_premium=args.min_premium,
        queue_at_price_ahead=not args.optimistic_queue, imbalance_pull=args.imbalance,
        min_spread_bp=args.min_spread_bp, close_only_last_s=args.close_only,
    )


def report(day: date, built: dict, s: dict, res) -> None:
    print(f"\n{'=' * 90}\n  {day}  expiry {built['expiry']} ({(built['expiry'] - day).days} DTE)  "
          f"fwd~{built['atm']:,.0f}  legs {len(built['legs'])}  (loaded in {built['load_s']:.0f}s)")
    if not s.get("fills"):
        print("  no fills;", s.get("stand_down"))
        return
    print(f"  fills {s['fills']:,}  units {s['units']:,}  orders {s['orders']:,}  "
          f"swept {s['swept_pct']:.1f}%  end |units| {s['end_abs_units']}")
    print(f"  gross   micro {s['gross_micro']:>11,.0f}   fv {s['gross_fair_value']:>11,.0f}   "
          f"liquidation {s['gross_liquidation']:>11,.0f}")
    print(f"  costs   member {s['costs']['member']:>10,.0f}   dhan {s['costs']['dhan']:>10,.0f}")
    print(f"  NET     member {s['net_member']:>10,.0f}   dhan {s['net_dhan']:>10,.0f}   "
          f"member, liquidated at close {s['net_member_liquidated']:>10,.0f}")
    print(f"  split   spread capture {s['spread_capture']:>10,.0f}   inventory {s['inventory_pnl']:>10,.0f}"
          f"   (capture vs fv {s['spread_capture_fv']:,.0f})")
    print(f"  per unit: capture {s['capture_per_unit']:+.4f}  member cost {s['cost_per_unit_member']:.4f}")
    hs = s["huang_stoll"]
    print("  Huang-Stoll eff/realised: " + "  ".join(
        f"{k} {v['effective']:+.3f}/{v['realised']:+.3f}" for k, v in hs.items()))
    if "r2" in s:
        print(f"  direction: beta {s['beta_lots']:+.2f} lots of underlying  R^2 {s['r2']:.3f}  "
              f"mean|delta| {s['mean_abs_delta_lots']:.2f} lots  max|delta| {s['max_abs_delta_lots']:.2f}"
              f"  mean vega Rs{s['mean_vega_rupees']:,.0f}/volpt  max|vega| {s['max_abs_vega_lots']:.1f} ATM lots")
    top = list(s["stand_down"].items())[:5]
    print("  stand-down: " + ", ".join(f"{k}={v:,}" for k, v in top))
    if getattr(report, "per_leg", False):
        print(f"    {'leg':>7} {'units':>8} {'buy%':>5} {'price':>8} {'eff':>7} {'real300':>8} "
              f"{'cost/u':>7} {'net/u':>7} {'net Rs':>10} {'end':>5}")
        for r in s["legs"]:
            net = r["capture"] + r["inventory"] - r["cost"]
            print(f"    {r['strike']:>6.0f}{r['cp']} {r['units']:>8,} {100*r['buys']/r['units']:>5.0f} "
                  f"{r['median_price']:>8.2f} {r['eff'] or 0:>+7.3f} {r['real300'] or 0:>+8.3f} "
                  f"{r['cost_per_unit']:>7.3f} {net/r['units']:>+7.3f} {net:>10,.0f} {r['end_units']:>5}")
    # Per-leg: who traded.
    per = {}
    for f in res.fills:
        a = per.setdefault(f.leg, [0, 0.0])
        a[0] += f.qty
        a[1] += (1 if f.side == "bid" else -1) * f.qty * (f.mark_micro - f.price)
    rows = sorted(per.items(), key=lambda x: -x[1][0])[:8]
    print("  busiest legs: " + "; ".join(
        f"{res.legs[i].strike:.0f}{'C' if res.legs[i].is_call else 'P'} {u:,}u cap{c:+,.0f} "
        f"end{res.positions[i].units:+d}" for i, (u, c) in rows))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", action="append")
    p.add_argument("--band", type=int, default=11, help="strikes either side of ATM to quote")
    p.add_argument("--leg", choices=("both", "ce", "pe"), default="both")
    p.add_argument("--levels", type=int, default=10)
    p.add_argument("--fv", choices=("own", "parity", "blend"), default="blend")
    p.add_argument("--book", choices=("depth", "merged"), default="depth",
                   help="merged = freshest of depth and quote feed, causally (book/merge.py)")
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--buffer", type=float, default=0.0, help="adverse buffer, ticks per side")
    p.add_argument("--cost-split", choices=("own", "symmetric"), default="own")
    p.add_argument("--join-only", action="store_true", help="never improve the touch")
    p.add_argument("--max-behind", type=float, default=0.0)
    p.add_argument("--leg-cap", type=int, default=5)
    p.add_argument("--delta-limit", type=float, default=5.0)
    p.add_argument("--delta-skew", type=float, default=0.5)
    p.add_argument("--leg-skew", type=float, default=0.5)
    p.add_argument("--vega-skew", type=float, default=0.0,
                   help="rupees shift on the ATM option per ATM-lot of portfolio vega")
    p.add_argument("--vega-limit", type=float, default=1e9, help="|portfolio vega| cap, ATM lots")
    p.add_argument("--min-premium", type=float, default=5.0)
    p.add_argument("--min-spread-bp", type=float, default=0.0,
                   help="stand a leg down while its spread/fair value is below this (bp)")
    p.add_argument("--min-dte", type=int, default=None,
                   help="skip days closer to expiry than this; default from config/instruments.yaml")
    p.add_argument("--optimistic-queue", action="store_true")
    p.add_argument("--imbalance", type=float, default=1.0,
                   help="pull the threatened side when |5-level imbalance| > this (1 = off)")
    p.add_argument("--close-only", type=float, default=0.0, help="seconds before close: reduce only")
    p.add_argument("--tag", default="", help="label for the JSON output")
    p.add_argument("--grid", default="",
                   help='JSON {"arg_name": [values,...]}: run the cartesian product on the '
                        'same loaded data; every combination is logged as its own configuration')
    p.add_argument("--per-leg", action="store_true", help="print the per-leg table")
    p.add_argument("--dump-fills", action="store_true", help="save every fill for analysis")
    p.add_argument("--unlock-holdout", metavar="REASON")
    args = p.parse_args(argv)

    days = list(PROTOCOL.develop) if not args.date else [date.fromisoformat(d) for d in args.date]
    # The quoting study's pre-registered scope (config/instruments.yaml, written
    # 2026-08-23 before any data existed): no quoting within min_days_to_expiry.
    if args.min_dte is None:
        import yaml
        args.min_dte = int(yaml.safe_load((_REPO_ROOT / "config" / "instruments.yaml").read_text())
                           ["options"]["min_days_to_expiry"])
    skipped = [d for d in days if (PROTOCOL.expiry_for(d) - d).days < args.min_dte]
    if skipped:
        print(f"skipping {[str(d) for d in skipped]}: fewer than {args.min_dte} days to expiry "
              f"(config/instruments.yaml options.min_days_to_expiry)")
    days = [d for d in days if d not in skipped]
    try:
        days = require_access(PROTOCOL, days, unlock_reason=args.unlock_holdout,
                              argv=sys.argv if argv is None else ["mm.py", *argv])
    except Exception as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}") from None
    import itertools
    report.per_leg = args.per_leg
    grid = json.loads(args.grid) if args.grid else {}
    combos = [dict(zip(grid, vals)) for vals in itertools.product(*grid.values())] or [{}]
    variants = []
    for over in combos:
        a = argparse.Namespace(**{**vars(args), **over})
        config = {k: v for k, v in sorted(vars(a).items())
                  if k not in ("date", "unlock_holdout", "tag", "grid", "per_leg", "dump_fills")}
        n = log_config(PROTOCOL, {"script": "mm.py", "config": config,
                                  "days": [str(d) for d in days]})
        variants.append((over, a, config, n))
    print(f"{len(variants)} configuration(s) logged; {variants[-1][3]} distinct tried so far")

    OUT.mkdir(parents=True, exist_ok=True)
    results = {i: [] for i in range(len(variants))}
    for d in days:
        built = build_day(d, args)
        for vi, (over, a, config, n) in enumerate(variants):
            t0 = time.time()
            res = run_portfolio(built["legs"], params_from(a), session_end_ns=session_bounds_ns(d)[1])
            s = summarise(res, forward_t=built["fwd_t"], forward=built["fwd"])
            s["sim_s"] = time.time() - t0
            s["day"] = str(d)
            if over:
                print(f"\n>>> variant {vi}: {over}")
            report(d, built, s, res)
            results[vi].append(s)
            if args.dump_fills:
                dump_fills(OUT / f"fills-{(args.tag or 'run')}-v{vi}-{d}.npz", res, built)
        del built
    print(f"\n{'#' * 90}\n  SUMMARY across {len(days)} day(s)")
    print(f"  {'variant':<44} {'net mem':>10} {'liquid.':>10} {'net dhan':>11} {'capture':>10} "
          f"{'invent.':>10} {'cost m':>10} {'units':>9}")
    for vi, (over, a, config, n) in enumerate(variants):
        rs = [x for x in results[vi] if x.get("fills")]
        tot = lambda k: sum(x.get(k, 0) for x in rs)  # noqa: E731
        cm = sum(x["costs"]["member"] for x in rs)
        label = json.dumps(over) if over else "(base)"
        print(f"  {label[:44]:<44} {tot('net_member'):>10,.0f} {tot('net_member_liquidated'):>10,.0f} "
              f"{tot('net_dhan'):>11,.0f} {tot('spread_capture'):>10,.0f} {tot('inventory_pnl'):>10,.0f} "
              f"{cm:>10,.0f} {tot('units'):>9,}")
        tag = (args.tag or "run") + (f"-v{vi}" if len(variants) > 1 else "")
        (OUT / f"{tag}.json").write_text(json.dumps(
            {"config": config, "params": asdict(params_from(a)), "days": results[vi]},
            indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
