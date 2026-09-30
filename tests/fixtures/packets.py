"""Synthetic wire packets for both Dhan feeds. No network, no credentials.

The whole point of this module is that the depth decoder is a solved problem
*before* the Data API is paid for: the wire layouts are known (DECISIONS #7,
#15), so well-formed bytes can be manufactured and the decoder driven to
exhaustion offline. At the subscription gate, day 1 dumps ~60 seconds of real
bytes, asserts the decoder handles them unchanged, and a real golden file joins
these builders rather than replacing them -- synthetic packets stay useful for the
malformed cases a real capture is unlikely to contain on demand.

**These builders are deliberately independent of nothing.** They pack with the
same format strings ``protocol.py`` unpacks, which means a wrong format string
would produce a fixture and a decoder that agree with each other and disagree with
NSE. ``tests/test_protocol.py`` closes that hole by also asserting absolute frame
sizes and hand-assembling one frame from literal formats typed out separately.
"""

from __future__ import annotations

import struct
from typing import Sequence

from bnfmm.data.protocol import (
    DEPTH_5_LEVEL_FMT,
    DEPTH_5_LEVELS,
    DEPTH_FRAME_SIZE,
    DEPTH_HEADER_FMT,
    DEPTH_HEADER_SIZE,
    DEPTH_LEVEL_FMT,
    DEPTH_LEVELS,
    FEED_DEPTH_5,
    FEED_DISCONNECT,
    FEED_DISCONNECT_FMT,
    FEED_FRAME_SIZES,
    FEED_FULL,
    FEED_OI,
    FEED_PREV_CLOSE,
    FEED_QUOTE,
    FEED_STATUS,
    FEED_TICKER,
    FULL_FMT,
    MSG_ASK,
    MSG_BID,
    MSG_DISCONNECT,
    NSE_FNO,
    OI_FMT,
    PREV_CLOSE_FMT,
    QUOTE_FMT,
    STATUS_FMT,
    TICKER_FMT,
    DEPTH_5_FMT,
    Side,
)

# --- a plausible BANKNIFTY ATM option book ------------------------------------
# Premium ~Rs 1,000, tick Rs 0.05, lot 30 -- the magnitudes the cost model is
# calibrated against (conftest.OPT_PREMIUM). Sizes are lot multiples because the
# exchange will not accept anything else.

OPTION_SECURITY_ID = 45_678
FUTURE_SECURITY_ID = 68_390
LOT = 30

BID_LEVELS: tuple[tuple[float, int, int], ...] = (
    (999.95, 90, 3),
    (999.90, 150, 4),
    (999.85, 300, 7),
    (999.80, 240, 5),
    (999.70, 600, 11),
    (999.60, 450, 8),
)
ASK_LEVELS: tuple[tuple[float, int, int], ...] = (
    (1000.15, 60, 2),
    (1000.20, 180, 5),
    (1000.25, 270, 6),
    (1000.35, 330, 9),
    (1000.45, 510, 10),
)


def _levels_body(levels: Sequence[tuple[float, int, int]], pad_to: int = DEPTH_LEVELS) -> bytes:
    """Pack levels and zero-pad to ``pad_to`` slots, as the feed always does."""
    if len(levels) > pad_to:
        raise ValueError(f"{len(levels)} levels will not fit in {pad_to} slots")
    body = b"".join(
        struct.pack(DEPTH_LEVEL_FMT, price, quantity, orders) for price, quantity, orders in levels
    )
    return body + struct.pack(DEPTH_LEVEL_FMT, 0.0, 0, 0) * (pad_to - len(levels))


def depth_frame(
    levels: Sequence[tuple[float, int, int]],
    *,
    side: Side = Side.BID,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    header_extra: int = 0,
    declared_length: int | None = None,
    body: bytes | None = None,
) -> bytes:
    """One 20-level depth frame, 332 bytes unless deliberately malformed.

    ``declared_length`` and ``body`` are the seams the malformed-input tests pull
    on: a header that lies about its own length, or a body of the wrong size.
    """
    payload = _levels_body(levels) if body is None else body
    length = DEPTH_HEADER_SIZE + len(payload) if declared_length is None else declared_length
    header = struct.pack(
        DEPTH_HEADER_FMT,
        length,
        MSG_BID if side is Side.BID else MSG_ASK,
        segment,
        security_id,
        header_extra,
    )
    return header + payload


