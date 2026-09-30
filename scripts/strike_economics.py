"""Per-strike market-making economics: is the spread wider than the cost floor?

WHY THIS EXISTS
    Options STT is levied on *premium*, not notional, so the statutory cost floor
    of a round trip scales roughly linearly with the option's price. The bid-ask
    spread does not scale the same way. So the ratio

        median market spread / round-trip cost floor

    varies enormously across a strike ladder, and it decides where -- if anywhere
    -- a market maker can operate at all. This is a pure arithmetic screen: it does
    no simulation and makes no assumption about fill rates or adverse selection. A
    strike that fails here cannot be profitable at any fill rate, so there is no
    point backtesting it.

USAGE
    python scripts/strike_economics.py
    python scripts/strike_economics.py --date 2026-08-25
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.book.reconstruct import load_book  # noqa: E402
from bnfmm.book.tape import load_tape  # noqa: E402
from bnfmm.sim.costs import CostModel, Product  # noqa: E402

ROOT = _REPO_ROOT / "data" / "tier_a" / "parquet"
SESSIONS = ROOT / "sessions"
DAYS = [date(2026, 8, 24), date(2026, 8, 25)]


def contracts(day: date) -> list[dict]:
    paths = sorted((SESSIONS / f"date={day.isoformat()}").glob("*-contracts.json"))
    if not paths:
        raise SystemExit(f"no contracts sidecar for {day}")
    return json.loads(paths[-1].read_text())["channels"]["depth"]["contracts"]


def scan(day: date, lots: int) -> None:
    member = CostModel.for_profile("member")
    dhan = CostModel.for_profile("dhan")

    rows = []
    for c in contracts(day):
        if c["instrument"] != "OPTIDX":
            continue
        tick = c["tick_size"]
        book = load_book(str(ROOT), day, c["security_id"], levels=1)
        if not len(book):
            continue
        mid = book.mid
        spread = book.spread
        two = book.two_sided & np.isfinite(spread) & (spread > 0)
        if two.sum() < 1000:  # too thin to characterise
            continue
        prem = float(np.nanmedian(mid[two]))
        sp_ticks = float(np.nanmedian(spread[two]) / tick)
        if not np.isfinite(prem) or prem <= 0:
            continue
        bm = member.breakeven_ticks(Product.OPTIONS, prem, c["lot_size"], tick, n_lots=lots)
        bd = dhan.breakeven_ticks(Product.OPTIONS, prem, c["lot_size"], tick, n_lots=lots)
        # Flow matters as much as headroom. A deep-ITM put can show 200 ticks of
        # headroom and be untradeable, because its spread is wide precisely because
        # nobody trades it. Day volume is read from the tape's own reconstruction.
        tape = load_tape(str(ROOT), day, c["security_id"])
        volume = int(tape.quantity.sum())
        headroom = sp_ticks - bm
        rows.append({
            "strike": c["strike"], "leg": c["option_type"], "premium": prem,
            "spread_ticks": sp_ticks, "member": bm, "dhan": bd,
            # Headroom is what is left of the spread after the cost floor. It is an
            # upper bound on gross edge per round trip and it ignores adverse
            # selection entirely, so a positive value is necessary, not sufficient.
            "headroom_member": headroom,
            "headroom_dhan": sp_ticks - bd,
            # Ratio is the scale-free version: how many times over does the spread
            # cover the floor? This is what actually differs across the ladder,
            # because STT scales with premium while the spread does not.
            "ratio_member": sp_ticks / bm if bm > 0 else float("inf"),
            "volume": volume,
            # A crude ceiling on daily gross: capture `headroom` ticks on a 1%
            # share of the day's volume, halved because a round trip needs two
            # fills. Not a forecast -- an order-of-magnitude screen.
            "capacity": headroom * tick * volume * 0.01 / 2.0,
            "two_sided_pct": 100.0 * float(two.mean()),
        })

    rows.sort(key=lambda r: (r["strike"], r["leg"]))
    print(f"\n{'=' * 104}")
    print(f"  PER-STRIKE ECONOMICS  {day}   ({lots} lot/order)")
    print("  Round-trip cost floor vs median market spread. Headroom = spread - floor,")
    print("  an upper bound on gross edge that ignores adverse selection entirely.")
    print("  ratio = spread/floor; capacity = headroom captured on 1% of day volume.")
    print("=" * 104)
    print(f"  {'strike':>7} {'leg':>3} {'premium':>9} {'spread':>7} {'floor_m':>8} "
          f"{'head_m':>8} {'ratio':>6} {'volume':>12} {'capacity':>10} {'head_dhan':>10}")
    for r in rows:
        print(f"  {r['strike']:>7.0f} {r['leg']:>3} {r['premium']:>9.2f} "
              f"{r['spread_ticks']:>7.1f} "
              f"{r['member']:>8.2f} {r['headroom_member']:>+8.2f} "
              f"{r['ratio_member']:>6.2f} {r['volume']:>12,} "
              f"{r['capacity']:>10,.0f} {r['headroom_dhan']:>+10.2f}")

    ok_m = [r for r in rows if r["headroom_member"] > 0]
    ok_d = [r for r in rows if r["headroom_dhan"] > 0]
    print(f"\n  member: {len(ok_m)}/{len(rows)} contracts clear the floor")
    print(f"  dhan  : {len(ok_d)}/{len(rows)} contracts clear the floor")

    # Rank by capacity, which is the only column that weighs headroom against flow.
    print("\n  TOP 8 BY CAPACITY (headroom x flow) -- member profile:")
    print(f"    {'strike':>7} {'leg':>3} {'premium':>9} {'spread':>7} {'floor':>7} "
          f"{'head':>7} {'ratio':>6} {'volume':>12} {'capacity':>10}")
    for r in sorted(ok_m, key=lambda r: -r["capacity"])[:8]:
        print(f"    {r['strike']:>7.0f} {r['leg']:>3} {r['premium']:>9.2f} "
              f"{r['spread_ticks']:>7.1f} {r['member']:>7.2f} "
              f"{r['headroom_member']:>+7.2f} {r['ratio_member']:>6.2f} "
              f"{r['volume']:>12,} {r['capacity']:>10,.0f}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", action="append")
    p.add_argument("--lots", type=int, default=1)
    a = p.parse_args(argv)
    days = [date.fromisoformat(d) for d in a.date] if a.date else DAYS
    for d in days:
        scan(d, a.lots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
