"""Tests for the two binary wire decoders. No network, no credentials.

Three groups, in descending order of what they would catch:

1. **Absolute-size and hand-assembly checks.** The fixtures pack with the same
   format strings the decoder unpacks, so a wrong format string would produce a
   fixture and a decoder that agree with each other and disagree with NSE. These
   tests pin byte sizes as literals and rebuild one frame from format strings typed
   out independently, which is the only thing here that could catch that.
2. **Framing.** Every test that matters splits the stream at a boundary a socket
   could actually produce. Both SDK decoders lose data on realistic buffers; the
   tests that would have caught those bugs are named for them.
3. **Malformed input.** A capture running unattended on a laptop for a month will
   see things a well-formed fixture never does.
"""

from __future__ import annotations

import struct

import pytest

from bnfmm.data import protocol as p

from .fixtures import packets as pk

# --- 1. sizes and hand-assembly ------------------------------------------------
# Literals, deliberately. Derived from the SDK's slice bounds (`data[0:162]`,
# `data[0:112]`, ...) and independently confirmed with struct.calcsize before
# being written down here. If a format string is edited, these fail.


def test_frame_sizes_are_what_the_wire_says() -> None:
    assert p.DEPTH_HEADER_SIZE == 12
    assert p.DEPTH_LEVEL_SIZE == 16
    assert p.DEPTH_FRAME_SIZE == 332  # 12 + 20 * 16
    assert p.FEED_HEADER_SIZE == 8
    assert p.DEPTH_5_LEVEL_SIZE == 20
    assert p.FEED_FRAME_SIZES == {
        p.FEED_TICKER: 16,
        p.FEED_DEPTH_5: 112,
        p.FEED_QUOTE: 50,
        p.FEED_OI: 12,
        p.FEED_PREV_CLOSE: 16,
        p.FEED_STATUS: 8,
        p.FEED_FULL: 162,
        p.FEED_DISCONNECT: 10,
    }


def test_a_hand_assembled_depth_frame_decodes() -> None:
    """The one test that does not trust the fixtures.

    Formats are typed out here rather than imported, so this disagrees with the
    decoder if either drifts. Everything else in this file shares constants with
    the thing it is testing.
    """
    header = struct.pack("<hBBiI", 332, 41, 2, 68390, 0)
    body = struct.pack("<dII", 999.95, 90, 3) + struct.pack("<dII", 999.90, 150, 4)
    body += struct.pack("<dII", 0.0, 0, 0) * 18
    raw = header + body
    assert len(raw) == 332

    frames, rest, _ = p.decode_depth_frames(raw)
    assert rest == b""
    (side,) = frames
    assert isinstance(side, p.DepthSide)
    assert side.security_id == 68390
    assert side.exchange_segment == 2
    assert side.side is p.Side.BID
    assert side.levels == (
        p.Level(price=999.95, quantity=90, orders=3),
        p.Level(price=999.90, quantity=150, orders=4),
    )
    assert side.padding_rows == 18


def test_the_length_field_is_at_the_front_and_little_endian() -> None:
    """Byte-level, because `<h` vs `>h` is a silent 256x error on frame length."""
    raw = pk.depth_frame(pk.BID_LEVELS)
    assert raw[0:2] == (332).to_bytes(2, "little")
    assert raw[2] == 41
    assert raw[3] == p.NSE_FNO


def test_msg_length_is_signed_so_the_max_frame_is_bounded() -> None:
    """`<h`, not `<H`. A 200-level frame is 3212 bytes so this never binds in
    practice, but the decoder must not treat a negative length as huge."""
    assert struct.calcsize("<h") == 2
    with pytest.raises(struct.error):
        struct.pack("<h", 40_000)


# --- 2. framing ----------------------------------------------------------------


def test_a_full_book_decodes_to_a_bid_and_an_ask() -> None:
    frames, rest, stats = p.decode_depth_frames(pk.depth_book())
    assert rest == b""
    bid, ask = frames
    assert bid.side is p.Side.BID and ask.side is p.Side.ASK
    assert bid.security_id == ask.security_id
    assert len(bid.levels) == len(pk.BID_LEVELS)
    assert len(ask.levels) == len(pk.ASK_LEVELS)
    assert stats.frames == 2
    assert stats.clean


