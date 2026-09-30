#!/usr/bin/env python3
"""BNFMM -- the single entry point. Record a live session, or work on a recorded one.

Run it with no arguments and it asks which of two things you want:

  1. **Record a live market session.** Waits for the next market open if the
     market is not open yet, runs preflight, captures through the close, then
     reconciles what it wrote against the raw byte log.
  2. **Work on data already recorded.** Lists the sessions on disk, takes a pick,
     and runs the offline routine against that session.

Everything this launches also exists as a directly callable script --
`scripts/capture.py` above all -- and nothing here reimplements it. What this file
adds is the two things a script cannot: the choice of which to run, and the
discipline of *waiting for the open* so that starting the process at 07:00 on a
Monday is a complete instruction rather than a mistake.

WHY WAITING IS A FEATURE AND NOT A CONVENIENCE
    A trading session happens once and cannot be re-run. The failure mode this
    removes is the one where a human means to start capture at 09:00, is not at
    the keyboard, and loses the open -- which is the single most information-dense
    part of the day. Launch this the night before, or at 07:00, and the open is
    covered without anyone being present.

NON-INTERACTIVE USE
    `--mode record` skips the menu, which is what you want under `systemd-inhibit`,
    `nohup`, or a timer. `--mode analyse --session <id>` does the same for the
    offline side. With no TTY and no `--mode`, this exits rather than guessing.

SAFETY
    No order-placement endpoint is reachable from anything this launches, by
    construction rather than by intent -- see `tests/test_no_order_placement.py`,
    which fails the build if an order symbol or a `dhanhq` order module appears
    anywhere under `src/`. Credentials come from the environment or a gitignored
    `.env`; see `src/bnfmm/data/auth.py`. The access token is a credential *in the
    URL* for both WebSocket feeds, so every URL this prints is redacted.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# `src` for the package, `scripts` because capture.py is a script rather than a
# module and importing it is strictly better than shelling out: one process, one
# Ctrl-C, one log stream, and a real exit code instead of a parsed one.
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import yaml  # noqa: E402

from bnfmm.data import auth, instruments as ins, protocol, rawlog, store  # noqa: E402

IST = store.IST

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FAILED = 2

CALENDAR_PATH = REPO_ROOT / "config" / "market_calendar.yaml"
CAPTURE_CONFIG_PATH = REPO_ROOT / "config" / "capture.yaml"

# Announce a pending wait on a schedule that gets denser as the target nears, so a
# nine-hour overnight wait does not produce nine hours of log noise but the last
# two minutes before the open are visible at a glance.
_WAIT_SLICE_S = 30.0


def log(message: str = "") -> None:
    if not message:
        print(flush=True)
        return
    print(f"{datetime.now(tz=IST):%H:%M:%S} {message}", flush=True)


def rule(title: str = "") -> None:
    print(f"\n{'=' * 72}", flush=True)
    if title:
        print(title, flush=True)
        print("=" * 72, flush=True)


# --- the market calendar ------------------------------------------------------


@dataclass(frozen=True)
class Calendar:
    """NSE session windows plus the trading-day rule.

    Weekends are a rule and need no data. Holidays need data this machine could
    not obtain (nseindia.com refuses automated requests), so `holidays_verified`
    is carried explicitly and reported on every wait rather than being silently
    assumed. An unlisted holiday costs a wasted morning, not bad data: the
    session records zero rows and the reconciliation says so.
    """

    holidays: frozenset[date]
    holidays_verified: bool
    capture_start: dtime
    capture_stop: dtime
    continuous_start: dtime
    continuous_end: dtime
    preflight_lead_s: float

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def at(self, day: date, clock: dtime) -> datetime:
        return datetime.combine(day, clock, tzinfo=IST)


def _clock(raw: object, default: str) -> dtime:
    text = str(raw or default)
    hh, mm = (int(x) for x in text.split(":", 1))
    return dtime(hour=hh, minute=mm, tzinfo=None)


def load_calendar(path: Path = CALENDAR_PATH) -> Calendar:
    cfg: dict = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text()) or {}
    sessions = cfg.get("sessions") or {}

    raw_holidays = cfg.get("holidays") or []
    holidays = frozenset(
        h if isinstance(h, date) else date.fromisoformat(str(h)) for h in raw_holidays
    )
    return Calendar(
        holidays=holidays,
        holidays_verified=bool(holidays),
        capture_start=_clock(sessions.get("capture_start"), "08:58"),
        capture_stop=_clock(sessions.get("capture_stop"), "15:35"),
        continuous_start=_clock(sessions.get("continuous_start"), "09:15"),
        continuous_end=_clock(sessions.get("continuous_end"), "15:30"),
        preflight_lead_s=float(cfg.get("preflight_lead_s", 600)),
    )


@dataclass(frozen=True)
class Window:
    """One capture window, already decided.

    `partial` means we are launching mid-session, so the open is already gone.
    That is worth saying out loud rather than discovering in the Parquet: a
    partial session is still useful data but it is not comparable to a full one,
    and the analysis has to know which it has.
    """

    day: date
    start: datetime
    stop: datetime
    partial: bool

    def wait_s(self, now: datetime) -> float:
        return max(0.0, (self.start - now).total_seconds())

    def duration_s(self, now: datetime) -> float:
        return max(0.0, (self.stop - max(now, self.start)).total_seconds())


def plan_window(cal: Calendar, now: datetime, *, search_days: int = 21) -> Window:
    """The next capture window at or after `now`.

    Three cases, in the order they are tested:
      - inside today's window  -> start immediately, flagged partial
      - before today's window  -> wait for today's open
      - after it, or today is not a trading day -> the next trading day's open

    `search_days` bounds the forward scan so a mistakenly enormous holiday list
    raises instead of looping.
    """
    today = now.date()
    if cal.is_trading_day(today):
        start = cal.at(today, cal.capture_start)
        stop = cal.at(today, cal.capture_stop)
        if now < stop:
            return Window(today, start, stop, partial=now > start)

    for ahead in range(1, search_days + 1):
        day = today + timedelta(days=ahead)
        if cal.is_trading_day(day):
            return Window(
                day,
                cal.at(day, cal.capture_start),
                cal.at(day, cal.capture_stop),
                partial=False,
            )
    raise SystemExit(
        f"no trading day found within {search_days} days of {today}; "
        f"check the holiday list in {CALENDAR_PATH.name}"
    )


def _human(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _announce_cadence(remaining_s: float) -> float:
    if remaining_s > 3600:
        return 900.0
    if remaining_s > 600:
        return 300.0
    if remaining_s > 120:
        return 60.0
    return 15.0


def sleep_until(target: datetime, *, label: str) -> None:
    """Sleep until a wall-clock instant, re-reading the clock every slice.

    Deliberately **not** one long `time.sleep`. Two reasons, both of which have
    bitten this kind of script before:

      - A laptop suspends. `time.sleep` is implemented on `CLOCK_MONOTONIC`,
        which on Linux does not advance while suspended, so a single long sleep
        wakes late by however long the lid was shut. Re-deriving the remaining
        time from the wall clock each slice makes suspend cost nothing.
      - Ctrl-C should be responsive. A slice of 30 s bounds how long an operator
        waits for the process to notice.

    The countdown is printed on a cadence that tightens as the target nears.
    """
    announced_at = 0.0
    while True:
        now = datetime.now(tz=IST)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        mono = time.monotonic()
        if mono - announced_at >= _announce_cadence(remaining) or announced_at == 0.0:
            log(f"{label}: {_human(remaining)} to go (target {target:%a %d %b %H:%M} IST)")
            announced_at = mono
        time.sleep(min(remaining, _WAIT_SLICE_S))


# --- preflight ----------------------------------------------------------------


def _looks_inhibited() -> bool:
    """Heuristic: is an ancestor process `systemd-inhibit`?

    Heuristic and labelled as one. `systemd-inhibit` holds the lock in the parent
    and execs nothing away, so its cmdline is usually visible one level up, but a
    shell wrapper or a different inhibitor breaks the check. A false negative
    costs a redundant warning, which is the right direction to be wrong in.
    """
    try:
        ppid = os.getppid()
        cmdline = Path(f"/proc/{ppid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "systemd-inhibit" in cmdline


def preflight(*, asof: date, allow_stale_master: bool, spot: float | None = None) -> bool:
    """Everything that should fail *before* the open rather than during it.

    Returns whether it is safe to proceed. Deliberately ordered cheapest-first so
    the common failure (no credentials) is reported in a second rather than after
    a 36 MB download.

    ``spot`` is forwarded to the dry run so the ladder printed here is the ladder
    that gets recorded. Resolving the two differently would make this check worse
    than useless -- it would confirm a universe nobody subscribes to.
    """
    rule("PREFLIGHT")
    ok = True

    # 1. Credentials. The cheapest check and the most common failure.
    auth.load_dotenv(REPO_ROOT / ".env")
    try:
        session = auth.login(allow_env_token=True)
    except auth.AuthError as exc:
        log(f"FAIL  credentials: {exc}")
        log("      fill in .env (see .env.example) -- nothing else here can run without it")
        return False
    log(f"ok    credentials: {session!r}")
    if session.likely_expired:
        log("WARN  the access token looks expired. A 24 h token minted yesterday evening")
        log("      will not survive today's close. Mint a fresh one before the open.")
        ok = False

    # 2. Instrument master. capture.py *raises* above 24 h rather than warning, so
    #    refreshing here is load-bearing and not hygiene: a master left over from
    #    the night before is already stale by the open.
    try:
        config = ins.load_config()
        path = ins.ensure_master(config, force=False)
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h > 12 and not allow_stale_master:
            log(f"      master is {age_h:.1f}h old, refreshing")
            path = ins.ensure_master(config, force=True)
            age_h = (time.time() - path.stat().st_mtime) / 3600.0
        log(f"ok    instrument master: {path.name}, {age_h:.1f}h old, {path.stat().st_size / 1e6:.1f} MB")
    except Exception as exc:  # noqa: BLE001 - any failure here is a preflight finding
        log(f"FAIL  instrument master: {type(exc).__name__}: {exc}")
        ok = False

    # 3. Disk. A full session is ~GB-scale and running out mid-afternoon loses the
    #    close, which is the second most valuable part of the day.
    try:
        usage = os.statvfs(REPO_ROOT)
        free_gb = usage.f_bavail * usage.f_frsize / 1e9
        verdict = "ok   " if free_gb > 20 else "WARN "
        log(f"{verdict} disk: {free_gb:.0f} GB free")
        if free_gb <= 20:
            ok = False
    except OSError as exc:
        log(f"WARN  disk: could not check ({exc})")

    # 4. Universe resolution, without touching a socket. This is the check that
    #    catches a mis-resolved strike ladder at 08:48 instead of at 15:40.
    log("")
    log("resolving the capture universe (dry run, no socket, no auth):")
    import capture  # noqa: PLC0415 - deliberately late; adds `scripts` path work

    rc = capture.main(
        [
            "--dry-run",
            "--asof",
            asof.isoformat(),
            *(["--spot", f"{spot}"] if spot is not None else []),
            *(["--allow-stale-master"] if allow_stale_master else []),
        ]
    )
    if rc != EXIT_OK:
        log(f"FAIL  universe dry run returned {rc}")
        ok = False
    else:
        log("ok    universe resolves and fits inside every documented cap")

    log("")
    log("PREFLIGHT PASSED" if ok else "PREFLIGHT RAISED AT LEAST ONE PROBLEM (see above)")
    return ok


# --- record mode --------------------------------------------------------------


def mode_record(args: argparse.Namespace) -> int:
    cal = load_calendar(Path(args.calendar))
    now = datetime.now(tz=IST)
    window = plan_window(cal, now)

    rule("RECORD A LIVE SESSION")
    log(f"now              {now:%a %d %b %Y %H:%M:%S} IST")
    log(f"capture window   {window.day:%a %d %b %Y}  {window.start:%H:%M} -> {window.stop:%H:%M} IST")
    log(f"continuous trade {cal.at(window.day, cal.continuous_start):%H:%M}"
        f" -> {cal.at(window.day, cal.continuous_end):%H:%M} IST")

    wait_s = window.wait_s(now)
    if window.partial:
        log("NOTE  the market is already open -- this will be a PARTIAL session.")
        log("      The pre-open call and the opening print are already gone and cannot")
        log("      be recovered. The data is still usable; it is just not comparable")
        log("      to a full session, and the manifest records the true start time.")
    elif wait_s > 0:
        log(f"waiting          {_human(wait_s)} until the open")
    if not cal.holidays_verified:
        log("WARN  the holiday list in config/market_calendar.yaml is EMPTY, so a public")
        log("      holiday is indistinguishable from a trading day here. If this wakes on")
        log("      a closed day it will record zero rows and say so -- a wasted morning,")
        log("      not corrupt data. Paste the NSE list in to remove the risk.")
    if wait_s > 1800 and not _looks_inhibited():
        log("WARN  a long wait is planned and this does not look like it is running under")
        log("      systemd-inhibit. A suspend will not lose the wait (the countdown is")
        log("      wall-clock based) but it WILL drop the sockets mid-session. Prefer:")
        log("        systemd-inhibit --what=sleep:idle .venv/bin/python main.py --mode record")

    # Preflight lands `preflight_lead_s` before the open when there is time for it,
    # and immediately otherwise. Doing it early is the point: a failure at 08:48
    # is recoverable, the same failure at 09:00 is a lost open.
    if wait_s > cal.preflight_lead_s:
        sleep_until(window.start - timedelta(seconds=cal.preflight_lead_s), label="waiting for preflight")

    if not preflight(
        asof=window.day, allow_stale_master=args.allow_stale_master, spot=args.spot
    ):
        if not args.force:
            log("")
            log("stopping. Fix the above, or re-run with --force to capture anyway.")
            return EXIT_FAILED
        log("--force given: proceeding despite preflight problems")

    now = datetime.now(tz=IST)
    if now < window.start:
        sleep_until(window.start, label="waiting for the open")

    now = datetime.now(tz=IST)
    duration = window.duration_s(now)
    if duration <= 0:
        log("the capture window closed while waiting; nothing to record")
        return EXIT_DEGRADED

    session_id = args.session_id or f"{datetime.now(tz=IST):%Y%m%d-%H%M%S}"
    rule(f"CAPTURING -- session {session_id}")
    log(f"running for {_human(duration)}, until about {window.stop:%H:%M} IST")
    log("Ctrl-C stops cleanly: buffers flush, manifests are written, and the raw")
    log("byte log is already on disk regardless.")
    log("")

    import capture  # noqa: PLC0415

    argv = [
        "--asof",
        window.day.isoformat(),
        "--session-id",
        session_id,
        "--duration",
        f"{duration:.0f}",
    ]
    if args.allow_stale_master:
        argv.append("--allow-stale-master")
    if args.spot is not None:
        argv += ["--spot", f"{args.spot}"]
    if args.channels:
        argv += ["--channels", args.channels]
    rc = capture.main(argv)

    rule("RECONCILIATION")
    log("The raw byte log is the primary output and Parquet is derived from it, so")
    log("the question that matters is whether they agree. Re-decoding the log")
    log("offline and comparing frame counts is what makes the log a safety net")
    log("rather than a comforting file.")
    log("")
    try:
        reconcile(session_id)
    except Exception as exc:  # noqa: BLE001 - never let reconciliation mask a good capture
        log(f"reconciliation failed: {type(exc).__name__}: {exc}")
        log("the capture itself is unaffected -- the raw log and Parquet are on disk")

    if rc == EXIT_OK:
        log("")
        log("session complete and clean.")
    return rc


# --- reconciliation -----------------------------------------------------------

# Which offline decoder replays which channel's raw log. `chain` is plain JSON
# rather than a binary frame stream, so it is counted, not decoded.
_DECODERS = {
    "depth": protocol.depth_decoder,
    "feed": protocol.market_feed_decoder,
}


def _capture_roots(config_path: Path = CAPTURE_CONFIG_PATH) -> tuple[Path, Path]:
    cfg = yaml.safe_load(config_path.read_text()) or {}
    session_cfg = cfg.get("session") or {}
    raw = REPO_ROOT / session_cfg.get("raw_root", "data/tier_a/raw")
    pq = REPO_ROOT / session_cfg.get("parquet_root", "data/tier_a/parquet")
    return raw, pq


def reconcile(session_id: str) -> dict:
    """Replay a session's raw log, re-decode it, and compare with what was stored.

    Returns a summary dict, and prints a per-channel table. The comparison is not
    decorative: the raw log exists precisely because the decoder was written
    against synthetic fixtures, and this is the check that says whether the bytes
    on disk can still be recovered if the decoder turns out to have been wrong.
    """
    raw_root, pq_root = _capture_roots()
    summary: dict = {"session_id": session_id, "channels": {}}

    stored_rows: dict[str, int] = {}
    stored_skips: dict[str, dict] = {}
    for man in store.read_manifests(pq_root):
        sid = str(man.get("session_id") or "")
        if not sid.startswith(session_id):
            continue
        table = man.get("table") or man.get("name")
        if table is None:
            continue
        key = f"{sid}:{table}"
        stored_rows[key] = int(man.get("rows_written") or 0)
        stored_skips[key] = dict(man.get("skipped_frames") or {})

    header = f"  {'channel':8} {'records':>9} {'bytes':>12} {'frames':>9}  status"
    log(header)
    log(f"  {'-' * 8} {'-' * 9} {'-' * 12} {'-' * 9}  {'-' * 30}")

    total_bytes = 0
    for channel in sorted({p.parent.parent.name for p in rawlog.find_parts(raw_root)}):
        parts = rawlog.find_parts(raw_root, channel=channel, session_id=session_id)
        if not parts:
            continue
        records = list(rawlog.replay(parts))
        nbytes = sum(len(r.payload) for r in records)
        total_bytes += nbytes

        frames = 0
        status = "counted (JSON, not a frame stream)"
        if channel in _DECODERS:
            decoder = _DECODERS[channel]()
            for rec in records:
                frames += len(decoder.feed(rec.payload))
            st = decoder.stats
            problems = []
            if st.unknown_codes:
                problems.append(f"unknown codes {dict(st.unknown_codes)}")
            if st.length_mismatches:
                problems.append(f"length mismatches {dict(st.length_mismatches)}")
            if st.padding_anomalies:
                problems.append(f"{st.padding_anomalies} padding anomalies")
            if st.unsorted_sides:
                problems.append(f"{st.unsorted_sides} unsorted sides")
            if st.partial_carries:
                # Not a problem: a chunk boundary mid-frame is normal on a stream.
                pass
            status = "clean re-decode" if not problems else "; ".join(problems)

        log(f"  {channel:8} {len(records):>9,} {nbytes:>12,} {frames:>9,}  {status}")
        summary["channels"][channel] = {
            "records": len(records),
            "bytes": nbytes,
            "frames_redecoded": frames,
            "status": status,
        }

    if not summary["channels"]:
        log(f"  no raw log parts found for session {session_id}")
        return summary

    log("")
    log("  stored Parquet rows, from the session manifests:")
    for key in sorted(stored_rows):
        skips = stored_skips.get(key) or {}
        extra = f"   skipped {skips}" if skips else ""
        log(f"    {key:44} {stored_rows[key]:>9,} rows{extra}")
    summary["stored_rows"] = stored_rows
    log("")
    log(f"  total payload bytes recoverable offline: {total_bytes:,}")
    log("  (payload only -- the on-disk log is larger by its per-record header,")
    log("   which carries the two arrival clocks and the length prefix)")
    return summary


# --- what exists offline, probed rather than asserted -------------------------


@dataclass(frozen=True)
class Stage:
    """One stage of the offline pipeline, and how to tell whether it exists yet.

    `available` is *probed* by importing rather than declared in a table, so this
    listing cannot go stale. When Phase 3 lands `bnfmm.sim.fills.QueueFill`, the
    row flips to available with no edit to this file -- which is the only kind of
    status report worth printing.
    """

    name: str
    module: str
    attr: str | None
    phase: str
    what_it_does: str

    @property
    def available(self) -> bool:
        try:
            mod = importlib.import_module(self.module)
        except Exception:  # noqa: BLE001 - an unimportable stage is an absent stage
            return False
        return self.attr is None or hasattr(mod, self.attr)


PIPELINE: tuple[Stage, ...] = (
    Stage("read", "bnfmm.data.store", "read_capture", "1a done",
          "load captured depth and quote rows back in arrival order"),
    Stage("qa", "bnfmm.data.qa", "segments", "1c",
          "split into clean segments on clock gaps; exclude bad ones with a reason"),
    Stage("book", "bnfmm.book.reconstruct", "replay", "2",
          "rebuild the 20-level book and decompose dqty into adds/cancels/trades"),
    Stage("fairvalue", "bnfmm.fairvalue.parity", "implied_forward", "2",
          "microprice, put-call-parity forward, basis filter, smile fit"),
    Stage("fills", "bnfmm.sim.fills", "QueueFill", "3",
          "the fill simulator: naive, touch and queue, both cancellation bounds"),
    Stage("quoter", "bnfmm.strategy.quoter", "Quoter", "4",
          "fixed-spread baseline and Avellaneda-Stoikov with inventory skew"),
    Stage("metrics", "bnfmm.analysis.metrics", "huang_stoll", "5",
          "effective/realized spread, adverse selection, markouts, Sharpe with CI"),
)


# --- session discovery --------------------------------------------------------


@dataclass
class Recorded:
    session_id: str
    trading_days: set[date] = field(default_factory=set)
    rows: dict[str, int] = field(default_factory=dict)
    raw_bytes: int = 0
    raw_parts: int = 0
    channels: set[str] = field(default_factory=set)
    connect_test: bool = False
    started_by: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def day_label(self) -> str:
        real = sorted(d for d in self.trading_days if d.year > 1970)
        if not real:
            return "unknown"
        return real[0].isoformat() if len(real) == 1 else f"{real[0]}..{real[-1]}"


def discover(*, include_connect_tests: bool = False) -> list[Recorded]:
    """Every session on disk, newest first.

    Sessions are grouped by session id rather than by date directory. That is
    deliberate: a writer that captured no rows has no clock to date itself by and
    falls back to 1970-01-01, so grouping by directory would split one real
    session across two dates and invent a 1970 session that never happened.
    """
    raw_root, pq_root = _capture_roots()
    found: dict[str, Recorded] = {}

    def get(sid: str) -> Recorded:
        return found.setdefault(sid, Recorded(session_id=sid))

    for man in store.read_manifests(pq_root):
        sid_full = str(man.get("session_id") or "")
        if not sid_full:
            continue
        # Writer manifests use "<session>-<channel>"; the capture-level one uses
        # the bare session id. The bare 15-char stamp is the grouping key.
        base = sid_full[:15]
        rec = get(base)
        table = man.get("table") or man.get("name")
        if table is not None:
            key = f"{sid_full.removeprefix(base).lstrip('-') or 'main'}/{table}"
            rec.rows[key] = int(man.get("rows_written") or 0)
        if man.get("connect_test"):
            rec.connect_test = True
        if man.get("started_by"):
            rec.started_by = str(man["started_by"])
        for ch in man.get("channels") or []:
            if isinstance(ch, str):
                rec.channels.add(ch)

    for part in rawlog.find_parts(raw_root):
        # <raw_root>/<channel>/date=YYYY-MM-DD/<session>-NNNNN.bnrl
        sid = part.name.split("-")[0] + "-" + part.name.split("-")[1]
        rec = get(sid[:15])
        rec.raw_parts += 1
        rec.raw_bytes += part.stat().st_size
        rec.channels.add(part.parent.parent.name)
        try:
            rec.trading_days.add(date.fromisoformat(part.parent.name.removeprefix("date=")))
        except ValueError:
            pass

    sessions = sorted(found.values(), key=lambda r: r.session_id, reverse=True)
    if not include_connect_tests:
        sessions = [s for s in sessions if not s.connect_test]
    return sessions


# --- analyse mode -------------------------------------------------------------


def _print_inventory(sessions: list[Recorded]) -> None:
    log(f"  {'#':>3}  {'session':16} {'trading day':12} {'kind':13} {'rows':>10} {'raw bytes':>13}"
        f"  channels")
    log(f"  {'-' * 3}  {'-' * 16} {'-' * 12} {'-' * 13} {'-' * 10} {'-' * 13}  {'-' * 18}")
    for i, s in enumerate(sessions, start=1):
        kind = "connect-test" if s.connect_test else "market"
        log(
            f"  {i:>3}  {s.session_id:16} {s.day_label:12} {kind:13} "
            f"{s.total_rows:>10,} {s.raw_bytes:>13,}  {','.join(sorted(s.channels))}"
        )


def mode_analyse(args: argparse.Namespace) -> int:
    rule("WORK ON A RECORDED SESSION")
    sessions = discover(include_connect_tests=args.include_connect_tests)
    if not sessions:
        log("no recorded sessions found.")
        raw_root, pq_root = _capture_roots()
        log(f"  raw log root : {raw_root}")
        log(f"  parquet root : {pq_root}")
        log("")
        log("Connect tests are hidden by default; pass --include-connect-tests to see them.")
        log("Record a session first:  .venv/bin/python main.py --mode record")
        return EXIT_DEGRADED

    log(f"{len(sessions)} session(s) on disk:")
    log("")
    _print_inventory(sessions)
    log("")

    chosen: Recorded | None = None
    if args.session:
        matches = [s for s in sessions if s.session_id.startswith(args.session)]
        if not matches:
            log(f"no session matches {args.session!r}")
            return EXIT_FAILED
        chosen = matches[0]
    elif args.date:
        matches = [s for s in sessions if args.date in s.day_label]
        if not matches:
            log(f"no session recorded on {args.date}")
            return EXIT_FAILED
        chosen = matches[0]
    elif sys.stdin.isatty():
        raw = input("  select a session by number (or date YYYY-MM-DD, blank = newest): ").strip()
        if not raw:
            chosen = sessions[0]
        elif raw.isdigit() and 1 <= int(raw) <= len(sessions):
            chosen = sessions[int(raw) - 1]
        else:
            matches = [s for s in sessions if raw in s.day_label or s.session_id.startswith(raw)]
            if not matches:
                log(f"nothing matches {raw!r}")
                return EXIT_FAILED
            chosen = matches[0]
    else:
        chosen = sessions[0]
        log(f"no TTY and no --session/--date: taking the newest, {chosen.session_id}")

    return run_offline_routine(chosen)


def run_offline_routine(rec: Recorded) -> int:
    """The offline routine for one recorded session.

    Runs every stage that exists, then states plainly which do not and which
    phase builds them. Stopping with an honest inventory is the correct
    behaviour here: a market maker that silently ran on a stub would produce
    numbers, and numbers from a stub are worse than no numbers.
    """
    rule(f"SESSION {rec.session_id}  ({rec.day_label})")
    log(f"channels     {', '.join(sorted(rec.channels)) or 'none'}")
    log(f"raw log      {rec.raw_parts} part(s), {rec.raw_bytes:,} bytes")
    for key in sorted(rec.rows):
        log(f"stored rows  {key:28} {rec.rows[key]:>10,}")
    if rec.connect_test:
        log("NOTE  this session is a connect test, not a market session.")
    log("")

    log("-- stage 1: reconcile raw log against stored Parquet " + "-" * 20)
    reconcile(rec.session_id)

    log("")
    log("-- stage 2: what the recorded data contains " + "-" * 29)
    describe_contents(rec)

    log("")
    log("-- stage 3: the market-making routine " + "-" * 35)
    missing = [s for s in PIPELINE if not s.available]
    present = [s for s in PIPELINE if s.available]
    log(f"  {'stage':11} {'phase':9} {'status':13} what it does")
    log(f"  {'-' * 11} {'-' * 9} {'-' * 13} {'-' * 40}")
    for s in PIPELINE:
        log(f"  {s.name:11} {s.phase:9} {'available' if s.available else 'NOT BUILT':13} "
            f"{s.what_it_does}")
    log("")
    if missing:
        log(f"  {len(present)} of {len(PIPELINE)} stages exist. The market maker cannot run yet:")
        log(f"  the first missing stage is '{missing[0].name}' (phase {missing[0].phase}).")
        log("")
        log("  This is the honest answer rather than a stub that returns numbers. The")
        log("  data captured today is not blocked by it -- capture and analysis were")
        log("  deliberately decoupled precisely because a trading session happens once")
        log("  and the simulator can be written any time. See BRIEFING.md, Phase 3.")
        return EXIT_DEGRADED

    log("  every stage is available; running the market maker")
    engine = importlib.import_module("bnfmm.sim.engine")
    return int(engine.run_recorded_session(rec.session_id))  # type: ignore[attr-defined]


def describe_contents(rec: Recorded) -> None:
    """Load the stored tables for this session's trading day and report what is in them.

    Filtered by date rather than by session id, because the Parquet layout
    partitions on `date=` and `security_id=` and carries the session only inside
    the part *filename*. One session per trading day is the operating plan, so
    date is the right granularity -- but if a day ever holds two sessions this
    counts both, which is why the day is printed alongside.
    """
    _, pq_root = _capture_roots()
    days = sorted(d for d in rec.trading_days if d.year > 1970) or None
    if days:
        log(f"  (filtered to date(s) {', '.join(d.isoformat() for d in days)})")
    for table in ("depth", "quotes"):
        try:
            tbl = store.read_capture(pq_root, table, dates=days)
        except Exception as exc:  # noqa: BLE001 - an unreadable table is the finding
            log(f"  {table:8} unreadable: {type(exc).__name__}: {exc}")
            continue
        if tbl.num_rows == 0:
            log(f"  {table:8} 0 rows")
            continue
        cols = tbl.column_names
        ids = set(tbl.column("security_id").to_pylist()) if "security_id" in cols else set()
        walls = tbl.column("recv_wall_ns").to_pylist() if "recv_wall_ns" in cols else []
        span = ""
        if walls:
            first = datetime.fromtimestamp(min(walls) / 1e9, tz=IST)
            last = datetime.fromtimestamp(max(walls) / 1e9, tz=IST)
            span = f", {first:%H:%M:%S} -> {last:%H:%M:%S} IST"
        log(f"  {table:8} {tbl.num_rows:,} rows over {len(ids)} instrument(s){span}")


# --- menu and CLI -------------------------------------------------------------

_MENU = """
  BNFMM -- BANKNIFTY market-making simulation

  1) Record a live market session
       Waits for the next market open if the market is not open yet, runs
       preflight, captures every channel through the close, then reconciles the
       raw byte log against what was stored. Safe to start the night before.

  2) Work on data already recorded
       Lists the sessions on disk, takes a pick, and runs the offline routine
       against it.

  q) quit
