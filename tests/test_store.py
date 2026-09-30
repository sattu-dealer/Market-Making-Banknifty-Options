"""Tests for buffered Parquet capture. No network, no clock, no credentials.

The properties that matter, in order:

1. **Round-trip identity.** A frame written and read back must be the same frame,
   field for field, including the anomaly flags. This is the one property a
   storage layer has to have and the one a schema review cannot confirm.
2. **Arrival order is recoverable.** Frames from one ``recv()`` share a timestamp,
   so the ordering has to survive in ``recv_seq`` or replay is non-deterministic.
3. **Crash tolerance.** A month of unattended capture will be killed mid-write at
   some point. That must cost the current buffer and nothing else.
4. **Clock damage is detected, not absorbed.** A wall-clock step and a genuine gap
   are indistinguishable in wall clock alone.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bnfmm.data import protocol as p
from bnfmm.data import store

from .fixtures import packets as pk

SECOND = 1_000_000_000

#: 2026-08-18 09:15:00 IST -- a plausible session open, and the value every test
#: offsets from so the partition date is predictable.
OPEN_WALL_NS = int(
    datetime(2026, 8, 18, 9, 15, tzinfo=store.IST).timestamp() * SECOND
)
OPEN_MONO_NS = 42 * SECOND


def _depth(**kwargs) -> p.DepthSide:
    frames, rest, _ = p.decode_depth_frames(pk.depth_frame(**kwargs))
    assert rest == b""
    return frames[0]


def _snapshot(**kwargs) -> p.Snapshot:
    frames, rest, _ = p.decode_feed_frames(pk.full_frame(**kwargs))
    assert rest == b""
    return frames[0]


# --- 1. round-trip identity ----------------------------------------------------


def test_a_depth_frame_round_trips_exactly(tmp_path) -> None:
    original = _depth(levels=pk.BID_LEVELS, header_extra=7)
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(original, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    (wall, mono, restored), = store.depth_sides(store.read_capture(tmp_path, "depth"))
    assert wall == OPEN_WALL_NS
    assert mono == OPEN_MONO_NS
    assert restored == original, "field-for-field, including the level tuple"


def test_both_sides_of_a_book_round_trip_as_separate_rows(tmp_path) -> None:
    """Pairing bids with asks is a reconstruction decision, not a storage one --
    storing paired books would invent a partner for a frame that arrived alone."""
    frames, _, _ = p.decode_depth_frames(pk.depth_book())
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.extend(frames, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    restored = [side for _, _, side in store.depth_sides(store.read_capture(tmp_path, "depth"))]
    assert restored == list(frames)
    assert [s.side for s in restored] == [p.Side.BID, p.Side.ASK]


def test_the_anomaly_flags_survive_the_round_trip(tmp_path) -> None:
    """A capture whose QA columns are recomputed on read would silently re-derive
    them from already-filtered levels and always report clean."""
    rows = ((999.95, 90, 3), (0.0, 0, 0), (999.85, 300, 7), (1000.00, 0, 0))
    body = b"".join(pk.struct.pack(p.DEPTH_LEVEL_FMT, *row) for row in rows)
    body += pk.struct.pack(p.DEPTH_LEVEL_FMT, 0.0, 0, 0) * 16
    original = _depth(levels=(), body=body)
    assert original.interleaved_padding and original.anomalous_rows == 1

    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(original, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    (_, _, restored), = store.depth_sides(store.read_capture(tmp_path, "depth"))
    assert restored == original
    assert restored.interleaved_padding
    assert restored.anomalous_rows == 1
    assert not restored.is_clean


def test_an_empty_book_round_trips_as_an_empty_list(tmp_path) -> None:
    """Illiquid strikes and pre-open both produce all-padding frames, and they are
    data: "no book at 09:07" is a fact the fill simulator needs."""
    original = _depth(levels=())
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(original, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    (_, _, restored), = store.depth_sides(store.read_capture(tmp_path, "depth"))
    assert restored.levels == ()
    assert restored.padding_rows == 20
    assert restored == original


def test_a_full_snapshot_round_trips_exactly(tmp_path) -> None:
    original = _snapshot()
    with store.snapshot_writer(tmp_path, "s1") as writer:
        writer.append(original, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    (_, _, restored), = store.snapshots(store.read_capture(tmp_path, "quotes"))
    assert restored == original
    assert restored.open_interest == 2_100_000
    assert len(restored.depth5) == 5


def test_a_quote_snapshot_keeps_open_interest_null_rather_than_zero(tmp_path) -> None:
    """Zero-filling would be a lie: 0 is a legitimate open interest, and an
    options study reads OI to tell opening flow from closing."""
    frames, _, _ = p.decode_feed_frames(pk.quote_frame())
    with store.snapshot_writer(tmp_path, "s1") as writer:
        writer.append(frames[0], recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    table = store.read_capture(tmp_path, "quotes")
    assert table.column("open_interest").to_pylist() == [None]
    (_, _, restored), = store.snapshots(table)
    assert restored.open_interest is None
    assert not restored.has_open_interest
    assert restored == frames[0]


def test_cumulative_volume_survives_as_an_integer(tmp_path) -> None:
    """Traded quantity is a *difference* of this column, so a float round trip
    would put rounding error into the fill simulator's only trade observable."""
    frames, _, _ = p.decode_feed_frames(pk.volume_tape([(1000.05, 30), (1000.10, 60)]))
    with store.snapshot_writer(tmp_path, "s1") as writer:
        for i, frame in enumerate(frames):
            writer.append(frame, recv_wall_ns=OPEN_WALL_NS + i * SECOND, recv_mono_ns=i * SECOND)

    volumes = store.read_capture(tmp_path, "quotes").column("volume").to_pylist()
    assert volumes == [1_000_030, 1_000_090]
    assert all(isinstance(v, int) for v in volumes)


