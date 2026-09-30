"""Tests for the raw byte log -- the safety net that makes tomorrow acceptable.

The thing under test is not "can it write a file". It is the claim the whole
capture plan rests on: **a raw log damaged by a crash, a corrupt block or a
suspended laptop still yields every undamaged record, and reports exactly what
it lost.** A reader that silently drops a corrupt record is worse than no reader,
because the resulting analysis looks complete. So most of these tests damage a
file on purpose and then assert on the accounting.
"""

from __future__ import annotations

import struct
import zlib
from datetime import date, datetime, timedelta

import pytest

from bnfmm.data import rawlog as rl


def wall_ns(
    *, year: int = 2026, month: int = 8, day: int = 24, hour: int = 9, minute: int = 15
) -> int:
    """A wall-clock nanosecond stamp at a given IST instant."""
    moment = datetime(year, month, day, hour, minute, tzinfo=rl.IST)
    return int(moment.timestamp() * rl.NS_PER_SECOND)


def write_records(tmp_path, payloads, *, channel="depth", session="20260824-090000", **kwargs):
    """Write payloads at one arrival instant and return (writer, path)."""
    writer = rl.RawLogWriter(tmp_path, channel, session, **kwargs)
    base = wall_ns()
    for i, payload in enumerate(payloads):
        writer.append(payload, recv_wall_ns=base + i * 1000, recv_mono_ns=i * 1000)
    writer.close()
    return writer


def first_record_offset(data: bytes) -> int:
    """Byte offset of the first record, i.e. past the magic and metadata line.

    Needed because the preamble JSON contains the literal ``BNRL`` in its
    ``record_magic`` field, so searching the whole file for the record magic finds
    the preamble first. That is not a flaw -- it is why the reader anchors on the
    terminated metadata line rather than on a magic scan.
    """
    return data.index(b"\n", len(rl.FILE_MAGIC)) + 1


def record_offsets(data: bytes) -> list[int]:
    """Offsets of every record in a part file, walking the length prefixes."""
    offsets = []
    offset = first_record_offset(data)
    while offset < len(data):
        magic, _, _, _, payload_len, _ = rl._HEADER.unpack_from(data, offset)
        assert magic == rl.RECORD_MAGIC
        offsets.append(offset)
        offset += rl.HEADER_SIZE + payload_len
    return offsets


#: Offset of the ``payload_len`` field within a record header.
LEN_FIELD = 4 + 8 + 8 + 8


# --- the round trip -----------------------------------------------------------


def test_round_trip_preserves_bytes_order_and_both_clocks(tmp_path):
    payloads = [b"\x00\x01\x02", b"", b"x" * 500, bytes(range(256))]
    writer = write_records(tmp_path, payloads)

    records, stats = rl.read_channel(tmp_path, "depth")

    assert [r.payload for r in records] == payloads
    assert [r.seq for r in records] == [0, 1, 2, 3]
    assert [r.recv_mono_ns for r in records] == [0, 1000, 2000, 3000]
    assert records[0].recv_wall_ns == wall_ns()
    assert stats.records == 4
    assert stats.clean, stats.summary()
    assert writer.stats.records == 4


def test_empty_payload_is_a_record_not_a_no_op(tmp_path):
    """A zero-length frame is a fact about the feed and must survive the trip."""
    write_records(tmp_path, [b""])
    records, stats = rl.read_channel(tmp_path, "depth")
    assert len(records) == 1
    assert records[0].payload == b""
    assert stats.clean


def test_writer_opens_lazily_so_the_date_comes_from_the_first_record(tmp_path):
    """No clock is read in the writer, so an unused channel leaves no file at all."""
    writer = rl.RawLogWriter(tmp_path, "depth", "20260824-090000")
    assert writer.path is None
    assert rl.find_parts(tmp_path) == []
    writer.close()
    assert rl.find_parts(tmp_path) == []

    writer = rl.RawLogWriter(tmp_path, "depth", "20260824-090001")
    # A wall clock inside the writer would partition this record under today.
    writer.append(b"a", recv_wall_ns=wall_ns(year=2026, month=8, day=25), recv_mono_ns=1)
    assert writer.path is not None
    assert writer.path.parent.name == "date=2026-08-25"
    writer.close()


