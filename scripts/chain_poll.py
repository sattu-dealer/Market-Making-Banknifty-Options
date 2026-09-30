#!/usr/bin/env python3
"""Poll the REST option chain into the raw log. Read-only: places no orders.

This is the second independent safety net for a capture session, and the word
that earns its keep is **independent**. It is plain JSON over HTTPS, so it
cannot fail for any reason the binary wire decoders can; and run as its own
process, it cannot fail for any reason ``capture.py`` can. If the WebSocket
capture dies at 11:00 for a reason nobody anticipated, the day still yields
whole-chain top-of-book, IV, greeks and OI at 3-second resolution.

    # alongside capture.py, in a second terminal
    python scripts/chain_poll.py --until 15:35

    # one poll per expiry, to check entitlement and the reply shape
    python scripts/chain_poll.py --polls 2 --verbose

It also reaches coverage the depth budget never can: 50 depth slots against
~760 legs per expiry. That makes it the cross-check that turns two feeds into a
data-quality check -- if the WebSocket top-of-book and the chain's
``top_bid_price`` disagree, one of them is wrong, and finding that out is free.

The response bytes are archived verbatim, wrapped in an envelope naming the
expiry (the vendor reply does not say which expiry it answers). Parsing is
deferred to an offline converter: a parser bug must not be able to cost a row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bnfmm.data import auth  # noqa: E402
from bnfmm.data import chain as chain_mod  # noqa: E402
from bnfmm.data import channels as ch  # noqa: E402
from bnfmm.data import protocol as proto  # noqa: E402
from bnfmm.data import store  # noqa: E402
from bnfmm.data import universe as uni  # noqa: E402
from bnfmm.data.rawlog import RawLogWriter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
IST = store.IST


def log(message: str) -> None:
    print(f"{datetime.now(tz=IST).strftime('%H:%M:%S')} {message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", default=str(REPO_ROOT / "config" / "capture.yaml"))
    p.add_argument("--asof", help="resolve expiries for this date (default: today, IST)")
    p.add_argument("--session-id", help="default: YYYYMMDD-HHMMSS in IST")
    p.add_argument("--channel", default="chain", help="raw-log channel name")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--until", help="stop at this IST wall-clock time, e.g. 15:35")
    p.add_argument("--polls", type=int, help="stop after this many successful polls")
    p.add_argument(
        "--expiry",
        action="append",
        help="explicit YYYY-MM-DD, repeatable; default: the config's front/next",
    )
    p.add_argument(
        "--interval",
        type=float,
        help=f"seconds between polls; the documented floor is "
        f"{proto.OPTION_CHAIN_MIN_INTERVAL_S}",
    )
    p.add_argument("--raw-root")
    p.add_argument(
        "--verbose",
        action="store_true",
        help="parse each reply locally and print a one-line ATM summary. The "
        "archived bytes are unaffected -- this only reads them back",
    )
    return p


def _until_seconds(value: str, now: datetime) -> float:
    hh, mm = (int(x) for x in value.split(":", 1))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run(args: argparse.Namespace) -> int:
    config = uni.load_capture_config(args.config)
    chain_cfg = dict(config.get("option_chain", {}))
    session_cfg = dict(config.get("session", {}))
    asof = date.fromisoformat(args.asof) if args.asof else datetime.now(tz=IST).date()

    if args.expiry:
        expiries = list(args.expiry)
        atm_strike = None
    else:
        master_rel = config.get(
            "instrument_master_path", "data/reference/api-scrip-master-detailed.csv"
        )
        universe = uni.resolve_capture_universe(
            uni.read_master(REPO_ROOT / master_rel), config, asof=asof
        )
        selectors = [str(s) for s in chain_cfg.get("expiries", ["front"])]
        expiries = [
            universe.expiries[uni._EXPIRY_INDEX[s]].isoformat()
            for s in selectors
            if s in uni._EXPIRY_INDEX and uni._EXPIRY_INDEX[s] < len(universe.expiries)
        ]
        atm_strike = universe.atm_strike
    if not expiries:
        raise SystemExit("no expiries to poll")
    log(f"polling expiries {expiries}")

    dhan = auth.login(allow_env_token=True)
    if dhan.likely_expired:
        log("WARNING: access token looks expired")
    log(f"authenticated as client {dhan.client_id}")

    scrip = int(chain_cfg.get("underlying_scrip", 25))
    segment = str(chain_cfg.get("underlying_segment", chain_mod.IDX_SEGMENT))
    headers = dhan.headers()

    async def fetch(expiry: str) -> bytes:
        payload = await asyncio.to_thread(
            chain_mod.fetch_chain_bytes,
            headers,
            underlying_scrip=scrip,
            underlying_segment=segment,
            expiry=expiry,
        )
        if args.verbose:
            _describe(payload, expiry, atm_strike)
        return payload

    now = datetime.now(tz=IST)
    session_id = args.session_id or now.strftime("%Y%m%d-%H%M%S")
    raw_root = Path(args.raw_root or REPO_ROOT / session_cfg.get("raw_root", "data/tier_a/raw"))
    duration = args.duration
    if args.until:
        remaining = _until_seconds(args.until, now)
        duration = remaining if duration is None else min(duration, remaining)
        log(f"stopping at {args.until} IST, in {remaining / 3600:.2f}h")

    rawlog = RawLogWriter(
        raw_root,
        args.channel,
        session_id,
        metadata={
            "session_id": session_id,
            "asof": asof.isoformat(),
            "source": "rest_option_chain",
            "underlying_scrip": scrip,
            "underlying_segment": segment,
            "expiries": expiries,
        },
        rotate_bytes=int(session_cfg.get("rotate_mib", 256)) * 1024 * 1024,
        flush_records=int(session_cfg.get("flush_records", 64)),
        flush_bytes=int(session_cfg.get("flush_mib", 1)) * 1024 * 1024,
        fsync=bool(session_cfg.get("fsync", False)),
    )

    interval = float(
        args.interval
        if args.interval is not None
        else chain_cfg.get("min_interval_s", proto.OPTION_CHAIN_MIN_INTERVAL_S + 0.5)
    )
    poller = ch.ChainPollChannel(
        name=args.channel,
        expiries=expiries,
        fetch=fetch,
        rawlog=rawlog,
        min_interval_s=interval,
        log=log,
    )
    log(f"interval {interval}s (documented floor {proto.OPTION_CHAIN_MIN_INTERVAL_S}s)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    try:
        await poller.run(stop=stop, duration_s=duration, max_polls=args.polls)
    finally:
        rawlog.close()

    log(poller.stats.summary())
    log(f"raw log: {[str(p) for p in rawlog.paths]}")
    if poller.stats.polls == 0:
        log("FAILED: no poll succeeded")
        return 2
    return 1 if poller.stats.errors else 0


def _describe(payload: bytes, expiry: str, atm_strike: float | None) -> None:
    """Print the one number the whole project hinges on: the ATM quoted spread.

    Breakeven is Rs 2.37 statutory-only and Rs 3.94 with Dhan's flat Rs 20 at one
    lot. Whether the real quoted spread clears that is the question Phase 0 could
    not answer offline, and this is the cheapest possible look at it.
    """
    try:
        snap = chain_mod.parse_chain(payload, expiry=expiry)
    except Exception as exc:  # noqa: BLE001
        log(f"  (could not parse reply for {expiry}: {type(exc).__name__}: {exc})")
        return
    ref = atm_strike if atm_strike is not None else snap.last_price
    if ref is None or not snap.legs:
        log(f"  {expiry}: {len(snap.legs)} legs, underlying {snap.last_price}")
        return
    atm = min(snap.strikes, key=lambda k: abs(k - ref))
    sides = snap.by_strike().get(atm, {})
    bits = []
    for side in ("CE", "PE"):
        leg = sides.get(side)
        if leg is None:
            continue
        spread = leg.spread
        bits.append(
            f"{side} {leg.top_bid_price}/{leg.top_ask_price}"
            + (f" spread {spread:.2f}" if spread is not None else "")
            + (f" iv {leg.implied_volatility:.1f}" if leg.implied_volatility else "")
        )
    log(f"  {expiry}: underlying {snap.last_price}, {len(snap.legs)} legs, ATM {atm:.0f} -> "
        + "; ".join(bits))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("interrupted")
        return 1
    except auth.AuthError as exc:
        log(f"authentication failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