def test_padding_is_stripped_but_counted() -> None:
    (bid, _), _, _ = p.decode_depth_frames(pk.depth_book())
    assert len(bid.levels) + bid.padding_rows == p.DEPTH_LEVELS
    assert all(lvl.quantity > 0 for lvl in bid.levels)
    assert bid.is_clean


def test_a_frame_split_across_chunks_is_carried_not_lost() -> None:
    """The bug in `MarketFeed.process_data`, in its depth-feed form.

    A 332-byte frame does not fit a socket read boundary, so this is the normal
    case rather than an edge case.
    """
    decoder = p.depth_decoder()
    payload = pk.depth_book()
    collected = []
    for chunk in pk.chunked(payload, 100):
        collected.extend(decoder.feed(chunk))
    assert len(collected) == 2
    assert decoder.pending_bytes == 0


@pytest.mark.parametrize("chunk_size", [1, 3, 11, 12, 13, 100, 331, 332, 333, 664, 10_000])
def test_framing_is_correct_at_every_chunk_boundary(chunk_size: int) -> None:
    """Including 1 byte at a time, and exactly on and either side of a frame edge."""
    payload = pk.walking_book(4)
    decoder = p.depth_decoder()
    frames = []
    for chunk in pk.chunked(payload, chunk_size):
        frames.extend(decoder.feed(chunk))
    assert len(frames) == 8
    assert decoder.pending_bytes == 0
    assert decoder.stats.bytes_in == len(payload)


def test_a_disconnect_mid_buffer_does_not_swallow_the_rest() -> None:
    """`FullDepth.process_20_depth_data` returns None on code 50, which ends its
    caller's loop and discards every frame after it. That is a silent data loss on
    exactly the buffer you most want to read."""
    payload = pk.depth_frame(pk.BID_LEVELS) + pk.depth_disconnect(806) + pk.depth_book()
    frames, rest, _ = p.decode_depth_frames(payload)
    assert rest == b""
    assert len(frames) == 4
    assert isinstance(frames[1], p.Disconnect)
    assert frames[1].is_entitlement
    assert all(isinstance(f, p.DepthSide) for f in (frames[0], frames[2], frames[3]))


def test_every_general_feed_packet_in_one_buffer_is_decoded() -> None:
    """`MarketFeed.process_data` reads `data[0:1]`, decodes one packet, returns --
    so at option-chain subscription counts most of the feed is discarded."""
    payload = (
        pk.ticker_frame() + pk.full_frame() + pk.oi_frame() + pk.quote_frame() + pk.status_frame()
    )
    frames, rest, stats = p.decode_feed_frames(payload)
    assert rest == b""
    assert len(frames) == 5
    assert [type(f).__name__ for f in frames] == [
        "Ticker",
        "Snapshot",
        "OpenInterest",
        "Snapshot",
        "MarketStatus",
    ]
    assert stats.clean


def test_an_unknown_code_is_skipped_by_its_declared_length() -> None:
    payload = pk.depth_unknown(msg_code=99, length=40) + pk.depth_frame(pk.BID_LEVELS)
    frames, rest, stats = p.decode_depth_frames(payload)
    assert rest == b""
    assert isinstance(frames[0], p.UnknownFrame)
    assert frames[0].msg_code == 99 and frames[0].length == 40
    assert isinstance(frames[1], p.DepthSide), "the known frame after it must survive"
    assert stats.unknown_codes == {99: 1}
    assert not stats.clean


def test_an_unknown_code_with_no_usable_length_is_unrecoverable() -> None:
    """There is no framing marker to resynchronise on, so guessing is worse than
    dropping the connection. Capture starts a new segment."""
    raw = struct.pack(p.DEPTH_HEADER_FMT, 0, 99, p.NSE_FNO, 1, 0) + b"\x00" * 20
    with pytest.raises(p.ProtocolError, match="resynchronised"):
        p.decode_depth_frames(raw)


def test_a_header_that_lies_about_its_length_is_counted_not_obeyed() -> None:
    """The SDK never reads this field, so nothing confirms the server fills it. The
    per-code size is the framing authority; a disagreement is a QA note."""
    payload = pk.depth_frame(pk.BID_LEVELS, declared_length=999) + pk.depth_frame(pk.ASK_LEVELS)
    frames, rest, stats = p.decode_depth_frames(payload)
    assert rest == b""
    assert len(frames) == 2, "framing followed the known size, not the lie"
    assert stats.length_mismatches == {p.MSG_BID: 1}
    assert frames[0].declared_length == 999