def test_path_layout_is_partitioned_and_part_numbered(tmp_path):
    writer = write_records(tmp_path, [b"a"], channel="feed", session="20260824-090000")
    path = writer.paths[0]
    assert path.parent.parent.name == "feed"
    assert path.parent.name == "date=2026-08-24"
    assert path.name == "20260824-090000-00000.bnrl"
    assert path.relative_to(tmp_path).parts[0] == "feed"


def test_preamble_describes_the_format_and_carries_metadata(tmp_path):
    """A part file must be decodable by someone who does not have this module."""
    writer = rl.RawLogWriter(
        tmp_path, "depth", "20260824-090000", metadata={"atm_strike": 57800, "spot": 57785.0}
    )
    writer.append(b"a", recv_wall_ns=wall_ns(), recv_mono_ns=1)
    writer.close()

    header = rl.read_preamble(writer.paths[0])
    assert header["channel"] == "depth"
    assert header["session_id"] == "20260824-090000"
    assert header["trading_date"] == "2026-08-24"
    assert header["part"] == 0
    assert header["first_seq"] == 0
    # The struct format and record magic are in the file, not only in the code.
    assert header["header_struct"] == rl._HEADER.format
    assert header["record_magic"] == "BNRL"
    assert header["atm_strike"] == 57800
    assert header["spot"] == 57785.0


def test_close_and_flush_are_idempotent(tmp_path):
    """Both run from a finally block, possibly twice, possibly before any append."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    writer.flush()  # nothing open yet
    writer.close()
    writer.close()
    assert writer.closed
    with pytest.raises(rl.RawLogError):
        writer.append(b"a", recv_wall_ns=wall_ns(), recv_mono_ns=1)


def test_context_manager_closes(tmp_path):
    with rl.RawLogWriter(tmp_path, "depth", "s1") as writer:
        writer.append(b"a", recv_wall_ns=wall_ns(), recv_mono_ns=1)
    assert writer.closed
    records, stats = rl.read_channel(tmp_path, "depth")
    assert len(records) == 1 and stats.clean


# --- refusing to lose data ----------------------------------------------------


def test_session_id_reuse_raises_rather_than_overwriting(tmp_path):
    """Reusing a session id is the one way a writer could destroy a captured day."""
    write_records(tmp_path, [b"first"], session="20260824-090000")
    again = rl.RawLogWriter(tmp_path, "depth", "20260824-090000")
    with pytest.raises(rl.RawLogError, match="must not be reused"):
        again.append(b"second", recv_wall_ns=wall_ns(), recv_mono_ns=1)

    # And the original day is untouched.
    records, _ = rl.read_channel(tmp_path, "depth")
    assert [r.payload for r in records] == [b"first"]


def test_oversize_payload_is_counted_not_raised(tmp_path):
    """An absurd chunk must cost that chunk, not the session."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    assert writer.append(b"a", recv_wall_ns=wall_ns(), recv_mono_ns=1) == 0
    seq = writer.append(b"x" * (rl.MAX_PAYLOAD + 1), recv_wall_ns=wall_ns(), recv_mono_ns=2)
    assert seq == -1
    assert writer.stats.oversize_rejected == 1
    # Sequence numbers are not consumed by a rejection, and capture continues.
    assert writer.append(b"b", recv_wall_ns=wall_ns(), recv_mono_ns=3) == 1
    writer.close()

    records, stats = rl.read_channel(tmp_path, "depth")
    assert [r.payload for r in records] == [b"a", b"b"]
    assert stats.clean