def depth_book(
    *,
    bids: Sequence[tuple[float, int, int]] = BID_LEVELS,
    asks: Sequence[tuple[float, int, int]] = ASK_LEVELS,
    security_id: int = OPTION_SECURITY_ID,
) -> bytes:
    """A bid frame followed by its ask frame -- how a book actually arrives."""
    return depth_frame(bids, side=Side.BID, security_id=security_id) + depth_frame(
        asks, side=Side.ASK, security_id=security_id
    )


def depth_disconnect(reason_code: int = 806) -> bytes:
    """A 12-byte disconnect frame. The reason rides in the header's trailing uint32,
    which on a depth frame means something else entirely -- see protocol.py."""
    return struct.pack(DEPTH_HEADER_FMT, DEPTH_HEADER_SIZE, MSG_DISCONNECT, 0, 0, reason_code)


def depth_unknown(msg_code: int = 99, length: int = 24) -> bytes:
    """A frame with a code neither SDK decodes, whose length must be respected."""
    header = struct.pack(DEPTH_HEADER_FMT, length, msg_code, NSE_FNO, OPTION_SECURITY_ID, 0)
    return header + b"\x00" * (length - DEPTH_HEADER_SIZE)


# --- general market feed -------------------------------------------------------


def _depth5_blob(
    bids: Sequence[tuple[float, int, int]] = BID_LEVELS,
    asks: Sequence[tuple[float, int, int]] = ASK_LEVELS,
) -> bytes:
    """The 100-byte interleaved five-level block inside Full and Depth5 packets."""
    rows = b""
    for i in range(DEPTH_5_LEVELS):
        bp, bq, bo = bids[i] if i < len(bids) else (0.0, 0, 0)
        ap, aq, ao = asks[i] if i < len(asks) else (0.0, 0, 0)
        rows += struct.pack(DEPTH_5_LEVEL_FMT, bq, aq, bo, ao, bp, ap)
    return rows


def full_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    ltp: float = 1000.05,
    last_quantity: int = LOT,
    last_trade_epoch: int = 1_787_000_000,
    average_price: float = 999.80,
    volume: int = 1_234_560,
    total_sell_quantity: int = 45_900,
    total_buy_quantity: int = 51_300,
    open_interest: int = 2_100_000,
    oi_day_high: int = 2_250_000,
    oi_day_low: int = 1_980_000,
    open_: float = 985.30,
    close: float = 991.15,
    high: float = 1_012.40,
    low: float = 978.05,
    bids: Sequence[tuple[float, int, int]] = BID_LEVELS,
    asks: Sequence[tuple[float, int, int]] = ASK_LEVELS,
    declared_length: int | None = None,
) -> bytes:
    """A 162-byte ``Full`` packet: the trade side plus a five-level cross-check."""
    size = FEED_FRAME_SIZES[FEED_FULL]
    return struct.pack(
        FULL_FMT,
        FEED_FULL,
        size if declared_length is None else declared_length,
        segment,
        security_id,
        ltp,
        last_quantity,
        last_trade_epoch,
        average_price,
        volume,
        total_sell_quantity,
        total_buy_quantity,
        open_interest,
        oi_day_high,
        oi_day_low,
        open_,
        close,
        high,
        low,
        _depth5_blob(bids, asks),
    )


def quote_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    ltp: float = 1000.05,
    last_quantity: int = LOT,
    last_trade_epoch: int = 1_787_000_000,
    average_price: float = 999.80,
    volume: int = 1_234_560,
    total_sell_quantity: int = 45_900,
    total_buy_quantity: int = 51_300,
    open_: float = 985.30,
    close: float = 991.15,
    high: float = 1_012.40,
    low: float = 978.05,
) -> bytes:
    """A 50-byte ``Quote`` packet. Same trade fields as Full, **no open interest**."""
    return struct.pack(
        QUOTE_FMT,
        FEED_QUOTE,
        FEED_FRAME_SIZES[FEED_QUOTE],
        segment,
        security_id,
        ltp,
        last_quantity,
        last_trade_epoch,
        average_price,
        volume,
        total_sell_quantity,
        total_buy_quantity,
        open_,
        close,
        high,
        low,
    )


def ticker_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    ltp: float = 1000.05,
    last_trade_epoch: int = 1_787_000_000,
) -> bytes:
    return struct.pack(
        TICKER_FMT, FEED_TICKER, FEED_FRAME_SIZES[FEED_TICKER], segment, security_id, ltp,
        last_trade_epoch,
    )