"""


def choose_mode() -> str:
    print(_MENU, flush=True)
    while True:
        raw = input("  choice [1/2/q]: ").strip().lower()
        if raw in {"1", "record", "r"}:
            return "record"
        if raw in {"2", "analyse", "analyze", "a"}:
            return "analyse"
        if raw in {"q", "quit", "exit"}:
            return "quit"
        print("  please answer 1, 2 or q", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=("record", "analyse"),
        help="skip the menu. Required when there is no TTY (systemd, nohup, cron).",
    )
    p.add_argument(
        "--calendar",
        default=str(CALENDAR_PATH),
        help="override config/market_calendar.yaml. Useful for a non-standard "
        "session (a muhurat session, a shortened day) and for exercising the "
        "wait-then-capture path end to end without waiting for an actual open.",
    )
    p.add_argument("--session-id", help="record: override the generated session id")
    p.add_argument("--channels", help="record: comma-separated subset of channels")
    p.add_argument(
        "--spot",
        type=float,
        help="record: centre the strike ladder on this level instead of the "
        "circuit-band bootstrap. The bootstrap reads the instrument master, whose "
        "price bands are set from the PREVIOUS close, so after a large overnight "
        "gap the recorded band is off-centre by gap/strike_step strikes and the "
        "20-level depth ladder loses its upper or lower wing. Passed through to "
        "both the preflight dry run and the capture itself, so the ladder printed "
        "at 08:48 is the one that gets recorded.",
    )
    p.add_argument(
        "--allow-stale-master",
        action="store_true",
        help="record: proceed with an instrument master older than a day",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="record: capture even if preflight reported a problem",
    )
    p.add_argument("--session", help="analyse: session id prefix to work on")
    p.add_argument("--date", help="analyse: trading date (YYYY-MM-DD) to work on")
    p.add_argument(
        "--include-connect-tests",
        action="store_true",
        help="analyse: also list connect-test sessions, which are hidden by default",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    mode = args.mode
    if mode is None:
        if not sys.stdin.isatty():
            print(
                "main.py needs a choice and there is no terminal to ask on.\n"
                "Pass --mode record or --mode analyse.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        mode = choose_mode()

    try:
        if mode == "quit":
            return EXIT_OK
        if mode == "record":
            return mode_record(args)
        return mode_analyse(args)
    except KeyboardInterrupt:
        log("")
        log("interrupted. Anything already written is on disk and is still valid:")
        log("the raw byte log is append-only and Parquet parts are flushed atomically.")
        return EXIT_DEGRADED


if __name__ == "__main__":
    raise SystemExit(main())