def test_stats_accumulate_across_chunks() -> None:
    decoder = p.depth_decoder()
    for chunk in pk.chunked(pk.walking_book(3), 77):
        decoder.feed(chunk)
    assert decoder.stats.frames == 6
    assert decoder.stats.chunks > 1
    assert decoder.stats.partial_carries > 0
    assert decoder.stats.by_code == {p.MSG_BID: 3, p.MSG_ASK: 3}
    assert "6 frames" in decoder.stats.summary()


def test_pending_bytes_reveals_a_stream_cut_mid_frame() -> None:
    """A non-zero remainder at end of session is a QA fact, not a warning to drop."""
    decoder = p.depth_decoder()
    decoder.feed(pk.depth_frame(pk.BID_LEVELS)[:200])
    assert decoder.pending_bytes == 200
    assert "pending=200" in repr(decoder)


# --- 3. malformed and adversarial input ----------------------------------------


def test_an_interleaved_padding_row_is_flagged_rather_than_filtered() -> None:
    """A real level after a padding row means the book is malformed. Quietly
    dropping the padding would turn it into a plausible five-level book."""
    rows = ((999.95, 90, 3), (0.0, 0, 0), (999.85, 300, 7))
    body = b"".join(struct.pack(p.DEPTH_LEVEL_FMT, *row) for row in rows)
    body += struct.pack(p.DEPTH_LEVEL_FMT, 0.0, 0, 0) * 17
    frames, _, stats = p.decode_depth_frames(pk.depth_frame((), body=body))
    (side,) = frames
    assert side.interleaved_padding
    assert not side.is_clean
    assert stats.padding_anomalies == 1
    assert len(side.levels) == 2, "the levels are still reported -- flagged, not dropped"


def test_a_priced_level_with_no_quantity_is_an_anomaly() -> None:
    body = struct.pack(p.DEPTH_LEVEL_FMT, 999.95, 0, 0)
    body += struct.pack(p.DEPTH_LEVEL_FMT, 0.0, 0, 0) * 19
    (side,), _, stats = p.decode_depth_frames(pk.depth_frame((), body=body))
    assert side.anomalous_rows == 1
    assert side.levels == ()
    assert stats.padding_anomalies == 1


def test_an_out_of_order_side_is_detected() -> None:
    """`combine_and_format_depth` sorts defensively, implying wire order is not
    guaranteed. Rather than sort silently, measure how often it happens."""
    ascending_bids = tuple(sorted(pk.BID_LEVELS, key=lambda lvl: lvl[0]))
    (side,), _, stats = p.decode_depth_frames(pk.depth_frame(ascending_bids, side=p.Side.BID))
    assert not side.is_sorted
    assert stats.unsorted_sides == 1

    (ok,), _, clean = p.decode_depth_frames(pk.depth_frame(pk.BID_LEVELS, side=p.Side.BID))
    assert ok.is_sorted and clean.unsorted_sides == 0


def test_an_empty_book_is_not_an_error() -> None:
    """All-padding happens before the open and in an illiquid strike."""
    (side,), _, stats = p.decode_depth_frames(pk.depth_frame(()))
    assert side.levels == ()
    assert side.padding_rows == 20
    assert side.best is None
    assert side.total_quantity == 0
    assert side.is_clean and stats.clean


@pytest.mark.parametrize("decode", [p.decode_depth_frames, p.decode_feed_frames])
def test_an_empty_buffer_yields_nothing(decode) -> None:
    frames, rest, stats = decode(b"")
    assert frames == []
    assert rest == b""
    assert stats.frames == 0
    assert stats.clean


def test_a_buffer_shorter_than_a_header_is_carried_whole() -> None:
    frames, rest, _ = p.decode_depth_frames(b"\x01\x02\x03")
    assert frames == []
    assert rest == b"\x01\x02\x03", "never discard bytes that might complete a header"


# --- disconnect handling -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        (805, "connections exceeded"),
        (806, "Data APIs"),
        (807, "expired"),
        (808, "client id"),
        (809, "authentication"),
    ],
)
def test_disconnect_reasons_are_explained(code: int, needle: str) -> None:
    (frame,), _, _ = p.decode_depth_frames(pk.depth_disconnect(code))
    assert isinstance(frame, p.Disconnect)
    assert needle in frame.reason
    assert frame.is_fatal, "every documented reason is fatal for this run"
    assert str(code) in str(frame)