# --- 2. arrival order ----------------------------------------------------------


def test_frames_from_one_recv_share_a_timestamp_and_keep_their_order(tmp_path) -> None:
    """One read returned them all, so one instant is the honest measurement --
    but a timestamp-only sort would then permute them arbitrarily."""
    frames, _, _ = p.decode_depth_frames(pk.walking_book(4))
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.extend(frames, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    table = store.read_capture(tmp_path, "depth")
    assert table.column("recv_wall_ns").to_pylist() == [OPEN_WALL_NS] * 8
    assert table.column("recv_seq").to_pylist() == list(range(8))
    assert [side for _, _, side in store.depth_sides(table)] == list(frames)


def test_reading_back_restores_arrival_order_across_files_and_partitions(tmp_path) -> None:
    """Written interleaved across two instruments and several files; read back in
    the order it arrived, not the order the files happen to sort in. Timestamps are
    distinct here -- the tie case, which is the one ``recv_seq`` exists for, is the
    next test."""
    writer = store.depth_writer(tmp_path, "s1", rows_per_file=2, max_buffered_rows=4)
    expected = []
    for i in range(9):
        security_id = 111 if i % 2 else 222
        frame = _depth(levels=pk.BID_LEVELS[: 1 + i % 5], security_id=security_id)
        writer.append(frame, recv_wall_ns=OPEN_WALL_NS + i * SECOND, recv_mono_ns=i * SECOND)
        expected.append(frame)
    writer.close()

    table = store.read_capture(tmp_path, "depth")
    assert table.column("recv_seq").to_pylist() == list(range(9))
    assert [side for _, _, side in store.depth_sides(table)] == expected


def test_arrival_order_survives_a_timestamp_tie_across_partitions(tmp_path) -> None:
    """The failure ``recv_seq`` exists to prevent, isolated.

    Every frame here shares one wall timestamp, because one ``recv()`` returned
    them all and that is the honest measurement. They land in two instrument
    partitions, so the file list sorts 111's rows ahead of 222's -- while the
    *first* frame to arrive was 222's. Sorting on the timestamp alone therefore
    cannot reconstruct arrival order no matter how the tie is broken, and book
    reconstruction stops being reproducible run to run.
    """
    frames = [
        _depth(levels=pk.BID_LEVELS[: 1 + i % 4], security_id=111 if i % 2 else 222)
        for i in range(8)
    ]
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.extend(frames, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=OPEN_MONO_NS)

    table = store.read_capture(tmp_path, "depth")
    assert len(set(table.column("recv_wall_ns").to_pylist())) == 1, "the tie is real"
    on_disk = pq.read_table(sorted(tmp_path.rglob("*.parquet"))[0]).column("recv_seq").to_pylist()
    assert on_disk[0] != 0, "file order differs from arrival order, so the sort must do work"

    assert table.column("recv_seq").to_pylist() == list(range(8))
    assert [side for _, _, side in store.depth_sides(table)] == frames


def test_recv_seq_counts_every_frame_including_the_unstored_ones(tmp_path) -> None:
    """A disconnect occupies a place in the arrival order. Numbering only stored
    rows would hide that a gap in the data had a *reason* sitting in it."""
    book, _, _ = p.decode_depth_frames(pk.depth_book())
    (drop,), _, _ = p.decode_depth_frames(pk.depth_disconnect(805))
    writer = store.depth_writer(tmp_path, "s1")
    writer.extend([book[0], drop, book[1]], recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    writer.close()

    assert store.read_capture(tmp_path, "depth").column("recv_seq").to_pylist() == [0, 2]
    assert writer.disconnects[0]["recv_seq"] == 1


# --- 3. partitioning, flushing and crash tolerance -----------------------------


def test_files_land_in_hive_partitions_by_date_and_instrument(tmp_path) -> None:
    with store.depth_writer(tmp_path, "sess-1") as writer:
        writer.append(
            _depth(levels=pk.BID_LEVELS, security_id=45_678),
            recv_wall_ns=OPEN_WALL_NS,
            recv_mono_ns=0,
        )

    written = list(tmp_path.rglob("*.parquet"))
    assert len(written) == 1
    assert written[0].relative_to(tmp_path).parts == (
        "depth",
        "date=2026-08-18",
        "security_id=45678",
        "part-sess-1-00000.parquet",
    )


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 8, 18, 9, 15, tzinfo=store.IST), date(2026, 8, 18)),
        (datetime(2026, 8, 18, 15, 30, tzinfo=store.IST), date(2026, 8, 18)),
        (datetime(2026, 8, 18, 23, 59, 59, tzinfo=store.IST), date(2026, 8, 18)),
        (datetime(2026, 8, 19, 0, 0, 1, tzinfo=store.IST), date(2026, 8, 19)),
    ],
)
def test_the_trading_date_is_the_ist_calendar_date(when: datetime, expected: date) -> None:
    """UTC agrees during regular NSE hours only by accident of the 10:00 UTC
    close. A session that runs late would land in the previous day's partition."""
    assert store.trading_date(int(when.timestamp() * SECOND)) == expected