def test_extend_shares_one_arrival_instant_and_counts_writes(tmp_path):
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    n = writer.extend([b"a", b"b", b"c"], recv_wall_ns=wall_ns(), recv_mono_ns=7)
    writer.close()
    assert n == 3
    records, _ = rl.read_channel(tmp_path, "depth")
    assert {r.recv_wall_ns for r in records} == {wall_ns()}
    assert {r.recv_mono_ns for r in records} == {7}


def test_non_bytes_payload_is_a_programming_error(tmp_path):
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    with pytest.raises(TypeError):
        writer.append("text", recv_wall_ns=wall_ns(), recv_mono_ns=1)
    writer.close()


@pytest.mark.parametrize("bad", ["", "has space", "../escape", "a" * 65, "x/y"])
def test_channel_and_session_names_are_validated(tmp_path, bad):
    """These names become path components; traversal must be impossible."""
    with pytest.raises(ValueError):
        rl.RawLogWriter(tmp_path, bad, "s1")
    with pytest.raises(ValueError):
        rl.RawLogWriter(tmp_path, "depth", bad)


@pytest.mark.parametrize("kwargs", [{"rotate_bytes": 0}, {"flush_records": 0}, {"flush_bytes": -1}])
def test_writer_rejects_nonsense_configuration(tmp_path, kwargs):
    with pytest.raises(ValueError):
        rl.RawLogWriter(tmp_path, "depth", "s1", **kwargs)


# --- rotation and date rollover -----------------------------------------------


def test_rotation_repeats_the_preamble_and_replays_continuously(tmp_path):
    """Each part must stand alone: a lost part cannot make the others unreadable."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", rotate_bytes=200)
    base = wall_ns()
    for i in range(20):
        writer.append(bytes([i]) * 40, recv_wall_ns=base + i, recv_mono_ns=i)
    writer.close()

    assert writer.stats.rotations >= 3
    assert len(writer.paths) == writer.stats.rotations + 1
    for part, path in enumerate(writer.paths):
        header = rl.read_preamble(path)
        assert header["part"] == part
        assert header["channel"] == "depth"

    # first_seq lets a reader place a part without reading the ones before it.
    assert rl.read_preamble(writer.paths[0])["first_seq"] == 0
    assert rl.read_preamble(writer.paths[1])["first_seq"] > 0

    records, stats = rl.read_channel(tmp_path, "depth")
    assert [r.seq for r in records] == list(range(20))
    assert stats.files == len(writer.paths)
    assert stats.clean, stats.summary()


def test_find_parts_sorts_by_part_not_by_glob_order(tmp_path):
    """Replay order is load-bearing; there is no sequence number on the wire."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", rotate_bytes=120)
    base = wall_ns()
    for i in range(30):
        writer.append(b"y" * 40, recv_wall_ns=base + i, recv_mono_ns=i)
    writer.close()
    parts = rl.find_parts(tmp_path, channel="depth")
    assert len(parts) > 9, "need a two-digit part count to catch lexical sorting"
    assert parts == sorted(parts, key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def test_date_rollover_starts_a_new_partition_at_part_zero(tmp_path):
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    writer.append(b"mon", recv_wall_ns=wall_ns(day=24), recv_mono_ns=1)
    writer.append(b"tue", recv_wall_ns=wall_ns(day=25), recv_mono_ns=2)
    writer.close()

    assert writer.stats.date_rollovers == 1
    assert {p.parent.name for p in writer.paths} == {"date=2026-08-24", "date=2026-08-25"}
    assert all(p.name.endswith("-00000.bnrl") for p in writer.paths)

    mon, _ = rl.read_channel(tmp_path, "depth", dates=[date(2026, 8, 24)])
    tue, _ = rl.read_channel(tmp_path, "depth", dates=[date(2026, 8, 25)])
    assert [r.payload for r in mon] == [b"mon"]
    assert [r.payload for r in tue] == [b"tue"]


def test_trading_date_is_ist_not_utc(tmp_path):
    """A 09:15 IST open is 03:45 UTC the same day; UTC partitioning is off by one
    for anything before 05:30 IST and would split a session in the wrong place."""
    assert rl.trading_date(wall_ns(hour=9, minute=15)) == date(2026, 8, 24)
    assert rl.trading_date(wall_ns(hour=0, minute=30)) == date(2026, 8, 24)
    assert rl.trading_date(wall_ns(hour=23, minute=59)) == date(2026, 8, 24)


def test_flush_thresholds_bound_the_loss_window(tmp_path):
    """SIGKILL is in the threat model, so buffered bytes must not accumulate."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", flush_records=2, flush_bytes=1 << 30)
    base = wall_ns()
    for i in range(6):
        writer.append(b"z" * 10, recv_wall_ns=base + i, recv_mono_ns=i)
    assert writer.stats.flushes == 3
    writer.close()


def test_records_are_readable_before_close(tmp_path):
    """A live check at 09:20 must be able to read the log the session is writing."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", flush_records=1)
    base = wall_ns()
    for i in range(3):
        writer.append(b"live", recv_wall_ns=base + i, recv_mono_ns=i)
    records, stats = rl.read_channel(tmp_path, "depth")
    assert len(records) == 3 and stats.clean
    writer.close()