def test_an_undocumented_disconnect_reason_is_not_fatal() -> None:
    """Retrying an unknown code is the right default; retrying a known one is not."""
    (frame,), _, _ = p.decode_depth_frames(pk.depth_disconnect(999))
    assert not frame.is_fatal
    assert "unknown reason 999" in frame.reason


def test_the_entitlement_code_is_singled_out() -> None:
    """806 is the websocket face of REST's DH-902, and answers at runtime whether
    the subscription is live. Every other fatal code needs a different response."""
    assert p.ENTITLEMENT_DISCONNECT == 806
    (gated,), _, _ = p.decode_depth_frames(pk.depth_disconnect(806))
    (expired,), _, _ = p.decode_depth_frames(pk.depth_disconnect(807))
    assert gated.is_entitlement and not expired.is_entitlement


def test_the_two_feeds_have_different_disconnect_layouts() -> None:
    """12 bytes with the reason in a uint32 on the depth feed; 10 bytes with the
    reason in a uint16 on the general feed. Decoding one with the other's layout
    reads the reason out of the wrong offset."""
    assert len(pk.depth_disconnect(806)) == 12
    assert len(pk.feed_disconnect(806)) == 10
    (a,), _, _ = p.decode_depth_frames(pk.depth_disconnect(806))
    (b,), _, _ = p.decode_feed_frames(pk.feed_disconnect(806))
    assert a.reason_code == b.reason_code == 806


# --- the general feed's trade fields -------------------------------------------


def test_a_full_packet_carries_the_whole_trade_side() -> None:
    (snap,), rest, _ = p.decode_feed_frames(pk.full_frame())
    assert rest == b""
    assert isinstance(snap, p.Snapshot)
    assert snap.ltp == 1000.05
    assert snap.last_quantity == pk.LOT
    assert snap.volume == 1_234_560
    assert snap.open_interest == 2_100_000
    assert snap.has_open_interest
    assert len(snap.depth5) == 5


def test_a_quote_packet_has_no_open_interest() -> None:
    """Which is why capture subscribes to Full: an options study without OI is
    missing the one field that says whether flow is opening or closing."""
    (snap,), _, _ = p.decode_feed_frames(pk.quote_frame())
    assert snap.open_interest is None
    assert not snap.has_open_interest
    assert snap.volume == 1_234_560, "everything else is the same layout"
    assert snap.depth5 == ()


def test_cumulative_volume_differences_into_traded_quantity() -> None:
    """The only observable the fill simulator can lean on. `last_quantity` is the
    most recent print alone and undercounts whenever two trades land between
    packets, so traded quantity comes from differencing `volume`."""
    prints = [(1000.05, 30), (1000.10, 60), (999.95, 90)]
    frames, _, _ = p.decode_feed_frames(pk.volume_tape(prints))
    volumes = [f.volume for f in frames]
    assert [b - a for a, b in zip(volumes, volumes[1:])] == [60, 90]
    assert volumes[0] - 1_000_000 == 30


def test_the_general_feed_carries_a_coarse_exchange_clock() -> None:
    """Depth frames have no timestamp at all, so `last_trade_epoch` is the only
    exchange-side time available -- one-second resolution, which is enough to bound
    local clock drift but not to measure latency."""
    frames, _, _ = p.decode_feed_frames(pk.volume_tape([(1000.0, 30)] * 3))
    stamps = [f.last_trade_epoch for f in frames]
    assert stamps == sorted(stamps)
    assert stamps[-1] - stamps[0] == 2


def test_float32_prices_are_snapped_to_paise() -> None:
    """57785.05 survives a float32 round trip as 57785.05078125. Without this,
    `ltp == best_bid` is False for a trade that happened at the bid -- and the fill
    simulator is built entirely out of that comparison."""
    raw = struct.unpack("<f", struct.pack("<f", 57_785.05))[0]
    assert raw != 57_785.05
    assert abs(raw - 57_785.05) > 1e-4

    (snap,), _, _ = p.decode_feed_frames(pk.full_frame(ltp=57_785.05))
    assert snap.ltp == 57_785.05


@pytest.mark.parametrize("price", [0.05, 1.35, 250.05, 1000.05, 57_785.05, 57_785.20])
def test_snapping_never_moves_a_price_by_a_tick(price: float) -> None:
    """The correction has to be smaller than the smallest tick (Rs 0.05) or it is
    not a de-quantisation, it is a price change."""
    (snap,), _, _ = p.decode_feed_frames(pk.full_frame(ltp=price))
    assert snap.ltp == price
    raw = struct.unpack("<f", struct.pack("<f", price))[0]
    assert abs(raw - snap.ltp) < 0.005