def test_a_session_spanning_midnight_splits_across_two_date_partitions(tmp_path) -> None:
    late = int(datetime(2026, 8, 18, 23, 59, 59, tzinfo=store.IST).timestamp() * SECOND)
    frame = _depth(levels=pk.BID_LEVELS)
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(frame, recv_wall_ns=late, recv_mono_ns=0)
        writer.append(frame, recv_wall_ns=late + 2 * SECOND, recv_mono_ns=2 * SECOND)

    dates = sorted(path.parent.parent.name for path in tmp_path.rglob("*.parquet"))
    assert dates == ["date=2026-08-18", "date=2026-08-19"]
    assert store.read_capture(tmp_path, "depth").num_rows == 2


def test_rows_per_file_starts_a_new_file_without_losing_rows(tmp_path) -> None:
    writer = store.depth_writer(tmp_path, "s1", rows_per_file=3, max_buffered_rows=10)
    for i in range(7):
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS + i, recv_mono_ns=i)
    writer.close()

    files = sorted(path.name for path in tmp_path.rglob("*.parquet"))
    assert files == [f"part-s1-{i:05d}.parquet" for i in range(3)]  # 3 + 3 + 1
    assert store.read_capture(tmp_path, "depth").num_rows == 7


def test_the_buffer_ceiling_flushes_before_memory_grows(tmp_path) -> None:
    """Two instruments, neither reaching rows_per_file on its own. Without the
    total ceiling a capture spread thinly over many strikes would buffer without
    bound -- which is the realistic shape, since 10 strikes each ticking slowly is
    exactly what the capture subscribes to."""
    writer = store.depth_writer(tmp_path, "s1", rows_per_file=4, max_buffered_rows=4)
    for i in range(4):
        writer.append(
            _depth(levels=pk.BID_LEVELS, security_id=111 if i % 2 else 222),
            recv_wall_ns=OPEN_WALL_NS + i,
            recv_mono_ns=i,
        )
    assert len(list(tmp_path.rglob("*.parquet"))) == 2, "flushed both partitions"
    writer.close()
    assert store.read_capture(tmp_path, "depth").num_rows == 4