# --- damage: the tests the safety net exists for ------------------------------


def test_truncated_tail_is_reported_as_bytes_lost_not_silently_dropped(tmp_path):
    """SIGKILL mid-write. The undamaged prefix survives and the loss is counted."""
    writer = write_records(tmp_path, [b"a" * 30, b"b" * 30, b"c" * 30])
    path = writer.paths[0]
    data = path.read_bytes()
    path.write_bytes(data[:-20])  # last record loses its tail

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert [r.payload for r in records] == [b"a" * 30, b"b" * 30]
    assert stats.truncated_files == 1
    assert stats.truncated_tail_bytes == rl.HEADER_SIZE + 30 - 20
    assert not stats.clean


def test_truncation_inside_a_header_is_also_a_truncated_tail(tmp_path):
    writer = write_records(tmp_path, [b"a" * 30, b"b" * 30])
    path = writer.paths[0]
    data = path.read_bytes()
    path.write_bytes(data[: -(30 + 10)])  # cut into the second header

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert [r.payload for r in records] == [b"a" * 30]
    assert stats.truncated_files == 1
    assert stats.truncated_tail_bytes == rl.HEADER_SIZE - 10


def test_crc_error_is_counted_and_the_reader_resyncs_on_the_next_record(tmp_path):
    """A corrupt block costs one record, not the rest of the file.

    This is the property that distinguishes a length-prefixed stream that can be
    recovered from one that cannot: without a per-record magic to resynchronise
    on, a single flipped length field loses everything downstream of it.
    """
    writer = write_records(tmp_path, [b"aaaa", b"bbbb", b"cccc"])
    path = writer.paths[0]
    data = bytearray(path.read_bytes())
    # Corrupt a payload byte of the middle record; its CRC no longer matches.
    idx = data.index(b"bbbb")
    data[idx] = ord("X")
    path.write_bytes(bytes(data))

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert [r.payload for r in records] == [b"aaaa", b"cccc"]
    assert stats.crc_errors == 1
    assert stats.resyncs == 1
    assert stats.resync_skipped_bytes > 0
    assert stats.seq_gaps == 1, "the lost record must show up as a gap, not vanish"
    assert not stats.clean
    assert "crc" in stats.summary()


def test_corrupt_header_length_does_not_swallow_the_file(tmp_path):
    """A flipped length field is the worst case for a length-prefixed format."""
    writer = write_records(tmp_path, [b"aaaa", b"bbbb", b"cccc"])
    path = writer.paths[0]
    data = bytearray(path.read_bytes())
    second = record_offsets(bytes(data))[1]
    # Claim the second record is 1 MiB long. Without resync, everything after is lost.
    struct.pack_into("<I", data, second + LEN_FIELD, 1 << 20)
    path.write_bytes(bytes(data))

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    payloads = [r.payload for r in records]
    assert b"aaaa" in payloads
    assert b"cccc" in payloads, "resync must recover the tail"
    assert stats.resyncs >= 1
    assert not stats.clean


