#!/usr/bin/env python3
"""Capture BANKNIFTY market data, open to close. Read-only: places no orders.

The objective is systematic harvesting, not live testing. Everything the API is
used for here is recording -- three channels into a uniform on-disk schema so
the market maker can be tested against the result at any later date. This
decouples the two deadlines that matter: the market happens once a day and
cannot be re-run, whereas the simulator can be written any evening.

    # 08:50 -- resolve and print the universe, touch no network
    python scripts/capture.py --dry-run

    # a 60-second live smoke test
    python scripts/capture.py --duration 60

    # the session
    python scripts/capture.py --until 15:35

    # Sunday / closed-market connect test: auth, URL, subscription acceptance,
    # and the absence of an 805. No frames will flow, so the staleness watchdog
    # is disabled and no data is not a failure.
    python scripts/capture.py --connect-test

Run it under a suspend inhibitor, because a suspended laptop leaves a half-open
socket the server may still count:

    systemd-inhibit --what=sleep:idle python scripts/capture.py --until 15:35

WHAT THIS WRITES
  1. ``data/tier_a/raw/<channel>/date=.../<session>-NNNNN.bnrl`` -- every byte
     received, verbatim, written **before** decoding. This is the primary
     output. If the decoders are wrong about real bytes, the session is still
     fully recoverable by re-decoding offline.
  2. ``data/tier_a/parquet/...`` -- decoded rows, secondary, and wrapped so that
     any store failure increments a counter instead of ending the session.
  3. ``data/tier_a/parquet/sessions/date=.../<session>-capture.json`` -- the
     resolved universe and every counter. A month of Parquet whose subscription
     list has to be inferred cannot be audited: a strike that never traded and
     a strike that was never subscribed look identical downstream.

Credentials come from the environment only; see ``src/bnfmm/data/auth.py``. The
access token rides in the WebSocket URL query string, so the URL is a
credential and is redacted in every log line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bnfmm.data import auth  # noqa: E402
from bnfmm.data import chain as chain_mod  # noqa: E402
from bnfmm.data import channels as ch  # noqa: E402
from bnfmm.data import protocol as proto  # noqa: E402
from bnfmm.data import store  # noqa: E402
from bnfmm.data import universe as uni  # noqa: E402
from bnfmm.data.rawlog import RawLogWriter  # noqa: E402
from bnfmm.data.reconnect import Backoff, ReconnectPolicy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
IST = store.IST

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2


def log(message: str) -> None:
    stamp = datetime.now(tz=IST).strftime("%H:%M:%S")
    print(f"{stamp} {message}", flush=True)


# -- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=str(REPO_ROOT / "config" / "capture.yaml"))
    p.add_argument("--asof", help="resolve the universe for this date (default: today, IST)")
    p.add_argument(
        "--spot",
        type=float,
        help="override the circuit-band spot bootstrap; the previous close is "
        "strictly better than an estimate inferred from price bands",
    )
    p.add_argument("--session-id", help="default: YYYYMMDD-HHMMSS in IST")
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    p.add_argument("--until", help="stop at this IST wall-clock time, e.g. 15:35")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the universe, print it and the subscription messages, and exit "
        "without opening a socket or authenticating",
    )
    p.add_argument(
        "--connect-test",
        action="store_true",
        help="connect and subscribe once per channel, then stop. Disables the "
        "staleness watchdog, so a closed market is not treated as a failure",
    )
    p.add_argument(
        "--no-parquet",
        action="store_true",
        help="write only the raw byte log; the escape hatch if pyarrow misbehaves",
    )
    p.add_argument(
        "--channels",
        help="comma-separated subset of channel names to run (default: all enabled)",
    )
    p.add_argument("--raw-root", help="override session.raw_root")
    p.add_argument("--parquet-root", help="override session.parquet_root")
    p.add_argument(
        "--allow-stale-master",
        action="store_true",
        help="proceed even if the instrument master is older than a day",
    )
    return p


def _parse_until(value: str, *, now: datetime) -> float:
    try:
        hh, mm = (int(x) for x in value.split(":", 1))
    except ValueError as exc:
        raise SystemExit(f"--until must be HH:MM in IST, got {value!r}") from exc
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# -- session assembly ---------------------------------------------------------


def session_id_for(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


def check_master_age(path: Path, *, allow_stale: bool) -> None:
    if not path.exists():
        raise SystemExit(
            f"instrument master not found at {path}\n"
            f"  run: python scripts/refresh_master.py"
        )
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    if age_h <= 24:
        log(f"instrument master is {age_h:.1f}h old: {path.name}")
        return
    message = (
        f"instrument master is {age_h:.1f}h old ({path.name}); a stale master "
        f"resolves the wrong strikes and the error is invisible in the data"
    )
    if allow_stale:
        log(f"WARNING: {message}")
    else:
        raise SystemExit(f"{message}\n  run: python scripts/refresh_master.py "
                         f"(or pass --allow-stale-master)")


def make_rawlog(root: Path, channel: str, session_id: str, cfg: dict, meta: dict) -> RawLogWriter:
    return RawLogWriter(
        root,
        channel,
        session_id,
        metadata=meta,
        rotate_bytes=int(cfg.get("rotate_mib", 256)) * 1024 * 1024,
        flush_records=int(cfg.get("flush_records", 64)),
        flush_bytes=int(cfg.get("flush_mib", 1)) * 1024 * 1024,
        fsync=bool(cfg.get("fsync", False)),
    )


def make_policy(cfg: dict, seed: int) -> ReconnectPolicy:
    return ReconnectPolicy(
        backoff=Backoff(
            base=float(cfg.get("base_delay_s", 1.0)),
            maximum=float(cfg.get("max_delay_s", 60.0)),
            multiplier=float(cfg.get("multiplier", 2.0)),
            jitter=float(cfg.get("jitter", 0.25)),
            seed=seed,
        ),
        max_attempts=int(cfg.get("max_attempts", 20)),
    )


def chain_expiries(universe: uni.CaptureUniverse, selectors: list[str]) -> list[str]:
    """Map ``front``/``next`` selectors onto real dates for the chain poller."""
    out: list[str] = []
    for sel in selectors:
        idx = uni._EXPIRY_INDEX.get(str(sel).lower())
        if idx is None:
            out.append(str(sel))  # an explicit YYYY-MM-DD
        elif idx < len(universe.expiries):
            out.append(universe.expiries[idx].isoformat())
        else:
            log(f"WARNING: chain expiry selector {sel!r} has no matching live expiry")
    if not out:
        raise SystemExit("chain poller has no expiries to poll")
    return out


def build_chain_fetch(session: auth.Session, config: dict) -> Any:
    """An async ``fetch(expiry) -> bytes`` over the blocking REST call."""
    cfg = config.get("option_chain", {})
    scrip = int(cfg.get("underlying_scrip", 25))
    segment = str(cfg.get("underlying_segment", chain_mod.IDX_SEGMENT))
    headers = session.headers()

    async def fetch(expiry: str) -> bytes:
        return await asyncio.to_thread(
            chain_mod.fetch_chain_bytes,
            headers,
            underlying_scrip=scrip,
            underlying_segment=segment,
            expiry=expiry,
        )

    return fetch


# -- run ----------------------------------------------------------------------


async def run_capture(args: argparse.Namespace) -> int:
    config = uni.load_capture_config(args.config)
    session_cfg = dict(config.get("session", {}))
    keepalive_cfg = dict(config.get("keepalive", {}))
    reconnect_cfg = dict(config.get("reconnect", {}))
    chain_cfg = dict(config.get("option_chain", {}))

    asof = date.fromisoformat(args.asof) if args.asof else datetime.now(tz=IST).date()
    master_rel = config.get(
        "instrument_master_path", "data/reference/api-scrip-master-detailed.csv"
    )
    master_path = REPO_ROOT / master_rel
    check_master_age(master_path, allow_stale=args.allow_stale_master)

    universe = uni.resolve_capture_universe(
        uni.read_master(master_path), config, asof=asof, spot=args.spot
    )
    # Echoed before anything connects, so a mis-resolved strike set is caught at
    # 08:50 rather than discovered in the Parquet at 15:40.
    print(universe.describe(), flush=True)

    wanted = None
    if args.channels:
        wanted = {c.strip() for c in args.channels.split(",") if c.strip()}
        unknown = wanted - {c.name for c in universe.channels}
        if unknown:
            raise SystemExit(f"unknown channel(s) {sorted(unknown)}")

    selected = [
        c
        for c in universe.channels
        if c.enabled and (wanted is None or c.name in wanted)
    ]
    if not selected:
        raise SystemExit("no channels selected")

    if args.dry_run:
        log("--dry-run: printing subscription messages, connecting to nothing")
        for cu in selected:
            if cu.protocol == "depth_20":
                msgs = proto.depth_subscriptions(cu.instruments)
            elif cu.protocol == "market_feed":
                msgs = proto.feed_subscriptions(cu.instruments)
            else:
                msgs = [
                    chain_mod.chain_request(
                        int(chain_cfg.get("underlying_scrip", 25)),
                        str(chain_cfg.get("underlying_segment", chain_mod.IDX_SEGMENT)),
                        e,
                    )
                    for e in chain_expiries(universe, list(chain_cfg.get("expiries", ["front"])))
                ]
            log(f"[{cu.name}] {len(msgs)} message(s):")
            for m in msgs:
                print(json.dumps(m)[:400], flush=True)
        if args.duration or args.until:
            log("note: --dry-run opens no socket, so --duration/--until are ignored")
        return EXIT_OK

    now = datetime.now(tz=IST)
    session_id = args.session_id or session_id_for(now)
    duration = args.duration
    if args.until:
        until_s = _parse_until(args.until, now=now)
        duration = until_s if duration is None else min(duration, until_s)
        log(f"stopping at {args.until} IST, in {until_s / 3600:.2f}h")
    if args.connect_test:
        # Long enough to see a subscription rejection or an 805, short enough to
        # be a pre-market check rather than a session.
        duration = duration if duration is not None else 20.0

    # Read the gitignored .env before touching auth. Real environment variables
    # still win, so an exported token overrides the file. Without this the whole
    # run dies at the login call with "CLIENT_ID is not set" while a perfectly
    # good .env sits two directories up -- which is a 09:00 failure, not a 23:00 one.
    auth.load_dotenv(REPO_ROOT / ".env")

    dhan = auth.login(allow_env_token=True)
    if dhan.likely_expired:
        log("WARNING: access token looks expired; a fresh one is a human-supplied step")
    log(f"authenticated as client {dhan.client_id}")

    raw_root = Path(args.raw_root or REPO_ROOT / session_cfg.get("raw_root", "data/tier_a/raw"))
    pq_root = Path(
        args.parquet_root or REPO_ROOT / session_cfg.get("parquet_root", "data/tier_a/parquet")
    )
    write_parquet = bool(session_cfg.get("write_parquet", True)) and not args.no_parquet
    if not write_parquet:
        log("Parquet output disabled; the raw byte log is unaffected")

    ping_interval = float(keepalive_cfg.get("ping_interval_s", 15.0))
    ping_timeout = float(keepalive_cfg.get("ping_timeout_s", 20.0))
    stale_after = (
        None if args.connect_test else float(keepalive_cfg.get("stale_after_s", 45.0))
    )
    connector = ch.websockets_connector(
        ping_interval_s=ping_interval, ping_timeout_s=ping_timeout
    )
    log(
        f"keepalive: library ping every {ping_interval:.0f}s against the "
        f"{proto.SERVER_SILENCE_TIMEOUT_S:.0f}s server limit; "
        f"staleness watchdog {stale_after if stale_after else 'disabled'}"
    )

    meta_base = {
        "session_id": session_id,
        "asof": asof.isoformat(),
        "atm_strike": universe.atm_strike,
        "spot": universe.spot,
        "front_expiry": universe.front_expiry.isoformat(),
    }

    rawlogs: list[RawLogWriter] = []
    writers: list[store.CaptureWriter] = []
    runners: list[tuple[str, Any]] = []
    stop = asyncio.Event()

    try:
        for seed, cu in enumerate(selected):
            meta = {
                **meta_base,
                "channel": cu.name,
                "protocol": cu.protocol,
                "security_ids": [c.security_id for c in cu.contracts],
            }
            rl = make_rawlog(raw_root, cu.name, session_id, session_cfg, meta)
            rawlogs.append(rl)

            if cu.protocol == "option_chain":
                poller = ch.ChainPollChannel(
                    name=cu.name,
                    expiries=chain_expiries(universe, list(chain_cfg.get("expiries", ["front"]))),
                    fetch=build_chain_fetch(dhan, config),
                    rawlog=rl,
                    min_interval_s=float(
                        chain_cfg.get("min_interval_s", proto.OPTION_CHAIN_MIN_INTERVAL_S + 0.5)
                    ),
                    log=log,
                )
                runners.append((cu.name, poller))
                continue

            appender = None
            if write_parquet:
                appenders = []
                if cu.protocol == "depth_20":
                    w = store.depth_writer(pq_root, f"{session_id}-{cu.name}")
                    writers.append(w)
                    appenders.append(ch.store_appender(w))
                else:
                    # The general feed carries snapshots and, on the depth-5 code,
                    # book frames; each writer records what is not its table as a
                    # skip, so fanning out costs nothing and loses nothing.
                    for factory in (store.snapshot_writer, store.depth_writer):
                        w = factory(pq_root, f"{session_id}-{cu.name}")
                        writers.append(w)
                        appenders.append(ch.store_appender(w))
                appender = ch.fanout_appender(appenders)

            factory = ch.depth_channel if cu.protocol == "depth_20" else ch.feed_channel
            channel = factory(
                name=cu.name,
                instruments=cu.instruments,
                token=dhan.access_token,
                client_id=dhan.client_id,
                rawlog=rl,
                connect=connector,
                store_append=appender,
                policy=make_policy(reconnect_cfg, seed),
                stale_after_s=stale_after,
                log=log,
            )
            runners.append((cu.name, channel))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass

        kwargs: dict[str, Any] = {"stop": stop, "duration_s": duration}
        tasks = []
        for name, runner in runners:
            if isinstance(runner, ch.ChainPollChannel):
                tasks.append(asyncio.create_task(runner.run(**kwargs), name=name))
            else:
                extra = {"max_connections": 1} if args.connect_test else {}
                tasks.append(asyncio.create_task(runner.run(**kwargs, **extra), name=name))

        log(f"capturing {len(tasks)} channel(s): {[n for n, _ in runners]}")
        await asyncio.gather(*tasks, return_exceptions=True)
        # One channel finishing must not leave the others running: a session with
        # depth stopped and the feed still going is worse than a clean stop,
        # because the gap is invisible in the feed's own data.
        stop.set()

    finally:
        for w in writers:
            try:
                w.close()
                w.write_manifest()
            except Exception as exc:  # noqa: BLE001
                log(f"WARNING: closing Parquet writer failed: {type(exc).__name__}: {exc}")
        for rl in rawlogs:
            try:
                rl.close()
            except Exception as exc:  # noqa: BLE001
                log(f"WARNING: closing raw log failed: {type(exc).__name__}: {exc}")

    return report(universe, runners, session_id, pq_root, asof, args)


def report(
    universe: uni.CaptureUniverse,
    runners: list[tuple[str, Any]],
    session_id: str,
    pq_root: Path,
    asof: date,
    args: argparse.Namespace,
) -> int:
    print("", flush=True)
    log("=== session summary ===")
    degraded: list[str] = []
    for name, runner in runners:
        stats = runner.stats
        log(f"[{name}] {stats.summary()}")
        if isinstance(runner, ch.FeedChannel):
            log(f"[{name}] decode: {runner.decoder.stats.summary()}")
            if runner.policy.stopped:
                degraded.append(f"{name}: {runner.policy.fatal_reason}")
            if stats.decode_errors or stats.store_errors:
                degraded.append(f"{name}: {stats.decode_errors} decode / {stats.store_errors} store errors")
            if not stats.had_data and not args.connect_test:
                degraded.append(f"{name}: received no data at all")
        elif isinstance(runner, ch.ChainPollChannel) and stats.errors and not stats.polls:
            degraded.append(f"{name}: every poll failed ({stats.last_error})")

    manifest = {
        "session_id": session_id,
        "asof": asof.isoformat(),
        "started_by": "scripts/capture.py",
        "connect_test": bool(args.connect_test),
        "universe": universe.manifest(),
        "channels": {name: runner.manifest() for name, runner in runners},
        "degraded": degraded,
    }
    out_dir = pq_root / "sessions" / f"date={asof.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{session_id}-capture.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    log(f"manifest: {out_path}")

    if degraded:
        log("DEGRADED:")
        for line in degraded:
            log(f"  - {line}")
        return EXIT_DEGRADED
    log("all channels clean")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_capture(args))
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_DEGRADED
    except auth.AuthError as exc:
        log(f"authentication failed: {exc}")
        return EXIT_FAILED
    except uni.UniverseError as exc:
        log(f"universe could not be resolved: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