def test_a_flush_leaves_no_temp_file_behind(tmp_path) -> None:
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    assert list(tmp_path.rglob("*.tmp")) == []


def test_a_leftover_temp_file_from_a_killed_flush_is_not_read_as_data(tmp_path) -> None:
    """The dangerous case is a temp file that was *fully written* before the kill:
    it parses cleanly, so a reader that globbed it would silently double rows.
    That is a wrong number rather than a crash, which is worse."""
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)

    good = next(tmp_path.rglob("*.parquet"))
    leftover = store._temp_path(good.with_name("part-s1-00001.parquet"))
    pq.write_table(pq.read_table(good), leftover)
    assert leftover.exists() and leftover.stat().st_size > 0

    assert store.read_capture(tmp_path, "depth").num_rows == 1
    assert leftover.name.startswith("."), "also ignorable by Arrow, Spark and Hive"


def test_a_partially_written_temp_file_does_not_break_the_reader(tmp_path) -> None:
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)

    good = next(tmp_path.rglob("*.parquet"))
    store._temp_path(good.with_name("part-s1-00001.parquet")).write_bytes(b"PAR1\x00truncated")
    assert store.read_capture(tmp_path, "depth").num_rows == 1


@pytest.mark.parametrize("stray", ["part-s1-00000.parquet.bak", "notes.txt", "_SUCCESS"])
def test_a_stray_file_beside_the_data_is_ignored_rather_than_read(tmp_path, stray: str) -> None:
    """What the explicit file list buys over the leading-dot convention.

    Arrow skips dotfiles on its own, so ``.tmp`` leftovers are covered twice. These
    are not dotfiles, and handing Arrow the *directory* reads them: a copy of a part
    file silently doubles every row, and a text file makes the whole read raise. Both
    are things an operator inspecting a month-old capture will plausibly leave behind,
    and the doubling is the worse of the two because it is a wrong number, not a
    crash.
    """
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)

    good = next(tmp_path.rglob("*.parquet"))
    good.with_name(stray).write_bytes(good.read_bytes() if stray.endswith(".bak") else b"junk")

    assert store.read_capture(tmp_path, "depth").num_rows == 1


def test_close_is_idempotent_and_appends_after_it_are_refused(tmp_path) -> None:
    writer = store.depth_writer(tmp_path, "s1")
    writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    first = writer.close()
    assert writer.close() == first
    assert store.read_capture(tmp_path, "depth").num_rows == 1, "not written twice"

    with pytest.raises(RuntimeError, match="closed"):
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)