def prev_close_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    prev_close: float = 991.15,
    prev_open_interest: int = 2_050_000,
) -> bytes:
    return struct.pack(
        PREV_CLOSE_FMT, FEED_PREV_CLOSE, FEED_FRAME_SIZES[FEED_PREV_CLOSE], segment, security_id,
        prev_close, prev_open_interest,
    )


def oi_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    open_interest: int = 2_100_000,
) -> bytes:
    return struct.pack(
        OI_FMT, FEED_OI, FEED_FRAME_SIZES[FEED_OI], segment, security_id, open_interest
    )


def status_frame(*, security_id: int = 0, segment: int = NSE_FNO) -> bytes:
    return struct.pack(STATUS_FMT, FEED_STATUS, FEED_FRAME_SIZES[FEED_STATUS], segment, security_id)


def depth5_frame(
    *,
    security_id: int = OPTION_SECURITY_ID,
    segment: int = NSE_FNO,
    ltp: float = 1000.05,
    bids: Sequence[tuple[float, int, int]] = BID_LEVELS,
    asks: Sequence[tuple[float, int, int]] = ASK_LEVELS,
) -> bytes:
    return struct.pack(
        DEPTH_5_FMT, FEED_DEPTH_5, FEED_FRAME_SIZES[FEED_DEPTH_5], segment, security_id, ltp,
        _depth5_blob(bids, asks),
    )


def feed_disconnect(reason_code: int = 806) -> bytes:
    """A 10-byte disconnect -- a *different* layout from the depth feed's 12-byte one."""
    return struct.pack(
        FEED_DISCONNECT_FMT, FEED_DISCONNECT, FEED_FRAME_SIZES[FEED_DISCONNECT], 0, 0, reason_code
    )


def feed_unknown(msg_code: int = 77, length: int = 20) -> bytes:
    header = struct.pack("<BHBI", msg_code, length, NSE_FNO, OPTION_SECURITY_ID)
    return header + b"\x00" * (length - 8)


# --- stream shaping ------------------------------------------------------------


def chunked(payload: bytes, size: int) -> list[bytes]:
    """Split a byte stream at arbitrary boundaries, which is what a socket does.

    Frame boundaries and ``recv()`` boundaries are unrelated, so the decoder is
    exercised at every offset a real session could produce.
    """
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [payload[i : i + size] for i in range(0, len(payload), size)]


def walking_book(
    n: int,
    *,
    security_id: int = OPTION_SECURITY_ID,
    start: float = 1000.00,
    tick: float = 0.05,
) -> bytes:
    """``n`` book updates whose touch drifts, for reconstruction and QA tests.

    Sizes shrink as the level ages so that queue-depletion logic has something to
    bite on; every quantity stays a lot multiple.
    """
    out = b""
    for i in range(n):
        mid = round(start + (i % 7 - 3) * tick, 2)
        bids = tuple(
            (round(mid - tick * (k + 1), 2), LOT * (k + 2 + i % 3), k + 2) for k in range(6)
        )
        asks = tuple(
            (round(mid + tick * (k + 1), 2), LOT * (k + 1 + (i + 1) % 3), k + 1) for k in range(6)
        )
        out += depth_book(bids=bids, asks=asks, security_id=security_id)
    return out


def volume_tape(
    prints: Sequence[tuple[float, int]],
    *,
    security_id: int = OPTION_SECURITY_ID,
    start_volume: int = 1_000_000,
    start_epoch: int = 1_787_000_000,
) -> bytes:
    """``Full`` packets whose cumulative volume advances by each print's size.

    This is the shape the fill simulator actually consumes: traded quantity is
    recovered by differencing ``volume``, not read off any single field.
    """
    out = b""
    volume = start_volume
    for i, (price, quantity) in enumerate(prints):
        volume += quantity
        out += full_frame(
            security_id=security_id,
            ltp=price,
            last_quantity=quantity,
            last_trade_epoch=start_epoch + i,
            volume=volume,
        )
    return out


__all__ = [
    "ASK_LEVELS",
    "BID_LEVELS",
    "FUTURE_SECURITY_ID",
    "LOT",
    "OPTION_SECURITY_ID",
    "chunked",
    "depth5_frame",
    "depth_book",
    "depth_disconnect",
    "depth_frame",
    "depth_unknown",
    "feed_disconnect",
    "feed_unknown",
    "full_frame",
    "oi_frame",
    "prev_close_frame",
    "quote_frame",
    "status_frame",
    "ticker_frame",
    "volume_tape",
    "walking_book",
]
