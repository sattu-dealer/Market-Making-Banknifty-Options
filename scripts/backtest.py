"""Run the market maker over captured days and report the result.

USAGE
    python scripts/backtest.py                        # both target days, ATM strike
    python scripts/backtest.py --date 2026-08-24
    python scripts/backtest.py --lots 20 --half-spread 3
    python scripts/backtest.py --strikes 5            # ATM +/- 2 strikes
    python scripts/backtest.py --json out.json
    python scripts/backtest.py --date 2026-08-31 --unlock-holdout "REASON"

WHICH DAYS
    The develop / holdout split is frozen in ``config/frozen/protocol.yaml`` and
    enforced by ``bnfmm.analysis.holdout``: with no ``--date`` only the develop
    days run, a holdout day refuses to load without ``--unlock-holdout`` (which
    is logged to ``reports/holdout_log.md``), and every configuration run is
    appended to ``reports/config_log.jsonl`` so results can be quoted "from N
    configurations". BRIEFING §18.5.

WHAT IT DOES
    For each requested day: reconstruct the option books, imply the forward from
    put-call parity across the whole captured strike ladder, difference the quote
    feed's volume counter into a trade tape, then quote two-sidedly against the
    real book and settle fills with the snapshot-to-snapshot depletion model.

    Results are reported paired -- zero brokerage (exchange member) and Dhan's
    Rs 20 flat per order -- because the flat fee is what decides viability at small
    size, and reporting only one of them would be misleading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.analysis.holdout import load_protocol, log_config, require_access  # noqa: E402
from bnfmm.book.reconstruct import align, load_book  # noqa: E402
from bnfmm.book.tape import load_tape  # noqa: E402
from bnfmm.fairvalue.microprice import microprice  # noqa: E402
from bnfmm.fairvalue.parity import discount, implied_forward  # noqa: E402
from bnfmm.sim.backtest import apply_costs, decompose_pnl, huang_stoll, run_day  # noqa: E402
from bnfmm.strategy.quoter import QuoteParams  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"

#: The frozen split, and the monthly expiry each day quotes into. BANKNIFTY has
#: no weeklies, so the front series is the only one quoted.
PROTOCOL = load_protocol()


def ist(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, IST).strftime("%H:%M:%S")


def load_contracts(day: date) -> dict:
    """The frozen instrument mapping for a captured day.

    Reads the immutable sidecar written by ``scripts/snapshot_contracts.py``, not
    the instrument master. The master is overwritten in place on every refresh and
    the vendor prunes expired contracts from it, so by now it can no longer resolve
    the August series at all -- the sidecar is the only thing that still can.
    """
    paths = sorted((SESSIONS / f"date={day.isoformat()}").glob("*-contracts.json"))
    if not paths:
        raise SystemExit(f"no contracts sidecar for {day}; run scripts/snapshot_contracts.py")
    # The last session of a day is the one that ran to the close.
    return json.loads(paths[-1].read_text())


def strike_map(sidecar: dict) -> dict[float, dict[str, int]]:
    out: dict[float, dict[str, int]] = {}
    for c in sidecar["channels"]["depth"]["contracts"]:
        if c["instrument"] != "OPTIDX":
            continue
        out.setdefault(c["strike"], {})[c["option_type"].lower()] = c["security_id"]
    return {k: v for k, v in out.items() if "ce" in v and "pe" in v}


def contract_spec(sidecar: dict, security_id: int) -> dict:
    for c in sidecar["channels"]["depth"]["contracts"]:
        if c["security_id"] == security_id:
            return c
    raise KeyError(security_id)


def parity_value(
    book, other, leg: str, strike: float, grid, forward, expiry,
) -> np.ndarray:
    """This leg's fair value from the *other* leg plus the consensus forward.

    Parity is an identity for European options, so given the cross-strike consensus
    forward ``F`` and the other leg's microprice, this leg's value follows with no
    volatility model at all:

        C = P + D*(F - K)          P = C - D*(F - K)

    Subtracting this leg's own microprice gives exactly ``D * (F - F_K)`` -- the
    discounted amount by which *this strike's* synthetic forward disagrees with the
    ladder's precision-weighted consensus. That difference is the entire signal: it
    says this strike's call is cheap relative to its put given what all 23 strikes
    jointly imply, which is a genuine cross-sectional relative-value view and not a
    repackaging of the option's own quote.

    Returned on ``book``'s own clock. NaN wherever the other leg or the forward was
    not yet live, which stands the quoter down rather than guessing.
    """
    n = len(book)
    out = np.full(n, np.nan)

    # The other leg, last-observation-carried-forward onto our clock. A strategy at
    # time T could not have seen a snapshot stamped after T, so `side="right" - 1`.
    j = np.searchsorted(other.recv_wall_ns, book.recv_wall_ns, side="right") - 1
    g = np.searchsorted(grid, book.recv_wall_ns, side="right") - 1
    live = (j >= 0) & (g >= 0)
    if not live.any():
        return out

    om = microprice(other.bid_px, other.bid_qty, other.ask_px, other.ask_qty)
    o = om[j[live]]
    f = forward[g[live]]
    d = discount(book.recv_wall_ns[live], expiry)

    sign = 1.0 if leg == "ce" else -1.0
    out[live] = o + sign * d * (f - strike)
    return out


def loo_forward(grid, l1, strikes, ks, drop: float, idx, expiry) -> np.ndarray:
    """The consensus forward rebuilt with one strike's own pair left out.

    The default forward is a precision-weighted average over all 23 strikes,
    including the pair being quoted. That is not circular -- the weight algebra
    makes it *conservative*, because

        adj = fair_value - micro = D * (F - F_K)

    and F is a weighted mean containing F_K with weight w_K, so

        adj = D * (1 - w_K) * (F_rest - F_K)

    Including yourself scales the signal by (1 - w_K) < 1: it can shrink the
    quote adjustment toward the option's own microprice, never inflate it beyond
    what the other strikes say. But the near-the-money pair carries the largest
    precision weight of the whole ladder, so w_K is not small there, and asserting
    "conservative" from algebra alone is exactly the kind of claim that should be
    measured. This rebuilds F from the other 22 strikes so the two can be compared
    directly.
    """
    return implied_forward(
        grid,
        {k: (l1[strikes[k]["ce"]], l1[strikes[k]["pe"]]) for k in ks if k != drop},
        idx,
        expiry,
    ).forward


def run(day: date, args) -> dict:
    expiry = PROTOCOL.expiry_for(day)
    sidecar = load_contracts(day)
    strikes = strike_map(sidecar)
    ks = sorted(strikes)

    print(f"\n{'=' * 78}")
    print(f"  {day}  (expiry {expiry}, {(expiry - day).days} DTE)   "
          f"{len(ks)} complete CE/PE strikes {ks[0]:.0f}..{ks[-1]:.0f}")
    print("=" * 78)

    # --- fair value: parity across the whole ladder, level 1 only -------------
    print("  reconstructing books for parity (level 1)...", flush=True)
    l1 = {}
    for k in ks:
        for leg in ("ce", "pe"):
            l1[strikes[k][leg]] = load_book(str(ROOT), day, strikes[k][leg], levels=1)
    grid, idx = align(l1)
    fwd = implied_forward(
        grid, {k: (l1[strikes[k]["ce"]], l1[strikes[k]["pe"]]) for k in ks}, idx, expiry
    )
    fs = fwd.summary()
    print(f"  parity forward: {fs['resolved']}/{fs['samples']} resolved "
          f"({fs['resolved_pct']:.1f}%), median {int(fs['median_strikes'])} strikes, "
          f"dispersion {fs['median_dispersion']:.2f}, band {fs['median_width']:.2f}")
    print(f"  strict-intersection empty at {fs['arb_violation_pct']:.1f}% of samples "
          f"(cross-strike dispersion exceeds spread -- a staleness measure, not arbitrage)")

    ok = np.isfinite(fwd.forward)
    atm_ref = float(np.median(fwd.forward[ok]))
    if args.strike:
        missing = [k for k in args.strike if k not in strikes]
        if missing:
            raise SystemExit(f"no complete CE/PE pair for strike(s) {missing}; have {ks}")
        chosen = sorted(args.strike)
    else:
        # Default: the strikes nearest the day's median forward.
        chosen = sorted(sorted(ks, key=lambda k: abs(k - atm_ref))[: args.strikes])
    legs = ("ce", "pe") if args.leg == "both" else (args.leg,)
    print(f"  day median forward {atm_ref:,.2f}; quoting {len(chosen)} strike(s): "
          f"{', '.join(f'{k:.0f}' for k in chosen)} [{'/'.join(l.upper() for l in legs)}]")

    params = QuoteParams(
        half_spread_ticks=args.half_spread,
        size_lots=args.lots,
        max_position_lots=args.max_position,
        skew_ticks_per_lot=args.skew,
    )

    # Load each quoted contract's book and tape once; the sweep below re-runs the
    # strategy over the same arrays rather than re-reading 60 MB per parameter.
    loaded = []
    for k in chosen:
        for leg in legs:
            sid = strikes[k][leg]
            spec = contract_spec(sidecar, sid)
            # Full-depth book for the contract we actually quote: the queue model
            # needs every level, not just the touch.
            book = load_book(str(ROOT), day, sid)
            tape = load_tape(
                str(ROOT), day, sid,
                bid=book.best_bid, ask=book.best_ask, book_wall_ns=book.recv_wall_ns,
            )
            # Two anchors are supported so the report can show what the parity signal
            # is worth rather than assert it:
            #   micro  -- the option's own microprice alone (self-contained baseline)
            #   parity -- the other leg plus the cross-strike consensus forward
            fv = None
            if args.anchor == "parity":
                other = l1[strikes[k]["pe" if leg == "ce" else "ce"]]
                # The consensus forward normally includes *this* strike's own pair.
                # `--exclude-self` rebuilds it from the other 22 strikes only, which
                # is the check that the edge is cross-sectional rather than the
                # contract talking to itself. See `parity_value` for why including
                # self is conservative rather than circular.
                f = fwd.forward
                if args.exclude_self:
                    f = loo_forward(grid, l1, strikes, ks, k, idx, expiry)
                fv = parity_value(book, other, leg, k, grid, f, expiry)
            loaded.append((k, leg, sid, spec, book, tape, fv))
            print(f"    loaded {k:.0f} {leg.upper()}: {len(book):,} snapshots, "
                  f"{len(tape):,} prints, median spread "
                  f"{np.nanmedian((book.best_ask - book.best_bid) / spec['tick_size']):.0f}"
                  f" ticks, median mid "
                  f"{np.nanmedian(book.mid):.2f}", flush=True)

    sweeps = args.sweep or [args.half_spread]
    all_runs = []
    for hs in sweeps:
        p = QuoteParams(
            half_spread_ticks=hs,
            size_lots=args.lots,
            max_position_lots=args.max_position,
            skew_ticks_per_lot=args.skew,
        )
        per_contract = []
        for k, leg, sid, spec, book, tape, fv in loaded:
            res = run_day(
                book=book, tape=tape, params=p,
                tick=spec["tick_size"], lot_size=spec["lot_size"],
                freeze_qty=spec["freeze_qty"],
                fair_value=fv,
                queue_at_price_ahead=not args.optimistic_queue,
            )
            hsd = huang_stoll(res, book)
            dec = decompose_pnl(res)
            costs = {pr: apply_costs(res, profile=pr) for pr in ("member", "dhan")}
            per_contract.append(
                {"strike": k, "leg": leg.upper(), "security_id": sid,
                 "spec": spec, "result": res, "hs": hsd, "costs": costs,
                 "decomp": dec,
                 "tape_units": int(tape.quantity.sum()), "tape_trades": len(tape)}
            )
            if len(sweeps) == 1:
                _print_contract(per_contract[-1])
        _print_totals(day, per_contract, p)
        all_runs.append({"half_spread_ticks": hs, "contracts": per_contract})

    if len(sweeps) > 1:
        _print_sweep(day, all_runs)

    return {"day": day.isoformat(), "expiry": expiry.isoformat(),
            "forward": fs, "runs": all_runs, "params": params}


def _print_contract(c: dict) -> None:
    r = c["result"]
    hs = c["hs"]
    print(f"\n  --- {c['strike']:.0f} {c['leg']} (id {c['security_id']}) ---")
    print(f"      snapshots {r.snapshots:,}  quoted {r.quoted_snapshots:,} "
          f"({100 * r.quoted_snapshots / max(r.snapshots, 1):.1f}%)  "
          f"fills {len(r.fills):,} ({r.filled_units:,} units)")
    if r.stand_down:
        top = sorted(r.stand_down.items(), key=lambda x: -x[1])[:4]
        print(f"      stood down: {', '.join(f'{k}={v:,}' for k, v in top)}")
    print(f"      market volume {c['tape_units']:,} units in {c['tape_trades']:,} prints; "
          f"we captured {100 * r.filled_units / max(c['tape_units'], 1):.3f}%")
    print(f"      position: max |{r.max_abs_position}| units, ended {r.final_units} "
          f"@ mark {r.final_mark:.2f}"
          + (f" (mark {r.final_mark_stale_ns / 1e9:.1f}s stale at the close)"
             if r.final_mark_stale_ns > 1_000_000_000 and r.final_units else ""))
    print(f"      PnL  realised {r.realised:>12,.2f}   unrealised {r.unrealised:>12,.2f}"
          f"   gross {r.gross_pnl:>12,.2f}")
    d = c.get("decomp") or {}
    if d.get("measurable"):
        print(f"      decomposition: spread capture {d['spread_capture']:>12,.2f}   "
              f"inventory {d['inventory_pnl']:>12,.2f}   "
              f"({d['inventory_share_pct']:+.1f}% of gross from inventory)")
    for p in ("member", "dhan"):
        cc = c["costs"][p]
        print(f"      {p:>6}: costs {cc['costs']:>11,.2f}  net {cc['net_pnl']:>12,.2f}"
              f"   ({cc['cost_per_fill']:.2f}/fill)")
    if hs.get("measurable"):
        print(f"      Huang-Stoll (per unit): effective {hs['effective_half_spread']:+.4f}  "
              f"realised {hs['realised_half_spread']:+.4f}  "
              f"adverse {hs['adverse_selection']:+.4f}")
        curve = hs.get("adverse_by_horizon") or {}
        if curve:
            print("      adverse selection by horizon: "
                  + "  ".join(f"{k}={v:+.4f}" for k, v in curve.items()))
    st = r.fill_stats
    print(f"      fill model: eligible {st.get('eligible_units', 0):,} units, "
          f"filled {st.get('filled_units', 0):,} "
          f"({st.get('fill_ratio_pct', 0):.2f}%), sweeps {st.get('swept_fills', 0):,}, "
          f"stale intervals {st.get('intervals_stale', 0):,}")


def _print_totals(day: date, per: list[dict], params: QuoteParams) -> None:
    print(f"\n  {'-' * 74}")
    print(f"  DAY TOTAL {day}   (half-spread {params.half_spread_ticks} ticks, "
          f"{params.size_lots} lot/side, cap {params.max_position_lots} lots)")
    gross = sum(c["result"].gross_pnl for c in per)
    fills = sum(len(c["result"].fills) for c in per)
    units = sum(c["result"].filled_units for c in per)
    print(f"    contracts {len(per)}   fills {fills:,}   units {units:,}")
    print(f"    gross PnL {gross:>14,.2f}")
    for p in ("member", "dhan"):
        cost = sum(c["costs"][p]["costs"] for c in per)
        print(f"    {p:>6}: costs {cost:>12,.2f}   net {gross - cost:>14,.2f}")
    print(f"  {'-' * 74}")


def _print_sweep(day: date, runs: list[dict]) -> None:
    """One line per half-spread, so the turning point is visible at a glance."""
    print(f"\n  HALF-SPREAD SWEEP {day}")
    print(f"    {'ticks':>6} {'fills':>9} {'units':>11} {'gross':>13} "
          f"{'net member':>13} {'net dhan':>14} {'eff/unit':>9} {'adverse':>9}")
    for r in runs:
        per = r["contracts"]
        gross = sum(c["result"].gross_pnl for c in per)
        fills = sum(len(c["result"].fills) for c in per)
        units = sum(c["result"].filled_units for c in per)
        cm = sum(c["costs"]["member"]["costs"] for c in per)
        cd = sum(c["costs"]["dhan"]["costs"] for c in per)
        # Unit-weighted across contracts, so a contract that barely traded does not
        # get the same say as one that carried the day's volume.
        w = [(c["hs"], c["result"].filled_units) for c in per if c["hs"].get("measurable")]
        tot = sum(u for _, u in w) or 1
        eff = sum(h["effective_half_spread"] * u for h, u in w) / tot
        adv = sum(h["adverse_selection"] * u for h, u in w) / tot
        print(f"    {r['half_spread_ticks']:>6.1f} {fills:>9,} {units:>11,} "
              f"{gross:>13,.0f} {gross - cm:>13,.0f} {gross - cd:>14,.0f} "
              f"{eff:>+9.4f} {adv:>+9.4f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", action="append", help="trading date (repeatable)")
    p.add_argument("--strikes", type=int, default=1, help="how many strikes to quote")
    p.add_argument("--strike", type=float, action="append",
                   help="quote this exact strike (repeatable); overrides --strikes")
    p.add_argument("--leg", choices=("both", "ce", "pe"), default="both")
    p.add_argument("--lots", type=int, default=1, help="lots per side")
    p.add_argument("--half-spread", type=float, default=2.0, help="ticks either side")
    p.add_argument("--sweep", type=float, action="append",
                   help="repeat to sweep half-spreads instead of a single run")
    p.add_argument("--max-position", type=int, default=10, help="inventory cap in lots")
    p.add_argument("--skew", type=float, default=0.5, help="ticks of skew per lot held")
    p.add_argument("--anchor", choices=("micro", "parity"), default="micro",
                   help="fair value: option's own microprice, or parity-anchored")
    p.add_argument("--optimistic-queue", action="store_true",
                   help="assume we join the FRONT of our price level (sensitivity)")
    p.add_argument("--exclude-self", action="store_true",
                   help="build the parity forward without the quoted strike's own pair")
    p.add_argument("--json", help="write the full result to this path")
    p.add_argument("--unlock-holdout", metavar="REASON",
                   help="permit holdout days; the reason is logged to reports/holdout_log.md")
    args = p.parse_args(argv)

    days = list(PROTOCOL.develop) if not args.date else [date.fromisoformat(d) for d in args.date]
    try:
        days = require_access(PROTOCOL, days, unlock_reason=args.unlock_holdout,
                              argv=sys.argv if argv is None else ["backtest.py", *argv])
    except Exception as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}") from None

    config = {k: v for k, v in sorted(vars(args).items())
              if k not in ("date", "json", "unlock_holdout")}
    n = log_config(PROTOCOL, {"script": "backtest.py", "config": config,
                              "days": [str(d) for d in days]})
    print(f"configuration logged; {n} distinct configuration(s) tried so far")

    out = []
    for d in days:
        out.append(run(d, args))

    if args.json:
        Path(args.json).write_text(json.dumps(_jsonable(out), indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


def _jsonable(obj):
    """Strip the dataclasses down to plain JSON, dropping the bulky fill lists."""
    out = []
    for day in obj:
        runs = []
        for r in day["runs"]:
            cs = []
            for c in r["contracts"]:
                res = c["result"]
                cs.append({
                    "strike": c["strike"], "leg": c["leg"],
                    "security_id": c["security_id"],
                    "snapshots": res.snapshots,
                    "quoted_snapshots": res.quoted_snapshots,
                    "fills": len(res.fills), "filled_units": res.filled_units,
                    "realised": res.realised, "unrealised": res.unrealised,
                    "gross_pnl": res.gross_pnl, "final_units": res.final_units,
                    "final_mark": res.final_mark,
                    "max_abs_position": res.max_abs_position,
                    "stand_down": res.stand_down, "fill_stats": res.fill_stats,
                    "huang_stoll": c["hs"], "costs": c["costs"],
                    "tape_units": c["tape_units"], "tape_trades": c["tape_trades"],
                })
            runs.append({"half_spread_ticks": r["half_spread_ticks"], "contracts": cs})
        out.append({"day": day["day"], "expiry": day["expiry"],
                    "forward": day["forward"], "runs": runs})
    return out


if __name__ == "__main__":
    raise SystemExit(main())
