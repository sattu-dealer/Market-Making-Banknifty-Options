"""Append-only log of raw feed bytes, written before anything is decoded.

This exists for one reason: **the market happens once a day and cannot be
re-run.** Every other component in this repo can be rewritten on any evening;
a capture session cannot. The decoders in ``protocol.py`` were built against
``tests/fixtures/packets.py`` -- a faithful reading of the vendor SDK, and a
faithful reading is still a reading. If they are wrong about real bytes, that
is discovered *during* the session, and by then the session is gone.

So ``capture.py`` writes every received chunk here, verbatim, before handing it
to a decoder. A decoder bug then costs a re-run of an offline script instead of
a trading day.

Three consequences of taking that seriously:

- **Stdlib only.** ``store.py`` imports pyarrow at module scope; if pyarrow
  raises on this machine, the Parquet path dies. The safety net must not die
  with it, so nothing here imports a third-party package -- including anything
  else in ``bnfmm.data``. The handful of duplicated lines below (``IST``,
  ``trading_date``, the session-id pattern) are duplicated deliberately.
- **Damage is bounded and reported, never silent.** Each record carries its own
  magic and a CRC32 over its header and payload. A corrupt record is *counted*
  and skipped by resynchronising on the next magic, so one bad write costs one
  record rather than the rest of the file. A truncated tail -- the expected
  shape of a SIGKILL or a power loss -- is detected and reported as bytes lost,
  not raised as a parse error.
- **No clock, no socket.** Timestamps arrive as arguments, exactly as in
  ``store.py``, which is what makes both writers testable without mocking time.

Durability policy is chosen for the actual threat model, which is a laptop that
suspends or gets killed, not a machine that loses power: buffered writes are
handed to the OS every ``flush_records`` records or ``flush_bytes`` bytes, so a
process death loses nothing, while ``fsync`` stays off by default because
paying a disk round trip per flush during a fast open is a worse trade than the
small window it closes. ``fsync=True`` is available for anyone who disagrees.

File layout, mirroring the partitioning ``store.py`` already uses so a session
is greppable across both outputs::

    <root>/<channel>/date=YYYY-MM-DD/<session_id>-NNNNN.bnrl

Wire format, little-endian throughout::

    preamble   b"BNRLOG1\\n" + <json metadata> + b"\\n"
    record     b"BNRL" | wall_ns i64 | mono_ns i64 | seq u64 | len u32 | crc u32 | payload
               \\_________________________ 36 bytes __________________________/

``crc`` is a CRC32 of the 28 header bytes between the magic and the crc field
itself, followed by the payload -- so a flipped bit in a timestamp is caught,
not just a flipped bit in the data.
"""

from __future__ import annotations

import json
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

#: Deliberately duplicated from ``store.py``: see the module docstring. India
#: has observed a fixed +05:30 with no daylight saving since 1945.
IST = timezone(timedelta(hours=5, minutes=30))

NS_PER_SECOND = 1_000_000_000

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Channel names are used as directory components, so they get the same
#: restriction as session ids.
CHANNEL_RE = SESSION_ID_RE

FILE_MAGIC = b"BNRLOG1\n"

RECORD_MAGIC = b"BNRL"

_HEADER = struct.Struct("<4sqqQII")

HEADER_SIZE = _HEADER.size  # 36

#: Offsets of the CRC-covered slice of the header (everything after the magic,
#: up to but excluding the crc field itself).
_CRC_SLICE = slice(len(RECORD_MAGIC), HEADER_SIZE - 4)

#: A WebSocket frame larger than this is corruption, not data. Dhan's largest
#: documented message is 332 bytes; TCP coalescing can hand us far more than
#: that in one ``recv``, but not megabytes.
MAX_PAYLOAD = 1 << 24  # 16 MiB

