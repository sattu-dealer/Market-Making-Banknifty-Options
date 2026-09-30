"""Buffered Parquet capture, partitioned by trading date and instrument.

Written for an unattended laptop capture running for a month, which sets every
design choice here:

- **Each flush is an atomic rename.** A killed process, a full disk or a suspend
  mid-write leaves the previous complete file and a discarded temp file, never a
  truncated Parquet that a reader chokes on. The cost is one rename per flush.
- **One row per frame, never per book.** Bids and asks arrive as separate frames
  and pairing them is a *reconstruction* decision (``bnfmm.book``), not a storage
  one. Storing paired books would silently invent pairings for the frames whose
  partner never arrived -- exactly the case worth studying.
- **Both clocks on every row.** The depth feed carries no timestamp, so local
  arrival time is the only clock; wall clock alone cannot distinguish a real gap
  from an NTP step or a resume from sleep. ``recv_mono_ns`` is immune to both, and
  the writer accumulates the divergence between the two as it goes -- it is the
  only component that sees every row in arrival order, so measuring it anywhere
  else means a second pass.
- **``recv_seq`` exists because timestamps tie.** Every frame in one ``recv()``
  shares an arrival instant, because that is what was actually measured; inventing
  sub-chunk times would be fabrication. But sorting on a tied timestamp reorders
  frames arbitrarily, which makes book reconstruction non-deterministic. The
  sequence number is the real, recorded arrival order.

Frames that are not market data -- disconnects, unknown codes -- are recorded in
the session manifest rather than dropped. A capture that quietly discards what it
did not understand cannot be audited later.

Nothing here opens a socket or reads a clock. Timestamps are supplied by the
caller, which keeps the writer testable and keeps clock policy in one place
(``sim/clock.py``, phase 2).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .protocol import (
    DecodeStats,
    Depth5Level,
    DepthSide,
    Disconnect,
    Frame,
    Level,
    Side,
    Snapshot,
    UnknownFrame,
)

#: India has observed a fixed +05:30 with no daylight saving since 1945, so the
#: offset is exact and needs no tz database. The trading *date* must be the IST
#: calendar date: a UTC date happens to agree during regular NSE hours, but only
#: by accident of the 10:00 UTC close, and relying on that accident is a bug
#: waiting for a session that runs late.
IST = timezone(timedelta(hours=5, minutes=30))

NS_PER_SECOND = 1_000_000_000

#: Rejects anything that could escape the capture root or collide across runs.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def trading_date(recv_wall_ns: int) -> date:
    """The IST calendar date a wall-clock nanosecond timestamp falls in."""
    seconds, nanos = divmod(recv_wall_ns, NS_PER_SECOND)
    return datetime.fromtimestamp(seconds + nanos / NS_PER_SECOND, tz=IST).date()


def _temp_path(final: Path) -> Path:
    """Where a file is built before its atomic rename into place.

    Two properties, both of which matter for a capture that will be SIGKILLed at
    some point during a month of unattended running. The suffix is not
    ``.parquet``, so the reader's ``*.parquet`` glob cannot pick up a leftover --
    and a *fully written but unrenamed* temp file is the dangerous case, since it
    would parse cleanly and silently duplicate rows. The leading dot is the
    convention Arrow, Spark and Hive all treat as ignorable, so other readers skip
    it too.
    """
    return final.parent / f".{final.name}.tmp"


# --- schemas -------------------------------------------------------------------
# Levels are three parallel lists rather than a list of structs so that a reader
# wanting only prices does not pay for quantities. Padding is already stripped by
# the decoder, so list length *is* the book depth and needs no sentinel.

_LEVEL_FIELDS = [
    ("price", pa.list_(pa.float64())),
    ("quantity", pa.list_(pa.uint32())),
    ("orders", pa.list_(pa.uint32())),
]

_STAMP_FIELDS = [
    ("recv_wall_ns", pa.int64()),
    ("recv_mono_ns", pa.int64()),
    ("recv_seq", pa.int64()),
    ("security_id", pa.int32()),
    ("exchange_segment", pa.uint8()),
]

DEPTH_SCHEMA = pa.schema(
    [
        *_STAMP_FIELDS,
        ("side", pa.string()),
        *_LEVEL_FIELDS,
        # Decoder findings, stored so QA never has to re-derive them and so a
        # zero-anomaly capture is a measurement rather than an absence of code.
        ("padding_rows", pa.uint8()),
        ("anomalous_rows", pa.uint8()),
        ("interleaved_padding", pa.bool_()),
        ("is_sorted", pa.bool_()),
        # The depth header's trailing uint32, whose meaning on a 20-level frame is
        # unknown (DECISIONS #15). Stored unmodified so a month of real captures
        # can settle empirically what it holds.
        ("header_extra", pa.uint32()),
        ("declared_length", pa.int32()),
    ]
)

SNAPSHOT_SCHEMA = pa.schema(
    [
        *_STAMP_FIELDS,
        ("ltp", pa.float64()),
        ("last_quantity", pa.uint32()),
        # The only exchange-side clock in the system, at one-second resolution.
        ("last_trade_epoch", pa.int64()),
        ("average_price", pa.float64()),
        # Cumulative for the day; per-interval traded quantity is a difference.
        ("volume", pa.int64()),
        ("total_sell_quantity", pa.int64()),
        ("total_buy_quantity", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        # Null on a Quote packet, which omits open interest entirely. Nullable
        # rather than zero-filled: 0 is a legitimate open interest.
        ("open_interest", pa.int64()),
        ("oi_day_high", pa.int64()),
        ("oi_day_low", pa.int64()),
        # The five-level block, for cross-checking the 20-level decoder.
        ("bid_price", pa.list_(pa.float64())),
        ("bid_quantity", pa.list_(pa.uint32())),
        ("bid_orders", pa.list_(pa.uint32())),
        ("ask_price", pa.list_(pa.float64())),
        ("ask_quantity", pa.list_(pa.uint32())),
        ("ask_orders", pa.list_(pa.uint32())),
        ("msg_code", pa.uint8()),
    ]
)


def _depth_row(frame: DepthSide) -> dict[str, object]:
    return {
        "security_id": frame.security_id,
        "exchange_segment": frame.exchange_segment,
        "side": frame.side.value,
        "price": [lvl.price for lvl in frame.levels],
        "quantity": [lvl.quantity for lvl in frame.levels],
        "orders": [lvl.orders for lvl in frame.levels],
        "padding_rows": frame.padding_rows,
        "anomalous_rows": frame.anomalous_rows,
        "interleaved_padding": frame.interleaved_padding,
        "is_sorted": frame.is_sorted,
        "header_extra": frame.header_extra,
        "declared_length": frame.declared_length,
    }


def _snapshot_row(frame: Snapshot) -> dict[str, object]:
    return {
        "security_id": frame.security_id,
        "exchange_segment": frame.exchange_segment,
        "ltp": frame.ltp,
        "last_quantity": frame.last_quantity,
        "last_trade_epoch": frame.last_trade_epoch,
        "average_price": frame.average_price,
        "volume": frame.volume,
        "total_sell_quantity": frame.total_sell_quantity,
        "total_buy_quantity": frame.total_buy_quantity,
        "open": frame.open,
        "high": frame.high,
        "low": frame.low,
        "close": frame.close,
        "open_interest": frame.open_interest,
        "oi_day_high": frame.oi_day_high,
        "oi_day_low": frame.oi_day_low,
        "bid_price": [lvl.bid_price for lvl in frame.depth5],
        "bid_quantity": [lvl.bid_quantity for lvl in frame.depth5],
        "bid_orders": [lvl.bid_orders for lvl in frame.depth5],
        "ask_price": [lvl.ask_price for lvl in frame.depth5],
        "ask_quantity": [lvl.ask_quantity for lvl in frame.depth5],
        "ask_orders": [lvl.ask_orders for lvl in frame.depth5],
        "msg_code": frame.msg_code,
    }


@dataclass(frozen=True)
class TableSpec:
    """One physical table: its schema, and which frames belong in it."""

    name: str
    schema: pa.Schema
    frame_type: type
    row_of: Callable[[Frame], dict[str, object]]


DEPTH_TABLE = TableSpec("depth", DEPTH_SCHEMA, DepthSide, _depth_row)
SNAPSHOT_TABLE = TableSpec("quotes", SNAPSHOT_SCHEMA, Snapshot, _snapshot_row)
TABLES = {spec.name: spec for spec in (DEPTH_TABLE, SNAPSHOT_TABLE)}


# --- clock discipline ----------------------------------------------------------


@dataclass
class ClockWitness:
    """Divergence between wall clock and monotonic clock, accumulated per row.

    ``time.monotonic`` cannot go backwards and is unaffected by NTP steps or a
    suspend; wall clock is affected by both. Their *deltas* should agree, so a
    disagreement localises the damage to a specific pair of rows -- which is what
    lets a segment be excluded from latency analysis rather than a whole session.

    A gap and a clock step look identical in wall clock alone. That is the entire
    reason both are recorded.
    """

    rows: int = 0
    wall_regressions: int = 0
    max_abs_skew_ns: int = 0
    max_skew_at_seq: int = -1
    total_abs_skew_ns: int = 0
    first_wall_ns: int | None = None
    last_wall_ns: int | None = None
    first_mono_ns: int | None = None
    last_mono_ns: int | None = None

    def observe(self, *, wall_ns: int, mono_ns: int, seq: int) -> None:
        if self.rows:
            d_wall = wall_ns - (self.last_wall_ns or 0)
            d_mono = mono_ns - (self.last_mono_ns or 0)
            if d_wall < 0:
                self.wall_regressions += 1
            skew = abs(d_wall - d_mono)
            self.total_abs_skew_ns += skew
            if skew > self.max_abs_skew_ns:
                self.max_abs_skew_ns = skew
                self.max_skew_at_seq = seq
        else:
            self.first_wall_ns = wall_ns
            self.first_mono_ns = mono_ns
        self.last_wall_ns = wall_ns
        self.last_mono_ns = mono_ns
        self.rows += 1

    @property
    def wall_span_ns(self) -> int:
        if self.first_wall_ns is None or self.last_wall_ns is None:
            return 0
        return self.last_wall_ns - self.first_wall_ns

    @property
    def mono_span_ns(self) -> int:
        if self.first_mono_ns is None or self.last_mono_ns is None:
            return 0
        return self.last_mono_ns - self.first_mono_ns

    @property
    def span_disagreement_ns(self) -> int:
        """End-to-end drift. Large means the session's wall clock cannot be trusted
        for durations, even if no single step was large."""
        return abs(self.wall_span_ns - self.mono_span_ns)

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "wall_regressions": self.wall_regressions,
            "max_abs_skew_ns": self.max_abs_skew_ns,
            "max_skew_at_seq": self.max_skew_at_seq,
            "total_abs_skew_ns": self.total_abs_skew_ns,
            "first_wall_ns": self.first_wall_ns,
            "last_wall_ns": self.last_wall_ns,
            "wall_span_ns": self.wall_span_ns,
            "mono_span_ns": self.mono_span_ns,
            "span_disagreement_ns": self.span_disagreement_ns,
        }


# --- the writer ----------------------------------------------------------------


@dataclass
class _Partition:
    rows: list[dict[str, object]] = field(default_factory=list)
    files: int = 0
    rows_written: int = 0


class CaptureWriter:
    """Buffers frames in memory and flushes whole Parquet files per partition.

    Layout, Hive-style so ``pyarrow.dataset`` can push date and instrument
    predicates down to the file list::

        <root>/<table>/date=YYYY-MM-DD/security_id=NNNNN/part-<session>-NNNNN.parquet
        <root>/sessions/date=YYYY-MM-DD/<session>-<table>.json

    Partitioning per instrument means a per-strike replay reads only that
    strike's files, which matters once a month of 10-strike capture exists.
    """

    def __init__(
        self,
        root: Path | str,
        spec: TableSpec,
        session_id: str,
        *,
        rows_per_file: int = 20_000,
        max_buffered_rows: int = 200_000,
    ) -> None:
        if not SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"session id {session_id!r} must match {SESSION_ID_RE.pattern} -- it "
                "becomes part of a file path"
            )
        if rows_per_file < 1 or max_buffered_rows < rows_per_file:
            raise ValueError("need 1 <= rows_per_file <= max_buffered_rows")

        self.root = Path(root)
        self.spec = spec
        self.session_id = session_id
        self.rows_per_file = rows_per_file
        self.max_buffered_rows = max_buffered_rows

        self._partitions: dict[tuple[date, int], _Partition] = {}
        self._buffered = 0
        self._seq = 0
        self._closed = False

        self.clock = ClockWitness()
        self.disconnects: list[dict[str, object]] = []
        self.skipped: dict[str, int] = {}
        self.decode_stats: DecodeStats | None = None
        self.notes: list[str] = []

    # -- ingest ---------------------------------------------------------------

    def append(self, frame: Frame, *, recv_wall_ns: int, recv_mono_ns: int) -> bool:
        """Buffer one frame. Returns whether it became a data row.

        Frames that are not this table's payload still leave a trace: disconnects
        with their arrival time, everything else as a count by type.
        """
        if self._closed:
            raise RuntimeError("writer is closed")

        seq = self._seq
        self._seq += 1
        self.clock.observe(wall_ns=recv_wall_ns, mono_ns=recv_mono_ns, seq=seq)

        if not isinstance(frame, self.spec.frame_type):
            self._record_skip(frame, recv_wall_ns=recv_wall_ns, seq=seq)
            return False

        row = self.spec.row_of(frame)
        row["recv_wall_ns"] = recv_wall_ns
        row["recv_mono_ns"] = recv_mono_ns
        row["recv_seq"] = seq

        key = (trading_date(recv_wall_ns), int(row["security_id"]))  # type: ignore[arg-type]
        part = self._partitions.setdefault(key, _Partition())
        part.rows.append(row)
        self._buffered += 1

        if len(part.rows) >= self.rows_per_file:
            self._flush_partition(key)
        elif self._buffered >= self.max_buffered_rows:
            self.flush()
        return True

    def extend(
        self, frames: Iterable[Frame], *, recv_wall_ns: int, recv_mono_ns: int
    ) -> int:
        """Buffer every frame from one ``recv()``, sharing its arrival timestamp.

        Sharing is correct rather than lazy: one read returned them all, so one
        instant is the honest measurement. ``recv_seq`` preserves their order.
        """
        return sum(
            self.append(f, recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns)
            for f in frames
        )

    def _record_skip(self, frame: Frame, *, recv_wall_ns: int, seq: int) -> None:
        if isinstance(frame, Disconnect):
            self.disconnects.append(
                {
                    "recv_wall_ns": recv_wall_ns,
                    "recv_seq": seq,
                    "reason_code": frame.reason_code,
                    "reason": frame.reason,
                    "is_fatal": frame.is_fatal,
                    "is_entitlement": frame.is_entitlement,
                }
            )
        label = (
            f"unknown_code_{frame.msg_code}"
            if isinstance(frame, UnknownFrame)
            else type(frame).__name__
        )
        self.skipped[label] = self.skipped.get(label, 0) + 1

    # -- output ---------------------------------------------------------------

    def partition_dir(self, day: date, security_id: int) -> Path:
        return self.root / self.spec.name / f"date={day.isoformat()}" / f"security_id={security_id}"

    def _flush_partition(self, key: tuple[date, int]) -> Path | None:
        part = self._partitions[key]
        if not part.rows:
            return None

        day, security_id = key
        directory = self.partition_dir(day, security_id)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"part-{self.session_id}-{part.files:05d}.parquet"
        tmp = _temp_path(final)

        table = pa.Table.from_pylist(part.rows, schema=self.spec.schema)
        pq.write_table(table, tmp, compression="zstd", store_schema=True)
        os.replace(tmp, final)  # atomic: readers see a whole file or no file

        part.rows_written += len(part.rows)
        part.files += 1
        self._buffered -= len(part.rows)
        part.rows = []
        return final

    def flush(self) -> list[Path]:
        """Write every buffered partition. Safe to call at any time."""
        written = [self._flush_partition(key) for key in list(self._partitions)]
        return [path for path in written if path is not None]

    def manifest(self) -> dict[str, object]:
        """Everything ``data/qa.py`` needs that is not in the data itself."""
        return {
            "session_id": self.session_id,
            "table": self.spec.name,
            "rows_written": sum(p.rows_written for p in self._partitions.values()),
            "rows_buffered": self._buffered,
            "frames_seen": self._seq,
            "partitions": [
                {
                    "date": day.isoformat(),
                    "security_id": security_id,
                    "rows": part.rows_written,
                    "files": part.files,
                }
                for (day, security_id), part in sorted(
                    self._partitions.items(), key=lambda kv: (kv[0][0], kv[0][1])
                )
            ],
            "clock": self.clock.as_dict(),
            "disconnects": self.disconnects,
            "skipped_frames": dict(sorted(self.skipped.items())),
            "decode_stats": self._decode_stats_dict(),
            "notes": self.notes,
        }

    def _decode_stats_dict(self) -> dict[str, object] | None:
        stats = self.decode_stats
        if stats is None:
            return None
        return {
            "chunks": stats.chunks,
            "bytes_in": stats.bytes_in,
            "frames": stats.frames,
            "partial_carries": stats.partial_carries,
            "by_code": {str(k): v for k, v in sorted(stats.by_code.items())},
            "unknown_codes": {str(k): v for k, v in sorted(stats.unknown_codes.items())},
            "length_mismatches": {
                str(k): v for k, v in sorted(stats.length_mismatches.items())
            },
            "padding_anomalies": stats.padding_anomalies,
            "unsorted_sides": stats.unsorted_sides,
            "clean": stats.clean,
            "summary": stats.summary(),
        }

    def write_manifest(self) -> Path:
        """One JSON per (session, table), atomically renamed like the data files.

        Dated by the first row's trading date, or by the earliest partition if the
        session produced no rows at all -- a session that captured nothing still
        needs a manifest, because "nothing arrived" is itself a QA finding.
        """
        if self.clock.first_wall_ns is not None:
            day = trading_date(self.clock.first_wall_ns)
        elif self._partitions:
            day = min(key[0] for key in self._partitions)
        else:
            day = date(1970, 1, 1)

        directory = self.root / "sessions" / f"date={day.isoformat()}"
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{self.session_id}-{self.spec.name}.json"
        tmp = _temp_path(final)
        tmp.write_text(json.dumps(self.manifest(), indent=2, sort_keys=False) + "\n")
        os.replace(tmp, final)
        return final

    def close(self) -> Path:
        """Flush, write the manifest, and refuse further appends. Idempotent."""
        if not self._closed:
            self.flush()
            self._closed = True
        return self.write_manifest()

    def __enter__(self) -> CaptureWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"CaptureWriter({self.spec.name}, session={self.session_id}, "
            f"frames={self._seq}, buffered={self._buffered})"
        )


def depth_writer(root: Path | str, session_id: str, **kwargs: object) -> CaptureWriter:
    return CaptureWriter(root, DEPTH_TABLE, session_id, **kwargs)  # type: ignore[arg-type]


def snapshot_writer(root: Path | str, session_id: str, **kwargs: object) -> CaptureWriter:
    return CaptureWriter(root, SNAPSHOT_TABLE, session_id, **kwargs)  # type: ignore[arg-type]


# --- reading back --------------------------------------------------------------


def read_capture(
    root: Path | str,
    table: str,
    *,
    dates: Sequence[date] | None = None,
    security_ids: Sequence[int] | None = None,
) -> pa.Table:
    """Read a capture back, sorted into true arrival order.

    Sorting by ``(recv_wall_ns, recv_seq)`` rather than timestamp alone is the
    point: frames from one ``recv()`` share a timestamp, so a timestamp-only sort
    permutes them and book reconstruction stops being reproducible.

    Returns an empty table with the right schema when nothing matches, so callers
    do not have to special-case a capture that produced no rows.
    """
    spec = TABLES.get(table)
    if spec is None:
        raise ValueError(f"unknown table {table!r}; known: {sorted(TABLES)}")

    base = Path(root) / spec.name
    if not base.exists():
        return spec.schema.empty_table()

    # An explicit file list rather than handing Arrow the directory. Two separate
    # defences, and it takes both: the leading dot in _temp_path is enough for Arrow
    # itself, which skips dotfiles, but directory discovery still reads anything else
    # sitting in the partition -- a copy of a part file silently doubles every row,
    # and a stray text file makes the read raise. Neither is hypothetical in a
    # directory an operator has been poking at.
    files = sorted(str(path) for path in base.glob("date=*/security_id=*/*.parquet"))
    if not files:
        return spec.schema.empty_table()

    # The partition keys are read from the directory names, so they are strings
    # until cast; declaring the schema keeps `date=` a real date.
    partitioning = ds.partitioning(
        pa.schema([("date", pa.date32()), ("security_id", pa.int32())]), flavor="hive"
    )
    dataset = ds.dataset(
        files, format="parquet", partitioning=partitioning, partition_base_dir=str(base)
    )

    filters = []
    if dates is not None:
        filters.append(ds.field("date").isin(list(dates)))
    if security_ids is not None:
        filters.append(ds.field("security_id").isin([int(s) for s in security_ids]))

    expression = None
    for clause in filters:
        expression = clause if expression is None else (expression & clause)

    scanned = dataset.to_table(filter=expression)
    if scanned.num_rows == 0:
        return spec.schema.empty_table()
    # `security_id` arrives from both the row and the directory name; keep the row's.
    columns = [name for name in spec.schema.names]
    scanned = scanned.select(columns)
    return scanned.sort_by([("recv_wall_ns", "ascending"), ("recv_seq", "ascending")])


def _levels(prices: Sequence[float], quantities: Sequence[int], orders: Sequence[int]):
    return tuple(
        Level(price=p, quantity=q, orders=o) for p, q, o in zip(prices, quantities, orders)
    )


def depth_sides(table: pa.Table) -> list[tuple[int, int, DepthSide]]:
    """Rehydrate ``(recv_wall_ns, recv_mono_ns, DepthSide)`` from a depth table.

    Round-tripping a frame through Parquet and back must be the identity, which
    is the one property a storage layer has to have and the one thing a schema
    review cannot confirm. ``tests/test_store.py`` asserts it frame by frame.
    """
    out = []
    for row in table.to_pylist():
        out.append(
            (
                row["recv_wall_ns"],
                row["recv_mono_ns"],
                DepthSide(
                    security_id=row["security_id"],
                    exchange_segment=row["exchange_segment"],
                    side=Side(row["side"]),
                    levels=_levels(row["price"], row["quantity"], row["orders"]),
                    padding_rows=row["padding_rows"],
                    anomalous_rows=row["anomalous_rows"],
                    interleaved_padding=row["interleaved_padding"],
                    header_extra=row["header_extra"],
                    declared_length=row["declared_length"],
                ),
            )
        )
    return out


def snapshots(table: pa.Table) -> list[tuple[int, int, Snapshot]]:
    """Rehydrate ``(recv_wall_ns, recv_mono_ns, Snapshot)`` from a quotes table."""
    out = []
    for row in table.to_pylist():
        depth5 = tuple(
            Depth5Level(
                bid_quantity=bq,
                ask_quantity=aq,
                bid_orders=bo,
                ask_orders=ao,
                bid_price=bp,
                ask_price=ap,
            )
            for bp, bq, bo, ap, aq, ao in zip(
                row["bid_price"],
                row["bid_quantity"],
                row["bid_orders"],
                row["ask_price"],
                row["ask_quantity"],
                row["ask_orders"],
            )
        )
        out.append(
            (
                row["recv_wall_ns"],
                row["recv_mono_ns"],
                Snapshot(
                    security_id=row["security_id"],
                    exchange_segment=row["exchange_segment"],
                    ltp=row["ltp"],
                    last_quantity=row["last_quantity"],
                    last_trade_epoch=row["last_trade_epoch"],
                    average_price=row["average_price"],
                    volume=row["volume"],
                    total_sell_quantity=row["total_sell_quantity"],
                    total_buy_quantity=row["total_buy_quantity"],
                    open=row["open"],
                    close=row["close"],
                    high=row["high"],
                    low=row["low"],
                    open_interest=row["open_interest"],
                    oi_day_high=row["oi_day_high"],
                    oi_day_low=row["oi_day_low"],
                    depth5=depth5,
                    msg_code=row["msg_code"],
                ),
            )
        )
    return out


def read_manifests(root: Path | str, *, session_id: str | None = None) -> list[dict]:
    """Every session manifest under ``root``, newest path last.

    ``data/qa.py`` reads these rather than re-deriving decode statistics, because
    bytes-in, partial carries and unknown codes are only visible at capture time.
    """
    base = Path(root) / "sessions"
    if not base.exists():
        return []
    out = []
    for path in sorted(base.glob("date=*/*.json")):
        if session_id is not None and not path.name.startswith(f"{session_id}-"):
            continue
        out.append(json.loads(path.read_text()))
    return out
