"""Freeze the ``security_id`` -> instrument mapping for captures already on disk.

WHY THIS EXISTS
    A captured row is keyed only by ``security_id`` -- it is both a Parquet column
    and the partition key. Nothing in the Parquet, and nothing in the session
    manifests written before 2026-09-02, records which *strike* that integer is.
    The only mapping lives in ``data/reference/api-scrip-master-detailed.csv``,
    which ``instruments.ensure_master`` overwrites in place (``tmp.replace(path)``,
    no archive) and from which the vendor prunes expired contracts.

    The August BANKNIFTY series expired 2026-08-25. The 2026-08-24 and 2026-08-25
    captures -- 1 DTE and 0 DTE, the two highest-value sessions -- are therefore
    one master refresh away from becoming 20-level books of unknown strikes. They
    survive today only because the master on disk is stale (written 2026-08-23),
    which is luck, not a design.

    This script converts that luck into a fact. For every captured session it
    resolves the subscribed ids against whatever master is present and writes an
    immutable sidecar next to the session manifest. Afterwards each capture is
    self-describing and no longer depends on a mutable external file.

    ``universe.py`` now embeds the same mapping in new manifests, so this is a
    one-time backfill for sessions recorded before that change -- and a safety net
    worth re-running whenever a fresh master might still hold an expiring series.

WHERE THE SUBSCRIBED IDS COME FROM
    The ``*-capture.json`` manifest when there is one. A hard-killed run never
    writes it (``report()`` runs after the ``try/finally``), so for those runs the
    ids are read from the ``BNRLOG1`` preamble every raw-log part file opens
    with -- the same list, written at subscribe time rather than at exit. The
    sidecar records which source it used. BRIEFING §13.11(a).

HOW IDS ARE RESOLVED
    Security ids are unique only within ``(EXCH_ID, SEGMENT)``: 25 is both Adani
    Enterprises (NSE/E) and Nifty Bank (NSE/I). Resolution is therefore
    restricted to NSE F&O (``D``) and NSE indices (``I``), which share no ids in
    the master, and indices go through ``universe.index_contracts`` so they are
    recorded as ``IDX_I`` rather than defaulting to ``NSE_FNO``. BRIEFING §13.11(b).

USAGE
    python scripts/snapshot_contracts.py              # every captured day
    python scripts/snapshot_contracts.py --date 2026-08-24
    python scripts/snapshot_contracts.py --check      # report only, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from bnfmm.data.instruments import _to_contract  # noqa: E402
from bnfmm.data.rawlog import RawLogError, read_preamble  # noqa: E402
from bnfmm.data.universe import _contract_manifest, index_contracts  # noqa: E402

SESSIONS = _REPO_ROOT / "data" / "tier_a" / "parquet" / "sessions"
RAW = _REPO_ROOT / "data" / "tier_a" / "raw"
# Channels whose preamble carries a subscription list. The chain poller's is
# always empty: it is keyed by expiry, not by security id.
RAW_CHANNELS = ("depth", "feed")
MASTER = _REPO_ROOT / "data" / "reference" / "api-scrip-master-detailed.csv"
ARCHIVE = _REPO_ROOT / "data" / "reference" / "archive"


def load_master() -> pd.DataFrame:
    if not MASTER.exists():
        raise SystemExit(f"instrument master not found: {MASTER}")
    return pd.read_csv(MASTER, low_memory=False)


def session_ids(manifest: dict) -> dict[str, list[int]]:
    """The subscribed ids per channel, from a capture manifest."""
    out: dict[str, list[int]] = {}
    for name, ch in (manifest.get("channels") or {}).items():
        meta = ((ch.get("rawlog") or {}).get("metadata") or {})
        ids = meta.get("security_ids") or []
        if ids:
            out[name] = [int(i) for i in ids]
    return out


def preamble_ids(day: str) -> dict[str, dict[str, list[int]]]:
    """``{session_id: {channel: ids}}`` from the raw-log preambles of one day.

    Every part file of a run repeats the same list, so the first readable part
    per (session, channel) is enough; later parts are checked for agreement.
    """
    out: dict[str, dict[str, list[int]]] = {}
    for chan in RAW_CHANNELS:
        for part in sorted((RAW / chan / f"date={day}").glob("*.bnrl")):
            try:
                header = read_preamble(part)
            except RawLogError as exc:
                print(f"  {day} {part.name}: unreadable preamble, skipped ({exc})")
                continue
            session = header.get("session_id") or part.name.rsplit("-", 1)[0]
            ids = [int(i) for i in header.get("security_ids") or []]
            if not ids:
                continue
            seen = out.setdefault(session, {}).setdefault(chan, ids)
            if seen != ids:
                raise SystemExit(f"{part}: preamble ids differ from an earlier part of {session}")
    return out


def resolve(master: pd.DataFrame, ids: list[int]) -> tuple[dict[int, dict], list[int]]:
    """Resolve ids to contract dicts within NSE F&O and NSE indices only."""
    nse = master[master.EXCH_ID.astype(str) == "NSE"]
    seg = nse.SEGMENT.astype(str)
    sid = pd.to_numeric(nse.SECURITY_ID, errors="coerce")
    wanted = set(ids)
    found: dict[int, dict] = {}

    idx_ids = sorted(set(sid[(seg == "I") & sid.isin(wanted)].astype(int)))
    for c in index_contracts(master, idx_ids) if idx_ids else []:
        found[c.security_id] = _contract_manifest(c)

    for _, row in nse[(seg == "D") & sid.isin(wanted)].iterrows():
        key = int(row["SECURITY_ID"])
        if key in found:  # D and I are disjoint today; fail loudly if that changes
            raise SystemExit(f"security_id {key} is in both NSE/D and NSE/I -- ambiguous")
        try:
            found[key] = _contract_manifest(_to_contract(row))
        except Exception as exc:  # a malformed master row must not lose the rest
            found[key] = {"security_id": key, "error": f"{type(exc).__name__}: {exc}"}
    missing = [i for i in ids if i not in found]
    return found, missing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", action="append", help="limit to this trading date (repeatable)")
    p.add_argument("--check", action="store_true", help="report only, write nothing")
    p.add_argument("--force", action="store_true", help="overwrite an existing sidecar")
    args = p.parse_args(argv)

    master = load_master()
    master_mtime = datetime.fromtimestamp(MASTER.stat().st_mtime, timezone.utc)
    print(f"master: {MASTER.name}  rows={len(master)}  mtime={master_mtime.isoformat()}")

    days = sorted(
        {d.name.split("=", 1)[1] for d in SESSIONS.glob("date=*")}
        | {d.name.split("=", 1)[1] for c in RAW_CHANNELS for d in (RAW / c).glob("date=*")}
    )
    days = [d for d in days if not d.startswith("1970")]  # epoch-dated sidecar dirs, no runs
    if args.date:
        days = [d for d in days if d in set(args.date)]
    if not days:
        raise SystemExit("no captured sessions found")

    total_missing = 0
    written = 0
    for day in days:
        runs: dict[str, tuple[str, dict[str, list[int]]]] = {}
        for mpath in sorted((SESSIONS / f"date={day}").glob("*-capture.json")):
            manifest = json.loads(mpath.read_text())
            session = manifest.get("session_id") or mpath.name.split("-capture")[0]
            runs[session] = ("manifest", session_ids(manifest))
        for session, per_channel in preamble_ids(day).items():
            if session in runs and runs[session][1]:
                continue
            runs[session] = ("rawlog_preamble", per_channel)

        for session, (id_source, per_channel) in sorted(runs.items()):
            if not per_channel:
                print(f"  {day} {session}: no subscribed ids found, skipped")
                continue
            mpath = SESSIONS / f"date={day}" / f"{session}-capture.json"

            out: dict[str, object] = {
                "session_id": session,
                "trading_date": day,
                "id_source": id_source,
                "source_master": MASTER.name,
                "source_master_mtime": master_mtime.isoformat(),
                "snapshot_written": datetime.now(timezone.utc).isoformat(),
                "channels": {},
            }
            summary = []
            for chan, ids in sorted(per_channel.items()):
                found, missing = resolve(master, ids)
                total_missing += len(missing)
                out["channels"][chan] = {
                    "requested": len(ids),
                    "resolved": len(found),
                    "missing": missing,
                    "contracts": [found[i] for i in ids if i in found],
                }
                flag = f" MISSING={len(missing)}" if missing else ""
                summary.append(f"{chan} {len(found)}/{len(ids)}{flag}")

            dest = mpath.parent / f"{session}-contracts.json"
            summary.append(f"[{id_source}]")
            if args.check:
                print(f"  {day} {session}: {'; '.join(summary)}  (check only)")
                continue
            if dest.exists() and not args.force:
                print(f"  {day} {session}: sidecar exists, use --force  ({'; '.join(summary)})")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, indent=2, sort_keys=True))
            tmp.replace(dest)
            written += 1
            print(f"  {day} {session}: {'; '.join(summary)}  -> {dest.name}")

    if not args.check:
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        stamp = master_mtime.date().isoformat()
        slice_path = ARCHIVE / f"master-BANKNIFTY-{stamp}.csv"
        if not slice_path.exists() or args.force:
            und = master.UNDERLYING_SYMBOL.astype(str)
            is_index = (master.SEGMENT.astype(str) == "I") & master.SECURITY_ID.isin([25, 13])
            bn = master[und.str.contains("BANKNIFTY", na=False) | is_index]
            bn.to_csv(slice_path, index=False)
            print(f"archived BANKNIFTY master slice: {slice_path.name} ({len(bn)} rows)")
        else:
            print(f"archive already present: {slice_path.name}")

    print(f"\nsidecars written: {written}   unresolved ids across all sessions: {total_missing}")
    if total_missing:
        print("WARNING: unresolved ids mean those contracts are already lost from this master.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