def test_oversize_declared_length_is_rejected_without_allocating(tmp_path):
    writer = write_records(tmp_path, [b"aaaa", b"bbbb"])
    path = writer.paths[0]
    data = bytearray(path.read_bytes())
    first = record_offsets(bytes(data))[0]
    struct.pack_into("<I", data, first + LEN_FIELD, rl.MAX_PAYLOAD + 1)
    path.write_bytes(bytes(data))

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert [r.payload for r in records] == [b"bbbb"]
    assert stats.oversize_headers == 1
    assert not stats.clean


def test_garbage_between_records_is_skipped_and_measured(tmp_path):
    """Filesystem damage need not be record-aligned."""
    writer = write_records(tmp_path, [b"aaaa", b"bbbb"])
    path = writer.paths[0]
    data = path.read_bytes()
    cut = data.index(b"bbbb") - rl.HEADER_SIZE
    path.write_bytes(data[:cut] + b"\xde\xad\xbe\xef" * 5 + data[cut:])

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert [r.payload for r in records] == [b"aaaa", b"bbbb"]
    assert stats.resyncs == 1
    assert stats.resync_skipped_bytes == 20
    assert stats.seq_gaps == 0, "no record was lost, only padding skipped"


def test_bad_file_magic_raises_because_it_is_not_our_file(tmp_path):
    """Damage inside a record is counted; a file that is not a raw log is an error."""
    path = tmp_path / "depth" / "date=2026-08-24" / "s1-00000.bnrl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a raw log at all")
    with pytest.raises(rl.RawLogError, match="bad file magic"):
        rl.read_preamble(path)
    with pytest.raises(rl.RawLogError):
        list(rl.iter_file(path))


def test_truncated_preamble_raises(tmp_path):
    path = tmp_path / "depth" / "date=2026-08-24" / "s1-00000.bnrl"
    path.parent.mkdir(parents=True)
    path.write_bytes(rl.FILE_MAGIC + b'{"channel":"depth"')  # no terminating newline
    with pytest.raises(rl.RawLogError, match="truncated"):
        rl.read_preamble(path)


def test_a_record_with_a_forged_crc_still_verifies(tmp_path):
    """Guards the CRC's coverage: it must cover the header, not only the payload.

    If the CRC were computed over the payload alone, rewriting a timestamp would
    go undetected -- and timestamps are the only clock the depth feed has.
    """
    writer = write_records(tmp_path, [b"payload"])
    path = writer.paths[0]
    data = bytearray(path.read_bytes())
    start = record_offsets(bytes(data))[0]
    struct.pack_into("<q", data, start + 4, wall_ns(hour=11))  # rewrite recv_wall_ns
    path.write_bytes(bytes(data))

    stats = rl.ScanStats()
    records = list(rl.iter_file(path, stats=stats))
    assert records == []
    assert stats.crc_errors == 1


def test_crc_matches_an_independent_computation(tmp_path):
    """Pin the on-disk CRC so a change to the format is a visible test failure."""
    writer = write_records(tmp_path, [b"abc"])
    data = writer.paths[0].read_bytes()
    start = record_offsets(data)[0]
    header = data[start : start + rl.HEADER_SIZE]
    stored = struct.unpack_from("<I", header, rl.HEADER_SIZE - 4)[0]
    expected = zlib.crc32(b"abc", zlib.crc32(header[rl._CRC_SLICE])) & 0xFFFFFFFF
    assert stored == expected