def test_a_session_id_that_could_escape_the_root_is_rejected(tmp_path) -> None:
    for bad in ("../../etc/passwd", "a/b", "", "with space", "-leading-dash"):
        with pytest.raises(ValueError, match="session id"):
            store.depth_writer(tmp_path, bad)
    store.depth_writer(tmp_path, "2026-08-18T0915Z.laptop")  # the realistic shape


def test_an_incoherent_flush_policy_is_rejected_at_construction(tmp_path) -> None:
    with pytest.raises(ValueError, match="rows_per_file"):
        store.depth_writer(tmp_path, "s1", rows_per_file=100, max_buffered_rows=10)


# --- 4. clock discipline -------------------------------------------------------


def test_an_ntp_step_is_detected_because_the_monotonic_clock_did_not_move(tmp_path) -> None:
    """The failure this exists to catch: wall clock jumps 30s, monotonic advances
    1s. In wall clock alone that is indistinguishable from a 30-second dropout."""
    writer = store.depth_writer(tmp_path, "s1")
    frame = _depth(levels=pk.BID_LEVELS)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS + SECOND, recv_mono_ns=SECOND)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS + 31 * SECOND, recv_mono_ns=2 * SECOND)
    writer.close()

    clock = writer.clock
    assert clock.max_abs_skew_ns == 29 * SECOND
    assert clock.max_skew_at_seq == 2, "localised to a row, so one segment is excluded"
    assert clock.wall_regressions == 0
    assert clock.span_disagreement_ns == 29 * SECOND


def test_a_backwards_wall_clock_is_counted(tmp_path) -> None:
    """Monotonic cannot regress, so a negative wall delta is unambiguous damage --
    and it makes any sort by wall clock reorder real arrivals."""
    writer = store.depth_writer(tmp_path, "s1")
    frame = _depth(levels=pk.BID_LEVELS)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS + 10 * SECOND, recv_mono_ns=0)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=SECOND)
    writer.close()

    assert writer.clock.wall_regressions == 1
    assert writer.clock.max_abs_skew_ns == 11 * SECOND


def test_a_clean_session_reports_no_skew(tmp_path) -> None:
    """A detector that fires on a healthy session is one that gets ignored."""
    writer = store.depth_writer(tmp_path, "s1")
    frame = _depth(levels=pk.BID_LEVELS)
    for i in range(20):
        writer.append(frame, recv_wall_ns=OPEN_WALL_NS + i * SECOND, recv_mono_ns=i * SECOND)
    writer.close()

    assert writer.clock.max_abs_skew_ns == 0
    assert writer.clock.wall_regressions == 0
    assert writer.clock.span_disagreement_ns == 0
    assert writer.clock.rows == 20


def test_a_real_gap_shows_in_both_clocks_and_is_not_flagged_as_skew(tmp_path) -> None:
    """The distinction the two clocks buy: a genuine 60-second dropout advances
    both, so it is a segment boundary rather than clock damage."""
    writer = store.depth_writer(tmp_path, "s1")
    frame = _depth(levels=pk.BID_LEVELS)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS + 60 * SECOND, recv_mono_ns=60 * SECOND)
    writer.close()

    assert writer.clock.max_abs_skew_ns == 0
    assert writer.clock.wall_span_ns == 60 * SECOND


