"""Binary wire decoding for Dhan's two market-data WebSockets. Pure functions.

Nothing here opens a socket, reads a file, or needs a credential, which is the
point: the whole protocol layer is testable before the Data API is ever paid for
(see README Status). ``tests/fixtures/packets.py`` builds well-formed frames from
these same format strings, and at the subscription gate a 60-second byte dump
replaces the synthetic fixture with a real golden file.

**Two feeds, not one.** The 20-level depth feed carries the order book and nothing
else -- no trade prints, no volume, no timestamp. Queue-position simulation needs
traded quantity at a price level, so capture must also run the general market feed
(``Full`` packets), whose cumulative ``volume`` field differences into per-interval
traded quantity and whose ``ltp`` gives the price of the most recent trade. The two
wire formats share nothing: different headers, different level layouts, different
disconnect frames, different price widths. They are decoded separately below and
reconciled in ``bnfmm.book``.

Layouts were read out of ``dhanhq``'s ``fulldepth.py`` and ``marketfeed.py`` rather
than from documentation, then cross-checked by ``struct.calcsize``. Three departures
from the SDK's own decoders, each a correctness fix rather than a preference:

1. **Framing is exhaustive.** ``MarketFeed.process_data`` decodes only the *first*
   packet in a ``recv()`` payload and discards the rest of the buffer;
   ``FullDepth.process_data`` threads a remainder but stops at the first frame it
   does not recognise, dropping everything after a disconnect frame. Both silently
   lose data. The decoders here consume a buffer to exhaustion and carry an
   incomplete trailing frame into the next chunk.
2. **Unknown message codes are skipped, counted, and survivable** rather than
   treated as end-of-buffer. A protocol addition should cost a QA note, not a
   session.
3. **float32 prices are snapped to paise.** The general feed sends prices as
   float32, so ``57785.05`` arrives as ``57785.05078125`` -- an error of 7.8e-4,
   which is small against a Rs 0.05 tick but fatal to any ``ltp == best_bid``
   comparison. NSE prices are paise-denominated, so rounding to 2 decimals
   *recovers* the intended value rather than losing precision. Depth-feed prices
   are float64 and are left exactly as sent.

The one field this module refuses to name is the depth header's trailing uint32.
The SDK reads it as ``no_of_rows`` on the 200-level feed and as the reason code on
a disconnect frame, and never reads it at all on 20-level depth frames -- so its
meaning there is unknown. It is exposed as ``header_extra``. **There is no sequence
number in this protocol**, which is why QA cannot check sequence monotonicity and
has to lean on the general feed's ``last_trade_epoch`` instead.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence
from urllib.parse import urlencode

# --- endpoints -----------------------------------------------------------------
# Both take the access token as a *query parameter*, so the connect URL is a
# credential. Log `redact_url(...)`, never the URL itself.

DEPTH_20_WSS = "wss://depth-api-feed.dhan.co/twentydepth"
DEPTH_200_WSS = "wss://full-depth-api.dhan.co/"
MARKET_FEED_WSS = "wss://api-feed.dhan.co"

# --- exchange segments ---------------------------------------------------------

SEGMENT_NAMES = {
    0: "IDX_I",
    1: "NSE_EQ",
    2: "NSE_FNO",
    3: "NSE_CURRENCY",
    4: "BSE_EQ",
    5: "MCX_COMM",
    7: "BSE_CURRENCY",
    8: "BSE_FNO",
}
NSE_FNO = 2

# --- disconnect reason codes, shared by both feeds -----------------------------

DISCONNECT_REASONS = {
    805: "no. of active websocket connections exceeded",
    806: "subscribe to Data APIs to continue",
    807: "access token is expired",
    808: "invalid client id",
    809: "authentication failed",
}

#: Codes where retrying the connection cannot help. 805 is included deliberately:
#: a suspended laptop leaves a half-open connection the server may still count, so
#: reconnecting into it burns the retry budget and can lock the run out entirely.
#: Treat it as fatal for this run and let a human clear it.
FATAL_DISCONNECTS = frozenset(DISCONNECT_REASONS)

#: The one code that answers the entitlement question at runtime, mirroring REST's
#: ``DH-902``. See ``scripts/check_entitlement.py``.
ENTITLEMENT_DISCONNECT = 806

# --- documented service limits -------------------------------------------------
#
# Read from the Dhan v2 documentation on 2026-08-23, not inferred from the SDK.
# They live here beside the wire format because they constrain what can legally be
# put on a socket, and they are asserted against rather than trusted: the capture
# universe is *enforced* against these numbers in ``universe.py`` so an arithmetic
# slip in a config file fails at resolve time instead of at 09:15 with an 805.

#: Concurrent WebSocket connections per user. A sixth connection does not fail --
#: it disconnects the *first* with 805, i.e. it kills the socket that is working.
MAX_WEBSOCKET_CONNECTIONS = 5

#: Hard per-connection cap on the 20-level depth feed. This is a *connection*
#: limit, not a rate limit: more instruments means more sockets, and there are
#: only five sockets.
DEPTH_MAX_INSTRUMENTS = 50

#: Per-connection cap on the general feed. Effectively unbounded for this project,
#: which is why the wide band lives on that channel.
FEED_MAX_INSTRUMENTS = 5000

#: The server closes a socket after this long without a client response, so any
#: keepalive interval must sit comfortably below it. ``reconnect.py`` re-exports
#: this and ``channels.py`` asserts its configured interval against it.
SERVER_SILENCE_TIMEOUT_S = 40.0

#: Option-chain REST throttle: one *unique* request per this many seconds. The
#: binding rate limit in the whole project, and finer than the resolution needed.
OPTION_CHAIN_MIN_INTERVAL_S = 3.0

# --- 20-level depth feed -------------------------------------------------------

DEPTH_HEADER_FMT = "<hBBiI"  # msg_length, msg_code, segment, security_id, extra
DEPTH_HEADER_SIZE = struct.calcsize(DEPTH_HEADER_FMT)  # 12
DEPTH_LEVEL_FMT = "<dII"  # float64 price, uint32 quantity, uint32 order count
DEPTH_LEVEL_SIZE = struct.calcsize(DEPTH_LEVEL_FMT)  # 16
DEPTH_LEVELS = 20
DEPTH_FRAME_SIZE = DEPTH_HEADER_SIZE + DEPTH_LEVELS * DEPTH_LEVEL_SIZE  # 332

MSG_BID = 41
MSG_ASK = 51
MSG_DISCONNECT = 50

#: Frame size per message code. Used as the framing authority in preference to the
#: header's own ``msg_length``, because the SDK never reads that field, so nothing
#: confirms the server fills it correctly. Disagreements are counted, not trusted.
DEPTH_FRAME_SIZES = {
    MSG_BID: DEPTH_FRAME_SIZE,
    MSG_ASK: DEPTH_FRAME_SIZE,
    MSG_DISCONNECT: DEPTH_HEADER_SIZE,
}

DEPTH_SUBSCRIBE_CODE = 23
DEPTH_BATCH_SIZE = 50  # 1 for the 200-level feed, which this project does not use

# --- general market feed -------------------------------------------------------

FEED_HEADER_FMT = "<BHBI"  # msg_code, msg_length, segment, security_id
FEED_HEADER_SIZE = struct.calcsize(FEED_HEADER_FMT)  # 8

FEED_TICKER = 2
FEED_DEPTH_5 = 3
FEED_QUOTE = 4
FEED_OI = 5
FEED_PREV_CLOSE = 6
FEED_STATUS = 7
FEED_FULL = 8
FEED_DISCONNECT = 50

TICKER_FMT = "<BHBIfI"
DEPTH_5_FMT = "<BHBIf100s"
QUOTE_FMT = "<BHBIfHIfIIIffff"
OI_FMT = "<BHBII"
PREV_CLOSE_FMT = "<BHBIfI"
STATUS_FMT = "<BHBI"
FULL_FMT = "<BHBIfHIfIIIIIIffff100s"
FEED_DISCONNECT_FMT = "<BHBIH"

#: Interleaved bid/ask, five levels, inside ``Full`` and ``Depth5`` packets. Note
#: this is a *different* layout from the 20-level feed: quantities first, prices
#: last, float32, and both sides in one packet.
DEPTH_5_LEVEL_FMT = "<IIHHff"
DEPTH_5_LEVEL_SIZE = struct.calcsize(DEPTH_5_LEVEL_FMT)  # 20
DEPTH_5_LEVELS = 5

FEED_FRAME_SIZES = {
    FEED_TICKER: struct.calcsize(TICKER_FMT),  # 16
    FEED_DEPTH_5: struct.calcsize(DEPTH_5_FMT),  # 112
    FEED_QUOTE: struct.calcsize(QUOTE_FMT),  # 50
    FEED_OI: struct.calcsize(OI_FMT),  # 12
    FEED_PREV_CLOSE: struct.calcsize(PREV_CLOSE_FMT),  # 16
    FEED_STATUS: struct.calcsize(STATUS_FMT),  # 8
    FEED_FULL: struct.calcsize(FULL_FMT),  # 162
    FEED_DISCONNECT: struct.calcsize(FEED_DISCONNECT_FMT),  # 10
}

REQUEST_TICKER = 15
REQUEST_QUOTE = 17
REQUEST_DEPTH_5 = 19
REQUEST_FULL = 21
REQUEST_DISCONNECT = 12
FEED_BATCH_SIZE = 100

#: What capture subscribes to. ``Full`` is the only packet carrying open interest
#: *and* last-traded quantity *and* a five-level book, so one subscription gives
#: both the trade stream the fill simulator needs and a free cross-check on the
#: 20-level decoder. ``Quote`` (50 bytes) omits open interest.
CAPTURE_REQUEST_CODE = REQUEST_FULL

#: float32 prices are snapped to this many decimals. See the module docstring.
PRICE_DP = 2


class ProtocolError(ValueError):
    """A buffer that cannot be resynchronised.

    Neither feed frames with a magic marker or a checksum, so once the stream is
    misaligned there is no way to recover position within it. Capture treats this
    as "drop the connection and start a new segment" rather than trying to guess.
    """


class Side(Enum):
    BID = "bid"
    ASK = "ask"


def redact_url(url: str) -> str:
    """The connect URL carries the access token, so this is what gets logged."""
    scrubbed = url
    for key in ("token", "access_token", "clientId"):
        marker = f"{key}="
        start = scrubbed.find(marker)
        while start != -1:
            end = scrubbed.find("&", start)
            end = len(scrubbed) if end == -1 else end
            scrubbed = f"{scrubbed[: start + len(marker)]}<redacted>{scrubbed[end:]}"
            start = scrubbed.find(marker, start + len(marker) + len("<redacted>"))
    return scrubbed


def feed_url(base: str, *, token: str, client_id: str, version: int | None = None) -> str:
    """Build a connect URL. **Never log the return value** -- use `redact_url`."""
    params: dict[str, str] = {}
    if version is not None:
        params["version"] = str(version)
    params.update({"token": token, "clientId": client_id, "authType": "2"})
    return f"{base}?{urlencode(params)}"


def depth_url(*, token: str, client_id: str) -> str:
    return feed_url(DEPTH_20_WSS, token=token, client_id=client_id)


def market_feed_url(*, token: str, client_id: str) -> str:
    return feed_url(MARKET_FEED_WSS, token=token, client_id=client_id, version=2)


# --- subscription messages -----------------------------------------------------

#: One subscription entry: exchange segment code plus a security id. The id is
#: accepted as ``int`` or ``str`` because the wire format wants a string and
#: ``instruments.Contract.feed_key`` already produces one, while hand-written
#: call sites and tests naturally use ints. ``_instrument_list`` normalises.
InstrumentKey = tuple[int, int | str]


def _batched(items: Sequence[InstrumentKey], size: int) -> list[list[tuple[int, str]]]:
    # Normalise the id to the wire form *before* de-duplicating. Since the id may
    # arrive as an int or a str, de-duplicating the raw tuples would let
    # ``(2, 45678)`` and ``(2, "45678")`` both through as distinct keys and
    # subscribe the same instrument twice -- which spends two of the fifty depth
    # slots on one contract and is invisible in the reply.
    normalised = [(int(segment), str(security_id)) for segment, security_id in items]
    ordered = list(dict.fromkeys(normalised))  # de-duplicate, preserving order
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def _instrument_list(batch: Iterable[InstrumentKey]) -> list[dict[str, object]]:
    out = []
    for segment, security_id in batch:
        if segment not in SEGMENT_NAMES:
            raise ValueError(f"unknown exchange segment {segment!r}")
        out.append({"ExchangeSegment": SEGMENT_NAMES[segment], "SecurityId": str(security_id)})
    return out


def depth_subscriptions(instruments: Sequence[InstrumentKey]) -> list[dict[str, object]]:
    """Subscription messages for the 20-level feed, batched at 50 instruments."""
    return [
        {
            "RequestCode": DEPTH_SUBSCRIBE_CODE,
            "InstrumentCount": len(batch),
            "InstrumentList": _instrument_list(batch),
        }
        for batch in _batched(instruments, DEPTH_BATCH_SIZE)
    ]


def feed_subscriptions(
    instruments: Sequence[InstrumentKey], request_code: int = CAPTURE_REQUEST_CODE
) -> list[dict[str, object]]:
    """Subscription messages for the general feed, batched at 100 instruments."""
    return [
        {
            "RequestCode": request_code,
            "InstrumentCount": len(batch),
            "InstrumentList": _instrument_list(batch),
        }
        for batch in _batched(instruments, FEED_BATCH_SIZE)
    ]


DISCONNECT_MESSAGE: dict[str, object] = {"RequestCode": REQUEST_DISCONNECT}


# --- decoded frames ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Level:
    """One price level of the 20-deep book. ``orders`` is what makes queue-length
    inference possible at all -- it is why this project uses Dhan (DECISIONS #2)."""

    price: float
    quantity: int
    orders: int

    @property
    def average_order_size(self) -> float:
        return self.quantity / self.orders if self.orders else 0.0


@dataclass(frozen=True, slots=True)
class DepthSide:
    """One side of the book from one 20-level frame.

    Bids and asks arrive as *separate* frames and are paired downstream by
    ``security_id``; a side is therefore never a complete book on its own.
    """

    security_id: int
    exchange_segment: int
    side: Side
    levels: tuple[Level, ...]
    padding_rows: int
    anomalous_rows: int
    interleaved_padding: bool
    header_extra: int
    declared_length: int

    @property
    def best(self) -> Level | None:
        return self.levels[0] if self.levels else None

    @property
    def total_quantity(self) -> int:
        return sum(lvl.quantity for lvl in self.levels)

    @property
    def is_sorted(self) -> bool:
        """Bids should descend and asks ascend. ``combine_and_format_depth`` sorts
        defensively, which implies the wire order is not guaranteed -- so this is
        measured on real data rather than assumed."""
        prices = [lvl.price for lvl in self.levels]
        if self.side is Side.BID:
            return all(a > b for a, b in zip(prices, prices[1:]))
        return all(a < b for a, b in zip(prices, prices[1:]))

    @property
    def is_clean(self) -> bool:
        return (
            not self.anomalous_rows
            and not self.interleaved_padding
            and len(self.levels) + self.padding_rows == DEPTH_LEVELS
        )


@dataclass(frozen=True, slots=True)
class Depth5Level:
    bid_quantity: int
    ask_quantity: int
    bid_orders: int
    ask_orders: int
    bid_price: float
    ask_price: float


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A ``Full`` (or ``Quote``) packet: the trade side of the world.

    ``volume`` is **cumulative for the day**, so traded quantity over an interval
    is a difference between consecutive snapshots -- and ``last_quantity`` is only
    the most recent print, which undercounts whenever more than one trade lands
    between packets. That gap is why the fill simulator bounds rather than
    estimates: the *total* traded quantity is observable, its distribution across
    price levels is not.
    """

    security_id: int
    exchange_segment: int
    ltp: float
    last_quantity: int
    last_trade_epoch: int
    average_price: float
    volume: int
    total_sell_quantity: int
    total_buy_quantity: int
    open: float
    close: float
    high: float
    low: float
    open_interest: int | None = None
    oi_day_high: int | None = None
    oi_day_low: int | None = None
    depth5: tuple[Depth5Level, ...] = ()
    msg_code: int = FEED_FULL

    @property
    def has_open_interest(self) -> bool:
        return self.open_interest is not None

    @property
    def best_bid(self) -> float | None:
        return self.depth5[0].bid_price if self.depth5 else None

    @property
    def best_ask(self) -> float | None:
        return self.depth5[0].ask_price if self.depth5 else None


@dataclass(frozen=True, slots=True)
class Ticker:
    security_id: int
    exchange_segment: int
    ltp: float
    last_trade_epoch: int


@dataclass(frozen=True, slots=True)
class OpenInterest:
    security_id: int
    exchange_segment: int
    open_interest: int


@dataclass(frozen=True, slots=True)
class PrevClose:
    security_id: int
    exchange_segment: int
    prev_close: float
    prev_open_interest: int


@dataclass(frozen=True, slots=True)
class MarketStatus:
    security_id: int
    exchange_segment: int


@dataclass(frozen=True, slots=True)
class Depth5Snapshot:
    security_id: int
    exchange_segment: int
    ltp: float
    depth5: tuple[Depth5Level, ...]


@dataclass(frozen=True, slots=True)
class Disconnect:
    """A server-initiated close. Carries *why*, which decides whether to retry."""

    reason_code: int

    @property
    def reason(self) -> str:
        return DISCONNECT_REASONS.get(self.reason_code, f"unknown reason {self.reason_code}")

    @property
    def is_fatal(self) -> bool:
        return self.reason_code in FATAL_DISCONNECTS

    @property
    def is_entitlement(self) -> bool:
        return self.reason_code == ENTITLEMENT_DISCONNECT

    def __str__(self) -> str:
        return f"disconnected ({self.reason_code}): {self.reason}"


@dataclass(frozen=True, slots=True)
class UnknownFrame:
    """A message code neither feed's SDK decodes. Skipped, not fatal."""

    msg_code: int
    length: int


Frame = (
    DepthSide
    | Snapshot
    | Ticker
    | OpenInterest
    | PrevClose
    | MarketStatus
    | Depth5Snapshot
    | Disconnect
    | UnknownFrame
)


@dataclass
class DecodeStats:
    """What the decoder saw. Feeds ``reports/data_quality.md``.

    A capture that reports zero anomalies is only credible if the counters exist
    to have reported some.
    """

    chunks: int = 0
    bytes_in: int = 0
    frames: int = 0
    partial_carries: int = 0
    by_code: Counter[int] = field(default_factory=Counter)
    unknown_codes: Counter[int] = field(default_factory=Counter)
    length_mismatches: Counter[int] = field(default_factory=Counter)
    padding_anomalies: int = 0
    unsorted_sides: int = 0

    @property
    def clean(self) -> bool:
        return not (
            self.unknown_codes
            or self.length_mismatches
            or self.padding_anomalies
            or self.unsorted_sides
        )

    def summary(self) -> str:
        parts = [f"{self.frames} frames from {self.bytes_in} bytes in {self.chunks} chunks"]
        if self.partial_carries:
            parts.append(f"{self.partial_carries} partial carries")
        if self.unknown_codes:
            parts.append(f"unknown codes {dict(self.unknown_codes)}")
        if self.length_mismatches:
            parts.append(f"declared-length mismatches {dict(self.length_mismatches)}")
        if self.padding_anomalies:
            parts.append(f"{self.padding_anomalies} padding anomalies")
        if self.unsorted_sides:
            parts.append(f"{self.unsorted_sides} unsorted sides")
        return "; ".join(parts)


# --- 20-level depth decoding ---------------------------------------------------


def _split_levels(body: bytes) -> tuple[tuple[Level, ...], int, int, bool]:
    """Separate real levels from the zero padding the feed always sends.

    All 20 slots are transmitted regardless of book depth, so padding is normal
    and a *real level after a padding row* is not. Both are reported: silently
    filtering the anomaly is how a malformed book becomes a plausible one.
    """
    levels: list[Level] = []
    padding = 0
    anomalous = 0
    interleaved = False
    seen_padding = False

    for i in range(DEPTH_LEVELS):
        price, quantity, orders = struct.unpack_from(DEPTH_LEVEL_FMT, body, i * DEPTH_LEVEL_SIZE)
        if price == 0.0 and quantity == 0:
            padding += 1
            seen_padding = True
        elif price > 0.0 and quantity > 0:
            if seen_padding:
                interleaved = True
            levels.append(Level(price=price, quantity=quantity, orders=orders))
        else:
            anomalous += 1

    return tuple(levels), padding, anomalous, interleaved


def decode_depth_frames(buf: bytes) -> tuple[list[Frame], bytes, DecodeStats]:
    """Decode as many 20-level frames as ``buf`` holds; return the leftover bytes.

    The leftover is the whole point. A WebSocket ``recv()`` boundary has nothing to
    do with a frame boundary, so a 332-byte frame routinely straddles two chunks.
    """
    stats = DecodeStats(chunks=1, bytes_in=len(buf))
    frames: list[Frame] = []
    offset = 0

    while offset + DEPTH_HEADER_SIZE <= len(buf):
        declared, msg_code, segment, security_id, extra = struct.unpack_from(
            DEPTH_HEADER_FMT, buf, offset
        )
        size = DEPTH_FRAME_SIZES.get(msg_code)

        if size is None:
            if declared <= 0 or declared < DEPTH_HEADER_SIZE:
                raise ProtocolError(
                    f"unknown depth message code {msg_code} with unusable declared "
                    f"length {declared} at offset {offset}: the stream cannot be "
                    "resynchronised, so this connection must be dropped"
                )
            size = declared

        if offset + size > len(buf):
            break  # incomplete frame; carry it

        if declared != size:
            stats.length_mismatches[msg_code] += 1

        if msg_code in (MSG_BID, MSG_ASK):
            levels, padding, anomalous, interleaved = _split_levels(
                buf[offset + DEPTH_HEADER_SIZE : offset + size]
            )
            side = DepthSide(
                security_id=security_id,
                exchange_segment=segment,
                side=Side.BID if msg_code == MSG_BID else Side.ASK,
                levels=levels,
                padding_rows=padding,
                anomalous_rows=anomalous,
                interleaved_padding=interleaved,
                header_extra=extra,
                declared_length=declared,
            )
            if not side.is_clean:
                stats.padding_anomalies += 1
            if not side.is_sorted:
                stats.unsorted_sides += 1
            frames.append(side)
        elif msg_code == MSG_DISCONNECT:
            frames.append(Disconnect(reason_code=extra))
        else:
            frames.append(UnknownFrame(msg_code=msg_code, length=size))
            stats.unknown_codes[msg_code] += 1

        stats.by_code[msg_code] += 1
        offset += size

    stats.frames = len(frames)
    remainder = buf[offset:]
    if remainder:
        stats.partial_carries = 1
    return frames, remainder, stats


# --- general feed decoding -----------------------------------------------------


def _px(raw: float) -> float:
    """Undo float32 quantisation on a paise-denominated price. See the docstring."""
    return round(raw, PRICE_DP)


def _depth5(blob: bytes) -> tuple[Depth5Level, ...]:
    out = []
    for i in range(DEPTH_5_LEVELS):
        bq, aq, bo, ao, bp, ap = struct.unpack_from(DEPTH_5_LEVEL_FMT, blob, i * DEPTH_5_LEVEL_SIZE)
        out.append(
            Depth5Level(
                bid_quantity=bq,
                ask_quantity=aq,
                bid_orders=bo,
                ask_orders=ao,
                bid_price=_px(bp),
                ask_price=_px(ap),
            )
        )
    return tuple(out)


def _decode_feed_frame(buf: bytes, offset: int, msg_code: int, size: int) -> Frame:
    chunk = buf[offset : offset + size]

    if msg_code == FEED_FULL:
        f = struct.unpack(FULL_FMT, chunk)
        return Snapshot(
            security_id=f[3],
            exchange_segment=f[2],
            ltp=_px(f[4]),
            last_quantity=f[5],
            last_trade_epoch=f[6],
            average_price=_px(f[7]),
            volume=f[8],
            total_sell_quantity=f[9],
            total_buy_quantity=f[10],
            open_interest=f[11],
            oi_day_high=f[12],
            oi_day_low=f[13],
            open=_px(f[14]),
            close=_px(f[15]),
            high=_px(f[16]),
            low=_px(f[17]),
            depth5=_depth5(f[18]),
            msg_code=FEED_FULL,
        )

    if msg_code == FEED_QUOTE:
        f = struct.unpack(QUOTE_FMT, chunk)
        return Snapshot(
            security_id=f[3],
            exchange_segment=f[2],
            ltp=_px(f[4]),
            last_quantity=f[5],
            last_trade_epoch=f[6],
            average_price=_px(f[7]),
            volume=f[8],
            total_sell_quantity=f[9],
            total_buy_quantity=f[10],
            open=_px(f[11]),
            close=_px(f[12]),
            high=_px(f[13]),
            low=_px(f[14]),
            msg_code=FEED_QUOTE,
        )

    if msg_code == FEED_TICKER:
        f = struct.unpack(TICKER_FMT, chunk)
        return Ticker(
            security_id=f[3], exchange_segment=f[2], ltp=_px(f[4]), last_trade_epoch=f[5]
        )

    if msg_code == FEED_PREV_CLOSE:
        f = struct.unpack(PREV_CLOSE_FMT, chunk)
        return PrevClose(
            security_id=f[3],
            exchange_segment=f[2],
            prev_close=_px(f[4]),
            prev_open_interest=f[5],
        )

    if msg_code == FEED_OI:
        f = struct.unpack(OI_FMT, chunk)
        return OpenInterest(security_id=f[3], exchange_segment=f[2], open_interest=f[4])

    if msg_code == FEED_STATUS:
        f = struct.unpack(STATUS_FMT, chunk)
        return MarketStatus(security_id=f[3], exchange_segment=f[2])

    if msg_code == FEED_DEPTH_5:
        f = struct.unpack(DEPTH_5_FMT, chunk)
        return Depth5Snapshot(
            security_id=f[3], exchange_segment=f[2], ltp=_px(f[4]), depth5=_depth5(f[5])
        )

    if msg_code == FEED_DISCONNECT:
        f = struct.unpack(FEED_DISCONNECT_FMT, chunk)
        return Disconnect(reason_code=f[4])

    raise AssertionError(f"unreachable: code {msg_code} is in FEED_FRAME_SIZES but undecoded")


def decode_feed_frames(buf: bytes) -> tuple[list[Frame], bytes, DecodeStats]:
    """Decode as many general-feed packets as ``buf`` holds; return the leftover.

    The SDK's equivalent reads ``data[0:1]``, decodes one packet, and returns --
    so every additional packet in the same ``recv()`` is lost. At option-chain
    subscription counts that is most of the feed.
    """
    stats = DecodeStats(chunks=1, bytes_in=len(buf))
    frames: list[Frame] = []
    offset = 0

    while offset + FEED_HEADER_SIZE <= len(buf):
        msg_code, declared, _segment, _security_id = struct.unpack_from(
            FEED_HEADER_FMT, buf, offset
        )
        size = FEED_FRAME_SIZES.get(msg_code)

        if size is None:
            if declared < FEED_HEADER_SIZE:
                raise ProtocolError(
                    f"unknown feed message code {msg_code} with unusable declared "
                    f"length {declared} at offset {offset}: the stream cannot be "
                    "resynchronised, so this connection must be dropped"
                )
            size = declared

        if offset + size > len(buf):
            break

        if declared != size:
            stats.length_mismatches[msg_code] += 1

        if msg_code in FEED_FRAME_SIZES:
            frames.append(_decode_feed_frame(buf, offset, msg_code, size))
        else:
            frames.append(UnknownFrame(msg_code=msg_code, length=size))
            stats.unknown_codes[msg_code] += 1

        stats.by_code[msg_code] += 1
        offset += size

    stats.frames = len(frames)
    remainder = buf[offset:]
    if remainder:
        stats.partial_carries = 1
    return frames, remainder, stats


# --- stateful wrappers ---------------------------------------------------------


class StreamDecoder:
    """Holds the partial-frame remainder and the running counters across chunks.

    Capture feeds this whatever ``recv()`` returns and gets back whole frames.
    Constructed with one of the two pure decoders above so the framing logic has
    exactly one implementation per feed.
    """

    def __init__(self, decoder, name: str) -> None:
        self._decode = decoder
        self.name = name
        self._buffer = b""
        self.stats = DecodeStats()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer += chunk
        frames, self._buffer, chunk_stats = self._decode(self._buffer)

        self.stats.chunks += 1
        self.stats.bytes_in += len(chunk)
        self.stats.frames += len(frames)
        self.stats.partial_carries += chunk_stats.partial_carries
        self.stats.by_code.update(chunk_stats.by_code)
        self.stats.unknown_codes.update(chunk_stats.unknown_codes)
        self.stats.length_mismatches.update(chunk_stats.length_mismatches)
        self.stats.padding_anomalies += chunk_stats.padding_anomalies
        self.stats.unsorted_sides += chunk_stats.unsorted_sides
        return frames

    @property
    def pending_bytes(self) -> int:
        """Non-zero at the end of a session means the stream was cut mid-frame."""
        return len(self._buffer)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name}, pending={self.pending_bytes})"


def depth_decoder() -> StreamDecoder:
    return StreamDecoder(decode_depth_frames, "twentydepth")


def market_feed_decoder() -> StreamDecoder:
    return StreamDecoder(decode_feed_frames, "marketfeed")