def test_seq_regression_across_parts_is_reported(tmp_path):
    """Two sessions' files read together must not look like one clean stream."""
    write_records(tmp_path, [b"a", b"b"], session="20260824-090000")
    write_records(tmp_path, [b"c", b"d"], session="20260824-091500")

    records, stats = rl.read_channel(tmp_path, "depth")
    assert len(records) == 4
    assert stats.seq_regressions == 1
    assert not stats.clean


def test_an_entirely_missing_part_is_detected_as_a_gap(tmp_path):
    """The failure this guards is a whole part going missing, which is silent.

    Sequence numbers continue across a rotation, so a lost part 00001 -- deleted,
    unreadable, or never copied off the laptop -- leaves parts 0 and 2 that are
    each internally perfect. Only a check that spans file boundaries can see the
    hole, and up to ``rotate_bytes`` of market data hides in it.
    """
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", rotate_bytes=150)
    base = wall_ns()
    for i in range(20):
        writer.append(b"p" * 40, recv_wall_ns=base + i, recv_mono_ns=i)
    writer.close()
    assert len(writer.paths) >= 3

    # A read of the intact session is clean...
    _, before = rl.read_channel(tmp_path, "depth")
    assert before.clean, before.summary()

    # ...and losing one part in the middle is not.
    writer.paths[1].unlink()
    records, after = rl.read_channel(tmp_path, "depth")
    assert after.seq_gaps == 1
    assert not after.clean
    assert "seq gaps" in after.summary()
    assert len(records) < before.records


def test_find_parts_filters_by_channel_date_and_session(tmp_path):
    write_records(tmp_path, [b"a"], channel="depth", session="s1")
    write_records(tmp_path, [b"b"], channel="feed", session="s1")
    write_records(tmp_path, [b"c"], channel="depth", session="s2")

    assert len(rl.find_parts(tmp_path)) == 3
    assert len(rl.find_parts(tmp_path, channel="depth")) == 2
    assert len(rl.find_parts(tmp_path, channel="depth", session_id="s2")) == 1
    assert rl.find_parts(tmp_path, dates=[date(2026, 8, 25)]) == []
    assert rl.find_parts(tmp_path / "nope") == []


def test_replay_concatenates_in_the_order_given(tmp_path):
    write_records(tmp_path, [b"a"], session="s1")
    write_records(tmp_path, [b"b"], session="s2")
    paths = rl.find_parts(tmp_path, channel="depth")
    assert [r.payload for r in rl.replay(paths)] == [b"a", b"b"]
    assert [r.payload for r in rl.replay(reversed(paths))] == [b"b", b"a"]


def test_stray_files_in_a_partition_are_ignored(tmp_path):
    """Manifests, notes and editor droppings live beside the parts."""
    writer = write_records(tmp_path, [b"a"])
    (writer.paths[0].parent / "notes.txt").write_text("do not read me")
    (writer.paths[0].parent / "s1-badpart.bnrl").write_text("nor me")
    records, stats = rl.read_channel(tmp_path, "depth")
    assert [r.payload for r in records] == [b"a"]
    assert stats.clean


def test_record_size_accounting(tmp_path):
    record = rl.RawRecord(seq=0, recv_wall_ns=1, recv_mono_ns=2, payload=b"abcd")
    assert record.record_bytes == rl.HEADER_SIZE + 4

    writer = write_records(tmp_path, [b"abcd", b"ef"])
    _, stats = rl.read_channel(tmp_path, "depth")
    assert stats.payload_bytes == 6
    assert stats.record_bytes == 2 * rl.HEADER_SIZE + 6
    assert writer.stats.payload_bytes == 6
    # file_bytes includes the preamble, so it exceeds the record bytes.
    assert writer.stats.file_bytes > stats.record_bytes