def test_a_suspend_and_resume_shows_as_a_span_disagreement(tmp_path) -> None:
    """`time.monotonic` does not tick across suspend on Linux, so a resumed laptop
    reports a big wall span against a small monotonic one -- the exact hazard that
    makes latency numbers noise if it goes unnoticed."""
    writer = store.depth_writer(tmp_path, "s1")
    frame = _depth(levels=pk.BID_LEVELS)
    writer.append(frame, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    resumed = OPEN_WALL_NS + int(timedelta(minutes=45).total_seconds()) * SECOND
    writer.append(frame, recv_wall_ns=resumed, recv_mono_ns=SECOND // 10)
    writer.close()

    assert writer.clock.span_disagreement_ns > 44 * 60 * SECOND
    assert writer.clock.mono_span_ns < SECOND


# --- 5. the manifest -----------------------------------------------------------


def test_the_manifest_records_disconnects_with_their_reason(tmp_path) -> None:
    """A gap whose cause was an entitlement lapse is a different finding from a
    gap caused by wifi, and only the manifest can tell them apart later."""
    (drop,), _, _ = p.decode_depth_frames(pk.depth_disconnect(806))
    writer = store.depth_writer(tmp_path, "s1")
    writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    writer.append(drop, recv_wall_ns=OPEN_WALL_NS + SECOND, recv_mono_ns=SECOND)
    path = writer.close()

    manifest = json.loads(path.read_text())
    (event,) = manifest["disconnects"]
    assert event["reason_code"] == 806
    assert event["is_entitlement"] is True
    assert event["is_fatal"] is True
    assert event["recv_wall_ns"] == OPEN_WALL_NS + SECOND
    assert manifest["skipped_frames"] == {"Disconnect": 1}
    assert manifest["rows_written"] == 1
    assert manifest["frames_seen"] == 2


def test_unknown_frames_are_counted_by_code_rather_than_dropped(tmp_path) -> None:
    """"We stored what we understood and here is what we did not" is auditable.
    Silently discarding it is not."""
    frames, _, _ = p.decode_depth_frames(
        pk.depth_unknown(msg_code=99, length=40) + pk.depth_unknown(msg_code=99, length=40)
    )
    writer = store.depth_writer(tmp_path, "s1")
    writer.extend(frames, recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    manifest = json.loads(writer.close().read_text())

    assert manifest["skipped_frames"] == {"unknown_code_99": 2}
    assert manifest["rows_written"] == 0


def test_the_manifest_carries_the_decoder_statistics(tmp_path) -> None:
    """bytes-in, partial carries and unknown codes exist only at capture time; a
    later pass over the Parquet cannot reconstruct them."""
    decoder = p.depth_decoder()
    writer = store.depth_writer(tmp_path, "s1")
    for i, chunk in enumerate(pk.chunked(pk.walking_book(3), 100)):
        frames = decoder.feed(chunk)
        writer.extend(frames, recv_wall_ns=OPEN_WALL_NS + i * SECOND, recv_mono_ns=i * SECOND)
    writer.decode_stats = decoder.stats
    manifest = json.loads(writer.close().read_text())

    stats = manifest["decode_stats"]
    assert stats["frames"] == 6
    assert stats["bytes_in"] == 6 * p.DEPTH_FRAME_SIZE
    assert stats["partial_carries"] > 0
    assert stats["by_code"] == {"41": 3, "51": 3}
    assert stats["clean"] is True


def test_the_manifest_lists_every_partition_it_wrote(tmp_path) -> None:
    writer = store.depth_writer(tmp_path, "s1", rows_per_file=2, max_buffered_rows=8)
    for i in range(5):
        writer.append(
            _depth(levels=pk.BID_LEVELS, security_id=111 if i % 2 else 222),
            recv_wall_ns=OPEN_WALL_NS + i,
            recv_mono_ns=i,
        )
    manifest = json.loads(writer.close().read_text())

    assert manifest["partitions"] == [
        {"date": "2026-08-18", "security_id": 111, "rows": 2, "files": 1},
        {"date": "2026-08-18", "security_id": 222, "rows": 3, "files": 2},
    ]
    assert manifest["rows_written"] == 5
    assert manifest["rows_buffered"] == 0


def test_a_session_that_captured_nothing_still_writes_a_manifest(tmp_path) -> None:
    """"Nothing arrived" is a QA finding. A missing manifest is indistinguishable
    from a capture that was never started."""
    path = store.depth_writer(tmp_path, "s1").close()
    manifest = json.loads(path.read_text())
    assert manifest["rows_written"] == 0
    assert manifest["frames_seen"] == 0
    assert manifest["clock"]["rows"] == 0
    assert manifest["partitions"] == []
    assert store.read_capture(tmp_path, "depth").num_rows == 0


def test_manifests_are_discoverable_and_filterable_by_session(tmp_path) -> None:
    for session in ("s1", "s2"):
        writer = store.depth_writer(tmp_path, session)
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
        writer.close()
    store.snapshot_writer(tmp_path, "s1").close()

    assert len(store.read_manifests(tmp_path)) == 3
    tables = {m["table"] for m in store.read_manifests(tmp_path, session_id="s1")}
    assert tables == {"depth", "quotes"}
    assert store.read_manifests(tmp_path / "nowhere") == []


# --- misc ----------------------------------------------------------------------


def test_an_empty_read_still_has_the_right_schema(tmp_path) -> None:
    """So callers never branch on "did anything get captured" before selecting."""
    for table, schema in (("depth", store.DEPTH_SCHEMA), ("quotes", store.SNAPSHOT_SCHEMA)):
        empty = store.read_capture(tmp_path, table)
        assert empty.num_rows == 0
        assert empty.schema == schema


def test_an_unknown_table_name_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown table"):
        store.read_capture(tmp_path, "trades")


def test_the_two_tables_do_not_share_a_directory(tmp_path) -> None:
    with store.depth_writer(tmp_path, "s1") as depth:
        depth.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)
    with store.snapshot_writer(tmp_path, "s1") as quotes:
        quotes.append(_snapshot(), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)

    assert store.read_capture(tmp_path, "depth").num_rows == 1
    assert store.read_capture(tmp_path, "quotes").num_rows == 1
    assert {path.name for path in tmp_path.iterdir()} == {"depth", "quotes", "sessions"}


def test_reads_can_be_narrowed_to_a_date_and_an_instrument(tmp_path) -> None:
    """Partition pruning is the reason for the layout: a per-strike replay over a
    month of capture should touch that strike's files only."""
    writer = store.depth_writer(tmp_path, "s1")
    day2 = OPEN_WALL_NS + 86_400 * SECOND
    for wall, security_id in (
        (OPEN_WALL_NS, 111),
        (OPEN_WALL_NS, 222),
        (day2, 111),
        (day2, 222),
    ):
        frame = _depth(levels=pk.BID_LEVELS, security_id=security_id)
        writer.append(frame, recv_wall_ns=wall, recv_mono_ns=0)
    writer.close()

    assert store.read_capture(tmp_path, "depth").num_rows == 4
    assert store.read_capture(tmp_path, "depth", security_ids=[111]).num_rows == 2
    assert store.read_capture(tmp_path, "depth", dates=[date(2026, 8, 19)]).num_rows == 2
    narrowed = store.read_capture(
        tmp_path, "depth", dates=[date(2026, 8, 19)], security_ids=[222]
    )
    assert narrowed.num_rows == 1
    assert narrowed.column("security_id").to_pylist() == [222]


def test_level_lists_stay_columnar_so_a_price_only_scan_is_cheap(tmp_path) -> None:
    """Three parallel lists rather than a list of structs: a scan that wants only
    prices should not read quantities off disk."""
    with store.depth_writer(tmp_path, "s1") as writer:
        writer.append(_depth(levels=pk.BID_LEVELS), recv_wall_ns=OPEN_WALL_NS, recv_mono_ns=0)

    path = next(tmp_path.rglob("*.parquet"))
    columns = pq.read_table(path, columns=["price"])
    assert columns.column_names == ["price"]
    assert columns.column("price").to_pylist() == [[lvl[0] for lvl in pk.BID_LEVELS]]
    assert pa.types.is_list(store.DEPTH_SCHEMA.field("price").type)