#: Roll to a new part file at roughly this size. Large enough that rotation is
#: rare, small enough that a single corrupt file is a bounded loss and that
#: files stay copyable.
DEFAULT_ROTATE_BYTES = 256 * 1024 * 1024

FILE_SUFFIX = ".bnrl"

_PART_RE = re.compile(r"^(?P<session>.+)-(?P<part>\d{5})" + re.escape(FILE_SUFFIX) + r"$")


class RawLogError(Exception):
    """A raw log could not be opened or its preamble could not be read.

    Deliberately *not* raised for corrupt or truncated records: those are the
    expected failure modes of an unattended capture and are counted in
    :class:`ScanStats` instead. Raising on them would mean one bad byte
    discards a session, which is the outcome this module exists to prevent.
    """


def trading_date(recv_wall_ns: int) -> date:
    """Calendar date in IST of a wall-clock nanosecond timestamp.

    Duplicated from ``store.py`` so that both outputs of one session land under
    the same ``date=`` partition without this module importing pyarrow.
    """
    return datetime.fromtimestamp(recv_wall_ns / NS_PER_SECOND, tz=IST).date()


def _validate_name(value: str, *, what: str) -> str:
    if not CHANNEL_RE.match(value):
        raise ValueError(f"{what} {value!r} must match {CHANNEL_RE.pattern}")
    return value


def channel_dir(root: Path | str, channel: str, day: date) -> Path:
    """Directory holding one channel's parts for one trading date."""
    return Path(root) / _validate_name(channel, what="channel") / f"date={day.isoformat()}"