def test_manifest_is_json_ready_and_names_every_part(tmp_path):
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", rotate_bytes=150, metadata={"spot": 1.5})
    base = wall_ns()
    for i in range(10):
        writer.append(b"q" * 40, recv_wall_ns=base + i, recv_mono_ns=i)
    writer.close()

    manifest = writer.manifest()
    assert manifest["channel"] == "depth"
    assert manifest["trading_date"] == "2026-08-24"
    assert manifest["records"] == 10
    assert manifest["files"] == len(writer.paths) == manifest["rotations"] + 1
    assert len(manifest["paths"]) == manifest["files"]
    assert manifest["metadata"] == {"spot": 1.5}
    import json

    json.dumps(manifest)  # must not raise: it goes into the session manifest


# --- the module's independence, which is a design constraint -------------------


def test_rawlog_imports_nothing_third_party():
    """The safety net must survive a pyarrow failure.

    ``store.py`` imports pyarrow at module scope, so if this module imported
    ``store`` -- or anything else that does -- a broken pyarrow install would take
    the raw log down with the Parquet path, and the whole argument for having a
    raw log would collapse. The duplicated ``IST``/``trading_date`` helpers are
    the deliberate price of that, and this test is what keeps the price paid.
    """
    import ast
    from pathlib import Path

    source = Path(rl.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imported.add(f"RELATIVE:{node.module}")
            elif node.module:
                imported.add(node.module.split(".")[0])

    stdlib = {
        "__future__", "dataclasses", "datetime", "json", "os", "re",
        "struct", "typing", "zlib", "pathlib", "collections.abc",
    }
    assert imported <= stdlib, f"raw log gained a dependency: {sorted(imported - stdlib)}"


def test_no_clock_is_read_anywhere_in_the_module():
    """Arrival timestamps are passed in, so replayed and live writes are identical.

    A clock read inside the writer would make the trading-date partition depend on
    when the record was *written* rather than when it *arrived*, which is wrong at
    a date boundary and untestable everywhere else. Checked on the parsed syntax
    tree rather than on the text, so the prose in the docstrings is free to
    discuss clocks.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(rl.__file__).read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)

    for forbidden in ("time", "time_ns", "monotonic", "monotonic_ns", "now", "today", "utcnow"):
        assert forbidden not in called, f"rawlog.py calls {forbidden}()"


def test_trading_date_of_a_negative_or_zero_stamp_does_not_crash():
    """Defensive: a decoder bug could hand through a nonsense timestamp, and the
    raw log must record it rather than refuse the record."""
    assert rl.trading_date(0) == datetime(1970, 1, 1, tzinfo=rl.IST).date()
    assert isinstance(rl.trading_date(-1), date)


def test_channel_dir_and_part_path_agree(tmp_path):
    day = date(2026, 8, 24)
    assert rl.part_path(tmp_path, "depth", day, "s1", 7).parent == rl.channel_dir(
        tmp_path, "depth", day
    )
    assert rl.part_path(tmp_path, "depth", day, "s1", 7).name == "s1-00007.bnrl"


def test_write_stats_summary_mentions_damage(tmp_path):
    """A rejected chunk is silent by construction, so the summary must say it.

    ``append`` returns -1 rather than raising, on the principle that one absurd
    frame must not end a session. That is only defensible if the count is said out
    loud somewhere, and the end-of-session line is the only place an operator
    looks. Asserted case-insensitively -- the wording is shouted on purpose but
    the test is about the fact being present, not its capitalisation.
    """
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    writer.append(b"x" * (rl.MAX_PAYLOAD + 1), recv_wall_ns=wall_ns(), recv_mono_ns=1)
    writer.append(b"ok", recv_wall_ns=wall_ns(), recv_mono_ns=2)
    writer.close()
    summary = writer.stats.summary().lower()
    assert "oversize" in summary
    assert "1 oversize" in summary

    # And the converse: a clean session must not cry wolf, or the warning stops
    # being read.
    clean = rl.RawLogWriter(tmp_path, "depth", "s2")
    clean.append(b"ok", recv_wall_ns=wall_ns(), recv_mono_ns=1)
    clean.close()
    assert "oversize" not in clean.stats.summary().lower()
    assert "rollover" not in clean.stats.summary().lower()


def test_a_full_session_shape_round_trips(tmp_path):
    """End to end at session scale: three channels, rotation, and a clean read.

    Cheap insurance against an interaction that only appears once several parts,
    several channels and several thousand records exist at the same time.
    """
    base = wall_ns()
    writers = {}
    for channel, size in (("depth", 332), ("feed", 162), ("chain", 4096)):
        w = rl.RawLogWriter(tmp_path, channel, "20260824-090000", rotate_bytes=64 * 1024)
        for i in range(400):
            w.append(bytes([i % 256]) * size, recv_wall_ns=base + i * 1_000_000, recv_mono_ns=i)
        w.close()
        writers[channel] = w

    total = 0
    for channel, w in writers.items():
        records, stats = rl.read_channel(tmp_path, channel)
        assert len(records) == 400
        assert stats.clean, f"{channel}: {stats.summary()}"
        assert stats.files == len(w.paths)
        total += stats.records
    assert total == 1200
    # Reading with no channel filter finds every part across all three channels.
    assert len(rl.find_parts(tmp_path)) == sum(len(w.paths) for w in writers.values())


def test_monotonic_clock_survives_a_wall_clock_step(tmp_path):
    """The pair of clocks is the whole point: an NTP step must stay detectable.

    ``store.py`` has ``ClockWitness`` for this; the raw log's job is simply not to
    normalise, reorder or reject the evidence.
    """
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    base = wall_ns()
    writer.append(b"before", recv_wall_ns=base, recv_mono_ns=1_000_000_000)
    # Wall clock jumps backwards 5 minutes; monotonic advances 1 ms.
    writer.append(
        b"after",
        recv_wall_ns=base - 300 * rl.NS_PER_SECOND,
        recv_mono_ns=1_001_000_000,
    )
    writer.close()

    records, stats = rl.read_channel(tmp_path, "depth")
    assert [r.payload for r in records] == [b"before", b"after"]
    assert records[1].recv_wall_ns < records[0].recv_wall_ns
    assert records[1].recv_mono_ns > records[0].recv_mono_ns
    assert stats.clean, "a clock step is data, not damage"


def test_a_day_boundary_wall_step_does_not_lose_the_record(tmp_path):
    """A backwards NTP step across midnight triggers a rollover into a past date.

    Ugly, but the record must land somewhere readable rather than be dropped.
    """
    writer = rl.RawLogWriter(tmp_path, "depth", "s1")
    writer.append(b"tue", recv_wall_ns=wall_ns(day=25, hour=9), recv_mono_ns=1)
    writer.append(b"mon", recv_wall_ns=wall_ns(day=24, hour=9), recv_mono_ns=2)
    writer.close()
    assert writer.stats.date_rollovers == 1
    all_records = list(rl.replay(rl.find_parts(tmp_path, channel="depth")))
    assert {r.payload for r in all_records} == {b"tue", b"mon"}


def test_rotation_boundary_is_checked_before_the_write_not_after(tmp_path):
    """Parts may exceed rotate_bytes by at most one record, which bounds the loss
    from a single corrupt file to one part plus one record."""
    writer = rl.RawLogWriter(tmp_path, "depth", "s1", rotate_bytes=500)
    base = wall_ns()
    for i in range(40):
        writer.append(b"w" * 100, recv_wall_ns=base + i, recv_mono_ns=i)
    writer.close()
    limit = 500 + rl.HEADER_SIZE + 100
    for path in writer.paths[:-1]:
        assert path.stat().st_size <= limit + len(rl.FILE_MAGIC) + 512


def test_iter_file_accepts_a_string_path(tmp_path):
    writer = write_records(tmp_path, [b"a"])
    assert [r.payload for r in rl.iter_file(str(writer.paths[0]))] == [b"a"]
    assert rl.read_preamble(str(writer.paths[0]))["channel"] == "depth"