def test_depth_feed_prices_are_float64_and_untouched() -> None:
    """The 20-level feed sends float64, so it needs no correction and gets none."""
    odd = 999.9700000000001
    (side,), _, _ = p.decode_depth_frames(pk.depth_frame(((odd, 30, 1),)))
    assert side.levels[0].price == odd


def test_the_five_level_block_is_a_cross_check_on_the_twenty_level_feed() -> None:
    """Two independent feeds reporting the same top five levels is a free decoder
    validation and an inter-feed latency measurement. It only works if the two
    layouts -- interleaved float32 vs per-side float64 -- decode to the same book."""
    (snap,), _, _ = p.decode_feed_frames(pk.full_frame())
    (bid, ask), _, _ = p.decode_depth_frames(pk.depth_book())
    assert snap.best_bid == bid.levels[0].price
    assert snap.best_ask == ask.levels[0].price
    for i in range(5):
        assert snap.depth5[i].bid_quantity == bid.levels[i].quantity
        assert snap.depth5[i].ask_orders == ask.levels[i].orders


def test_a_five_level_block_pads_when_the_book_is_thin() -> None:
    (snap,), _, _ = p.decode_feed_frames(pk.full_frame(bids=pk.BID_LEVELS[:2], asks=()))
    assert snap.depth5[2].bid_price == 0.0
    assert snap.best_ask == 0.0, "an empty side reads as zero, not as missing"


def test_level_reports_average_order_size() -> None:
    """Per-level order count is the reason this project uses Dhan; average order
    size is the queue-length input it exists to provide."""
    lvl = p.Level(price=999.95, quantity=300, orders=4)
    assert lvl.average_order_size == 75.0
    assert p.Level(price=0.0, quantity=0, orders=0).average_order_size == 0.0


def test_the_thin_feed_packets_decode_to_their_own_types() -> None:
    """Ticker, OI, PrevClose, Depth5 and Status are not subscribed to, but they
    arrive anyway on a shared connection and must not be mistaken for each other --
    Ticker and PrevClose have the *same* 16-byte size and format string."""
    payload = pk.ticker_frame() + pk.prev_close_frame() + pk.depth5_frame() + pk.oi_frame()
    frames, rest, _ = p.decode_feed_frames(payload)
    assert rest == b""
    tick, prev, depth5, oi = frames

    assert isinstance(tick, p.Ticker) and tick.ltp == 1000.05
    assert isinstance(prev, p.PrevClose)
    assert prev.prev_close == 991.15 and prev.prev_open_interest == 2_050_000
    assert isinstance(depth5, p.Depth5Snapshot) and len(depth5.depth5) == 5
    assert isinstance(oi, p.OpenInterest) and oi.open_interest == 2_100_000
    assert p.FEED_FRAME_SIZES[p.FEED_TICKER] == p.FEED_FRAME_SIZES[p.FEED_PREV_CLOSE]


def test_an_unknown_feed_code_is_skipped_and_the_stream_survives() -> None:
    """The general feed is the one likelier to grow codes -- it already carries
    seven, and the SDK's `else: pass` would have swallowed the rest of the buffer."""
    payload = pk.feed_unknown(msg_code=77, length=20) + pk.full_frame()
    frames, rest, stats = p.decode_feed_frames(payload)
    assert rest == b""
    assert isinstance(frames[0], p.UnknownFrame) and frames[0].msg_code == 77
    assert isinstance(frames[1], p.Snapshot)
    assert stats.unknown_codes == {77: 1}


@pytest.mark.parametrize("chunk_size", [1, 7, 8, 9, 50, 161, 162, 163, 1_000])
def test_the_feed_decoder_carries_partials_at_every_boundary(chunk_size: int) -> None:
    """Full packets are 162 bytes, so they straddle reads even more often than
    depth frames do. Mixed sizes in one stream is the realistic case."""
    payload = pk.full_frame() + pk.ticker_frame() + pk.quote_frame() + pk.full_frame()
    decoder = p.market_feed_decoder()
    frames = []
    for chunk in pk.chunked(payload, chunk_size):
        frames.extend(decoder.feed(chunk))
    assert len(frames) == 4
    assert decoder.pending_bytes == 0
    assert decoder.stats.by_code == {p.FEED_FULL: 2, p.FEED_TICKER: 1, p.FEED_QUOTE: 1}
    assert "marketfeed" in repr(decoder)