def part_path(root: Path | str, channel: str, day: date, session_id: str, part: int) -> Path:
    """Path of one part file. Parts are numbered from zero, zero-padded to five."""
    _validate_name(session_id, what="session id")
    return channel_dir(root, channel, day) / f"{session_id}-{part:05d}{FILE_SUFFIX}"


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One received chunk, exactly as it came off the socket.

    ``payload`` is *not* one protocol frame -- a single ``recv()`` may carry
    several frames or a fragment of one. Framing is the decoder's job; this is
    the arrival record.
    """

    seq: int
    recv_wall_ns: int
    recv_mono_ns: int
    payload: bytes

    @property
    def record_bytes(self) -> int:
        """On-disk footprint including the header. ``len(payload)`` is the rest."""
        return HEADER_SIZE + len(self.payload)


@dataclass(slots=True)
class WriteStats:
    """What the writer did. Goes into the session manifest verbatim."""

    records: int = 0
    payload_bytes: int = 0
    file_bytes: int = 0
    files: int = 0
    flushes: int = 0
    fsyncs: int = 0
    rotations: int = 0
    date_rollovers: int = 0
    oversize_rejected: int = 0

    def summary(self) -> str:
        line = (
            f"{self.records} records, {self.payload_bytes} payload bytes, "
            f"{self.file_bytes} file bytes across {self.files} file(s), "
            f"{self.flushes} flushes, {self.rotations} rotations"
        )
        # Counted-not-raised only works if the count is eventually said out loud.
        # These two are silent by construction, so the end-of-session line is the
        # only place an operator can learn they happened.
        if self.oversize_rejected:
            line += f"; {self.oversize_rejected} OVERSIZE CHUNKS REJECTED"
        if self.date_rollovers:
            line += f"; {self.date_rollovers} date rollovers"
        return line


class RawLogWriter:
    """Append raw chunks to a rotating, self-describing log.

    The first :meth:`append` opens the file, so the trading date comes from the
    first record rather than from a clock read here. That keeps the "no clock in
    the writer" rule intact and means an empty channel leaves no empty file.

    Not thread-safe, and not asyncio-safe across ``await`` boundaries; each
    channel in ``capture.py`` owns one writer and appends from one task.
    """

    def __init__(
        self,
        root: Path | str,
        channel: str,
        session_id: str,
        *,
        metadata: dict | None = None,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
        flush_records: int = 64,
        flush_bytes: int = 1 << 20,
        fsync: bool = False,
    ) -> None:
        if rotate_bytes <= 0:
            raise ValueError("rotate_bytes must be positive")
        if flush_records <= 0:
            raise ValueError("flush_records must be positive")
        if flush_bytes <= 0:
            raise ValueError("flush_bytes must be positive")

        self.root = Path(root)
        self.channel = _validate_name(channel, what="channel")
        self.session_id = _validate_name(session_id, what="session id")
        self.metadata = dict(metadata or {})
        self.rotate_bytes = rotate_bytes
        self.flush_records = flush_records
        self.flush_bytes = flush_bytes
        self.fsync = fsync

        self.stats = WriteStats()
        self._fh = None
        self._path: Path | None = None
        self._part = 0
        self._day: date | None = None
        self._seq = 0
        self._file_bytes = 0
        self._since_flush_records = 0
        self._since_flush_bytes = 0
        self._paths: list[Path] = []
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> RawLogWriter:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def path(self) -> Path | None:
        """Path currently being written, or ``None`` before the first append."""
        return self._path

    @property
    def paths(self) -> tuple[Path, ...]:
        """Every part written by this writer, in order."""
        return tuple(self._paths)

    @property
    def next_seq(self) -> int:
        """Sequence number the next appended record will receive."""
        return self._seq

    def _open(self, day: date) -> None:
        path = part_path(self.root, self.channel, day, self.session_id, self._part)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise RawLogError(
                f"{path} already exists; a session id must not be reused for one channel"
            )
        preamble = FILE_MAGIC + self._preamble_json(day) + b"\n"
        # Buffering is left to Python's default block buffering and forced by
        # _maybe_flush; a line-buffered or unbuffered handle would turn every
        # append into a syscall.
        fh = open(path, "xb")
        try:
            fh.write(preamble)
        except BaseException:
            fh.close()
            raise
        self._fh = fh
        self._path = path
        self._day = day
        self._file_bytes = len(preamble)
        self._since_flush_records = 0
        self._since_flush_bytes = len(preamble)
        self._paths.append(path)
        self.stats.files += 1
        self.stats.file_bytes += len(preamble)

    def _preamble_json(self, day: date) -> bytes:
        header = {
            "format": FILE_MAGIC.decode().strip(),
            "channel": self.channel,
            "session_id": self.session_id,
            "trading_date": day.isoformat(),
            "part": self._part,
            "header_struct": _HEADER.format,
            "record_magic": RECORD_MAGIC.decode(),
            "first_seq": self._seq,
            **self.metadata,
        }
        return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()

    def close(self) -> None:
        """Flush and close. Idempotent, and safe to call from a finally block."""
        if self._closed:
            return
        self._closed = True
        if self._fh is not None:
            try:
                self.flush()
            finally:
                self._fh.close()
                self._fh = None

    # -- writing -----------------------------------------------------------

    def append(self, payload: bytes, *, recv_wall_ns: int, recv_mono_ns: int) -> int:
        """Append one chunk and return its sequence number.

        Rejects an oversized payload by counting it rather than raising: a
        capture that dies because one chunk was absurd is worse than a capture
        that records having seen one absurd chunk. Returns ``-1`` in that case.
        """
        if self._closed:
            raise RawLogError("writer is closed")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError(f"payload must be bytes-like, got {type(payload).__name__}")
        payload = bytes(payload)
        if len(payload) > MAX_PAYLOAD:
            self.stats.oversize_rejected += 1
            return -1

        day = trading_date(recv_wall_ns)
        if self._fh is None:
            self._open(day)
        elif day != self._day:
            # NSE closes long before midnight IST, so this is a safety valve
            # rather than an expected path -- but a log spanning two date
            # partitions would break every reader that filters on date=.
            self._roll(day, rollover=True)
        elif self._file_bytes >= self.rotate_bytes:
            self._roll(day, rollover=False)

        seq = self._seq
        header = _HEADER.pack(RECORD_MAGIC, recv_wall_ns, recv_mono_ns, seq, len(payload), 0)
        crc = zlib.crc32(header[_CRC_SLICE])
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        record = header[: HEADER_SIZE - 4] + struct.pack("<I", crc) + payload

        assert self._fh is not None
        self._fh.write(record)

        self._seq = seq + 1
        written = len(record)
        self._file_bytes += written
        self._since_flush_records += 1
        self._since_flush_bytes += written
        self.stats.records += 1
        self.stats.payload_bytes += len(payload)
        self.stats.file_bytes += written
        self._maybe_flush()
        return seq

    def extend(self, payloads: Iterable[bytes], *, recv_wall_ns: int, recv_mono_ns: int) -> int:
        """Append several chunks sharing one arrival instant. Returns the count."""
        n = 0
        for payload in payloads:
            if self.append(payload, recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns) >= 0:
                n += 1
        return n

    def _roll(self, day: date, *, rollover: bool) -> None:
        self.flush()
        assert self._fh is not None
        self._fh.close()
        self._fh = None
        if rollover:
            self._part = 0
            self.stats.date_rollovers += 1
        else:
            self._part += 1
            self.stats.rotations += 1
        self._open(day)

    def _maybe_flush(self) -> None:
        if (
            self._since_flush_records >= self.flush_records
            or self._since_flush_bytes >= self.flush_bytes
        ):
            self.flush()

    def flush(self) -> None:
        """Hand buffered bytes to the OS, and to the disk if ``fsync`` is set."""
        if self._fh is None:
            return
        self._fh.flush()
        self.stats.flushes += 1
        if self.fsync:
            os.fsync(self._fh.fileno())
            self.stats.fsyncs += 1
        self._since_flush_records = 0
        self._since_flush_bytes = 0

    # -- reporting ---------------------------------------------------------

    def manifest(self) -> dict:
        """JSON-ready summary for the session manifest."""
        return {
            "channel": self.channel,
            "session_id": self.session_id,
            "trading_date": self._day.isoformat() if self._day else None,
            "paths": [str(p) for p in self._paths],
            "records": self.stats.records,
            "payload_bytes": self.stats.payload_bytes,
            "file_bytes": self.stats.file_bytes,
            "files": self.stats.files,
            "flushes": self.stats.flushes,
            "fsyncs": self.stats.fsyncs,
            "rotations": self.stats.rotations,
            "date_rollovers": self.stats.date_rollovers,
            "oversize_rejected": self.stats.oversize_rejected,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ScanStats:
    """What a read found, including everything it could not read.

    Every field here is a thing that would otherwise be invisible. A reader that
    returns records without reporting that it skipped 3 corrupt ones and lost a
    412-byte tail is worse than no reader, because the resulting analysis looks
    complete.
    """

    files: int = 0
    records: int = 0
    payload_bytes: int = 0
    record_bytes: int = 0
    crc_errors: int = 0
    header_errors: int = 0
    oversize_headers: int = 0
    resyncs: int = 0
    resync_skipped_bytes: int = 0
    truncated_tail_bytes: int = 0
    truncated_files: int = 0
    seq_gaps: int = 0
    seq_regressions: int = 0

    #: Last sequence number seen, carried across files so that ``replay`` detects
    #: an entirely missing part. Without this the check resets at every file
    #: boundary, and losing part 00001 of a session -- up to ``rotate_bytes`` of
    #: market data -- reads as perfectly clean.
    last_seq: int | None = None

    @property
    def damaged_records(self) -> int:
        return self.crc_errors + self.header_errors + self.oversize_headers

    @property
    def clean(self) -> bool:
        """True when every byte in every file was accounted for as a record."""
        return (
            self.damaged_records == 0
            and self.resyncs == 0
            and self.truncated_tail_bytes == 0
            and self.seq_gaps == 0
            and self.seq_regressions == 0
        )

    def summary(self) -> str:
        parts = [f"{self.records} records from {self.files} file(s)"]
        if self.damaged_records:
            parts.append(
                f"{self.crc_errors} crc, {self.header_errors} header, "
                f"{self.oversize_headers} oversize"
            )
        if self.resyncs:
            parts.append(f"{self.resyncs} resyncs ({self.resync_skipped_bytes} bytes skipped)")
        if self.truncated_tail_bytes:
            parts.append(f"{self.truncated_tail_bytes} bytes truncated tail")
        if self.seq_gaps or self.seq_regressions:
            parts.append(f"{self.seq_gaps} seq gaps, {self.seq_regressions} regressions")
        if self.clean:
            parts.append("clean")
        return "; ".join(parts)


def read_preamble(path: Path | str) -> dict:
    """Read one part file's metadata line without reading its records."""
    path = Path(path)
    with open(path, "rb") as fh:
        magic = fh.read(len(FILE_MAGIC))
        if magic != FILE_MAGIC:
            raise RawLogError(f"{path}: bad file magic {magic!r}, expected {FILE_MAGIC!r}")
        line = fh.readline()
    if not line.endswith(b"\n"):
        raise RawLogError(f"{path}: preamble metadata line is truncated")
    try:
        header = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RawLogError(f"{path}: preamble is not valid JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise RawLogError(f"{path}: preamble is not a JSON object")
    return header


def _next_magic(buf: bytes, start: int) -> int:
    return buf.find(RECORD_MAGIC, start)


def iter_file(
    path: Path | str,
    *,
    stats: ScanStats | None = None,
) -> Iterator[RawRecord]:
    """Yield every readable record in one part file, in written order.

    Reads the whole file into memory: a part is bounded by ``rotate_bytes`` and
    offline replay is not memory-constrained, which buys straightforward
    resynchronisation. Corrupt regions are skipped by scanning forward to the
    next record magic and are counted in ``stats``; a truncated tail ends the
    iteration and is counted as lost bytes.
    """
    path = Path(path)
    read_preamble(path)  # raises on a file that is not a raw log at all
    data = path.read_bytes()
    if stats is not None:
        stats.files += 1

    # read_preamble has already established that the magic and the terminated
    # metadata line are both present, so this find cannot come back negative.
    offset = data.index(b"\n", len(FILE_MAGIC)) + 1
    end = len(data)

    while offset < end:
        remaining = end - offset
        if remaining < HEADER_SIZE:
            if stats is not None:
                stats.truncated_tail_bytes += remaining
                stats.truncated_files += 1
            return

        magic, wall_ns, mono_ns, seq, payload_len, crc = _HEADER.unpack_from(data, offset)
        if magic != RECORD_MAGIC:
            offset = _resync(data, offset, stats, kind="header")
            if offset < 0:
                return
            continue
        if payload_len > MAX_PAYLOAD:
            offset = _resync(data, offset, stats, kind="oversize")
            if offset < 0:
                return
            continue

        record_end = offset + HEADER_SIZE + payload_len
        if record_end > end:
            # Two different faults look identical at this point, because the CRC
            # covers the header and payload together and so cannot be verified
            # without the payload:
            #   (a) SIGKILL or power loss between writing a header and its
            #       payload -- a genuine truncated tail, and the common case;
            #   (b) a corrupted length field in the middle of an intact file.
            # What separates them is what comes after. If another record magic
            # appears later in the file then the file was not truncated here, the
            # length is simply wrong, and treating it as a tail would discard
            # every record after it -- the exact failure a resyncable format
            # exists to prevent.
            if _next_magic(data, offset + 1) < 0:
                if stats is not None:
                    stats.truncated_tail_bytes += remaining
                    stats.truncated_files += 1
                return
            offset = _resync(data, offset, stats, kind="header")
            if offset < 0:
                return
            continue

        payload = data[offset + HEADER_SIZE : record_end]
        want = zlib.crc32(data[offset + _CRC_SLICE.start : offset + _CRC_SLICE.stop])
        want = zlib.crc32(payload, want) & 0xFFFFFFFF
        if want != crc:
            offset = _resync(data, offset, stats, kind="crc")
            if offset < 0:
                return
            continue

        if stats is not None:
            stats.records += 1
            stats.payload_bytes += payload_len
            stats.record_bytes += HEADER_SIZE + payload_len
            # Carried on ``stats``, not in a local, so that one ScanStats threaded
            # through ``replay`` spans part boundaries.
            expect_seq = stats.last_seq
            if expect_seq is not None:
                if seq > expect_seq + 1:
                    stats.seq_gaps += 1
                elif seq <= expect_seq:
                    stats.seq_regressions += 1
            stats.last_seq = seq
        offset = record_end
        yield RawRecord(seq=seq, recv_wall_ns=wall_ns, recv_mono_ns=mono_ns, payload=payload)


def _resync(data: bytes, offset: int, stats: ScanStats | None, *, kind: str) -> int:
    """Count a damaged record and return the next plausible record offset, or -1."""
    if stats is not None:
        if kind == "crc":
            stats.crc_errors += 1
        elif kind == "oversize":
            stats.oversize_headers += 1
        else:
            stats.header_errors += 1
    nxt = _next_magic(data, offset + 1)
    if nxt < 0:
        if stats is not None:
            stats.truncated_tail_bytes += len(data) - offset
            stats.truncated_files += 1
        return -1
    if stats is not None:
        stats.resyncs += 1
        stats.resync_skipped_bytes += nxt - offset
    return nxt


def find_parts(
    root: Path | str,
    *,
    channel: str | None = None,
    dates: Sequence[date] | None = None,
    session_id: str | None = None,
) -> list[Path]:
    """List part files under ``root``, sorted by channel, date, session, part.

    Sorted rather than glob-ordered because replay order is load-bearing:
    ``recv_seq`` only reconstructs arrival order within a session, so parts must
    be concatenated in part order.
    """
    root = Path(root)
    if not root.exists():
        return []
    channels = [channel] if channel else sorted(p.name for p in root.iterdir() if p.is_dir())
    wanted = {d.isoformat() for d in dates} if dates else None

    found: list[tuple[str, str, str, int, Path]] = []
    for ch in channels:
        cdir = root / ch
        if not cdir.is_dir():
            continue
        for ddir in sorted(cdir.iterdir()):
            if not ddir.is_dir() or not ddir.name.startswith("date="):
                continue
            day = ddir.name.removeprefix("date=")
            if wanted is not None and day not in wanted:
                continue
            for path in ddir.iterdir():
                match = _PART_RE.match(path.name)
                if not match:
                    continue
                sess = match.group("session")
                if session_id is not None and sess != session_id:
                    continue
                found.append((ch, day, sess, int(match.group("part")), path))
    found.sort(key=lambda row: row[:4])
    return [row[4] for row in found]


def replay(
    paths: Iterable[Path | str],
    *,
    stats: ScanStats | None = None,
) -> Iterator[RawRecord]:
    """Yield records from several part files in the order given."""
    for path in paths:
        yield from iter_file(path, stats=stats)


def read_channel(
    root: Path | str,
    channel: str,
    *,
    dates: Sequence[date] | None = None,
    session_id: str | None = None,
) -> tuple[list[RawRecord], ScanStats]:
    """Read one channel's records eagerly, with the stats that describe the read.

    Returning the stats alongside the records rather than logging them is
    deliberate: a caller cannot use the data without being handed the evidence
    of what was damaged.
    """
    stats = ScanStats()
    paths = find_parts(root, channel=channel, dates=dates, session_id=session_id)
    records = list(replay(paths, stats=stats))
    return records, stats