# --- subscription messages and URL hygiene -------------------------------------


def test_depth_subscriptions_batch_at_fifty() -> None:
    instruments = [(p.NSE_FNO, 1000 + i) for i in range(120)]
    messages = p.depth_subscriptions(instruments)
    assert [m["InstrumentCount"] for m in messages] == [50, 50, 20]
    assert all(m["RequestCode"] == 23 for m in messages)
    assert messages[0]["InstrumentList"][0] == {
        "ExchangeSegment": "NSE_FNO",
        "SecurityId": "1000",
    }


def test_feed_subscriptions_batch_at_a_hundred_and_default_to_full() -> None:
    messages = p.feed_subscriptions([(p.NSE_FNO, 1000 + i) for i in range(250)])
    assert [m["InstrumentCount"] for m in messages] == [100, 100, 50]
    assert all(m["RequestCode"] == p.REQUEST_FULL for m in messages)


def test_duplicate_instruments_are_dropped_before_batching() -> None:
    """A duplicate subscription is a wasted slot against a capped instrument count."""
    messages = p.depth_subscriptions([(p.NSE_FNO, 1)] * 10 + [(p.NSE_FNO, 2)])
    assert messages[0]["InstrumentCount"] == 2


def test_an_int_id_and_a_str_id_are_the_same_instrument() -> None:
    """The two forms both occur, so de-duplication has to happen after normalising.

    ``instruments.Contract.feed_key`` produces a *string* id because that is what
    the wire format wants, while hand-written call sites and configs naturally use
    ints. De-duplicating the raw tuples would let ``(2, 1)`` and ``(2, "1")`` both
    through -- spending two of the fifty depth slots on one contract, with nothing
    in the reply to show it.
    """
    messages = p.depth_subscriptions([(p.NSE_FNO, 45678), (p.NSE_FNO, "45678"), (0, 25)])
    assert messages[0]["InstrumentCount"] == 2
    assert messages[0]["InstrumentList"] == [
        {"ExchangeSegment": "NSE_FNO", "SecurityId": "45678"},
        {"ExchangeSegment": "IDX_I", "SecurityId": "25"},
    ]
    # And the same on the general feed, which shares the batching helper.
    feed = p.feed_subscriptions([(p.NSE_FNO, "1"), (p.NSE_FNO, 1)])
    assert feed[0]["InstrumentCount"] == 1


def test_a_full_depth_batch_of_mixed_id_forms_still_fits_the_cap() -> None:
    """The consequence that matters: 50 distinct contracts must stay one message."""
    mixed = [(p.NSE_FNO, i) if i % 2 else (p.NSE_FNO, str(i)) for i in range(50)]
    messages = p.depth_subscriptions(mixed + mixed)
    assert len(messages) == 1
    assert messages[0]["InstrumentCount"] == 50


def test_an_unknown_segment_is_rejected_at_build_time() -> None:
    with pytest.raises(ValueError, match="unknown exchange segment"):
        p.depth_subscriptions([(42, 1)])


def test_the_connect_url_is_a_credential_and_redaction_removes_it() -> None:
    """Both feeds take the access token as a query parameter, so the URL is as
    secret as the token. `FullDepth.connect` prints it. This is what gets logged."""
    url = p.depth_url(token="secret-token-value", client_id="1100000000")
    assert "secret-token-value" in url
    redacted = p.redact_url(url)
    assert "secret-token-value" not in redacted
    assert "1100000000" not in redacted
    assert "twentydepth" in redacted, "the endpoint is still identifiable"


def test_the_market_feed_url_carries_the_version() -> None:
    url = p.market_feed_url(token="t", client_id="c")
    assert "version=2" in url and "authType=2" in url
    assert url.startswith(p.MARKET_FEED_WSS)


def test_redaction_survives_a_token_containing_url_characters() -> None:
    """A JWT has dots and dashes; a naive split on '&' or '.' would leak a suffix."""
    token = "eyJhbGciOiJIUzI1NiJ9.abc-def_ghi.jkl"
    redacted = p.redact_url(p.depth_url(token=token, client_id="1100000000"))
    for fragment in ("abc-def_ghi", "jkl", "eyJhbGciOiJIUzI1NiJ9"):
        assert fragment not in redacted
