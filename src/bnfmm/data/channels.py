"""Async capture channels: one socket (or one poller) each, with a raw log first.

The whole point of this module is the *order of operations* inside the receive
loop, which is the same in every channel:

1. Read the two clocks, immediately, before anything else touches the bytes.
2. **Write the bytes to the raw log.** Unconditionally, before decoding.
3. Decode, and count what fails.
4. Write Parquet, inside a guard, so a pyarrow problem cannot kill capture.

Steps 3 and 4 are best-effort; steps 1 and 2 are not. That inversion is the
entire safety argument for running a capture whose decoders have never seen real
bytes: a decoder bug becomes an offline re-run rather than a lost trading day.

**Testability is the other design constraint, and it is not decoration.** The
failure modes that matter here -- a subscription that is never sent, a
reconnect loop that hammers a refused socket, a watchdog that never fires --
are all invisible until the one morning that cannot be repeated. So the
connection is an injected callable returning anything with ``send``/``recv``,
and the clocks and ``sleep`` are injected too. A scripted fake socket then
drives subscribe, receive, disconnect, staleness and reconnect paths in
milliseconds, with no network and no real time.

**Two keepalives, because they fail differently.** The vendor SDK keeps its
sockets alive with a protocol-level ``ws.ping()``, and the ``websockets``
library does that automatically on a timer, so there is no hand-rolled ping
task here -- only an assertion that the configured interval sits under Dhan's
40 s silence limit. What the library ping does *not* catch is a laptop that
suspends: the TCP connection can survive, pongs can resume, and no market data
flows at all. That is indistinguishable from a healthy quiet feed unless
something is watching the data itself, which is why staleness is measured on
**bytes received** rather than on pongs.

On staleness the socket is closed *explicitly* before reconnecting. Skipping
that is what turns a resumed laptop into an 805: a half-open connection the
server still counts, plus a new one, and the server drops the oldest -- the one
that was working.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol, Sequence

from . import protocol as proto
from .protocol import (
    DISCONNECT_MESSAGE,
    Disconnect,
    Frame,
    ProtocolError,
    SERVER_SILENCE_TIMEOUT_S,
    StreamDecoder,
    depth_decoder,
    depth_subscriptions,
    depth_url,
    feed_subscriptions,
    market_feed_decoder,
    market_feed_url,
    redact_url,
)
from .rawlog import RawLogWriter
from .reconnect import Backoff, ReconnectPolicy

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class WebSocketLike(Protocol):
    """The three methods this module needs from a WebSocket connection.

    Narrow on purpose: the smaller this surface, the less a test fake has to
    imitate, and the less the capture path depends on a library version.
    """

    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> bytes | str: ...

    async def close(self) -> None: ...


#: ``connect(url)`` -> an open connection. Injected so tests never open a socket.
Connector = Callable[[str], Awaitable[WebSocketLike]]


def websockets_connector(
    *,
    ping_interval_s: float,
    ping_timeout_s: float,
    open_timeout_s: float = 20.0,
    max_size: int | None = None,
) -> Connector:
    """Build a connector over ``websockets``, with the keepalive policy checked.

    The assertion is the point: an interval above Dhan's 40 s silence limit means
    the server closes the socket on a schedule, and that failure looks exactly
    like a flaky network from the outside.
    """
    if not 0 < ping_interval_s < SERVER_SILENCE_TIMEOUT_S:
        raise ValueError(
            f"ping_interval_s={ping_interval_s} must be in (0, {SERVER_SILENCE_TIMEOUT_S}); "
            f"the server closes a socket after {SERVER_SILENCE_TIMEOUT_S}s of client silence"
        )
    if ping_timeout_s <= 0:
        raise ValueError("ping_timeout_s must be positive")

    async def connect(url: str) -> WebSocketLike:
        from websockets.asyncio.client import connect as ws_connect

        return await ws_connect(
            url,
            ping_interval=ping_interval_s,
            ping_timeout=ping_timeout_s,
            open_timeout=open_timeout_s,
            # Dhan's largest documented frame is 332 bytes, but TCP coalescing
            # hands us whatever it likes; no cap avoids a spurious close on a
            # busy open.
            max_size=max_size,
        )

    return connect


@dataclass(slots=True)
class ChannelStats:
    """Per-channel counters, written into the session manifest.

    Every counter here exists because its absence would hide something: bytes
    without frames means a decode problem, frames without rows means a store
    problem, and reconnects without either means the socket is being refused.
    """

    connects: int = 0
    subscriptions_sent: int = 0
    chunks: int = 0
    bytes_in: int = 0
    frames: int = 0
    rows: int = 0
    raw_records: int = 0
    disconnect_frames: int = 0
    decode_errors: int = 0
    store_errors: int = 0
    stale_timeouts: int = 0
    connect_errors: int = 0
    first_data_wall_ns: int | None = None
    last_data_wall_ns: int | None = None
    last_reason_code: int | None = None

    @property
    def had_data(self) -> bool:
        return self.chunks > 0

    def summary(self) -> str:
        parts = [
            f"{self.connects} connect(s)",
            f"{self.chunks} chunks / {self.bytes_in} bytes",
            f"{self.frames} frames / {self.rows} rows",
        ]
        if self.decode_errors:
            parts.append(f"{self.decode_errors} DECODE ERRORS")
        if self.store_errors:
            parts.append(f"{self.store_errors} STORE ERRORS")
        if self.stale_timeouts:
            parts.append(f"{self.stale_timeouts} stale timeouts")
        if self.disconnect_frames:
            parts.append(f"{self.disconnect_frames} disconnect frames")
        return "; ".join(parts)

    def manifest(self) -> dict:
        return {
            "connects": self.connects,
            "subscriptions_sent": self.subscriptions_sent,
            "chunks": self.chunks,
            "bytes_in": self.bytes_in,
            "frames": self.frames,
            "rows": self.rows,
            "raw_records": self.raw_records,
            "disconnect_frames": self.disconnect_frames,
            "decode_errors": self.decode_errors,
            "store_errors": self.store_errors,
            "stale_timeouts": self.stale_timeouts,
            "connect_errors": self.connect_errors,
            "first_data_wall_ns": self.first_data_wall_ns,
            "last_data_wall_ns": self.last_data_wall_ns,
            "last_reason_code": self.last_reason_code,
        }


class _Stop(Exception):
    """Internal: the run should end cleanly rather than reconnect."""


@dataclass
class FeedChannel:
    """One WebSocket feed: connect, subscribe, receive, log, decode, store, repeat.

    ``store_append`` is separate from the raw log so that the two outputs fail
    independently -- a pyarrow exception increments a counter and the session
    keeps recording bytes. ``None`` disables the Parquet path entirely, which is
    the ``--no-parquet`` escape hatch.
    """

    name: str
    url: str
    subscriptions: Sequence[dict]
    connect: Connector
    rawlog: RawLogWriter
    decoder: StreamDecoder
    store_append: Callable[[Sequence[Frame], int, int], int] | None = None
    policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    stale_after_s: float | None = 45.0
    subscribe_pause_s: float = 0.05
    send_disconnect_on_close: bool = True
    log: Logger = _noop
    wall_ns: Callable[[], int] = time.time_ns
    mono_ns: Callable[[], int] = time.monotonic_ns
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    stats: ChannelStats = field(default_factory=ChannelStats)
    on_frames: Callable[[Sequence[Frame], int, int], None] | None = None

    def __post_init__(self) -> None:
        if self.stale_after_s is not None and self.stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive or None")

    # -- public ------------------------------------------------------------

    async def run(
        self,
        *,
        stop: asyncio.Event | None = None,
        duration_s: float | None = None,
        max_connections: int | None = None,
    ) -> ChannelStats:
        """Run until ``stop`` is set, ``duration_s`` elapses, or a fatal drop.

        ``duration_s`` is measured on the monotonic clock, so a suspend or an NTP
        step cannot end a session early or extend it.
        """
        deadline = None if duration_s is None else self.mono_ns() + int(duration_s * 1e9)

        while True:
            if stop is not None and stop.is_set():
                self.log(f"[{self.name}] stop requested")
                return self.stats
            if deadline is not None and self.mono_ns() >= deadline:
                self.log(f"[{self.name}] duration reached")
                return self.stats
            if max_connections is not None and self.stats.connects >= max_connections:
                self.log(f"[{self.name}] connection budget of {max_connections} used")
                return self.stats

            reason_code: int | None = None
            try:
                reason_code = await self._session(stop=stop, deadline=deadline)
            except _Stop:
                return self.stats
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure is a reconnect candidate
                self.stats.connect_errors += 1
                self.log(f"[{self.name}] connection failed: {type(exc).__name__}: {exc}")

            attempt = self.policy.on_disconnect(reason_code)
            if not attempt.retry:
                # Loud, and with the operator instruction attached: 806 means the
                # subscription, 805 means a stale socket, 807-809 mean credentials.
                self.log(f"[{self.name}] NOT RECONNECTING -- {attempt.reason}")
                if attempt.needs_operator:
                    self.log(f"[{self.name}] operator action required before this channel can run")
                return self.stats

            self.log(
                f"[{self.name}] reconnecting in {attempt.delay_s:.1f}s "
                f"(attempt {attempt.attempt}): {attempt.reason}"
            )
            await self.sleep(attempt.delay_s)

    # -- one connection ----------------------------------------------------

    async def _session(
        self,
        *,
        stop: asyncio.Event | None,
        deadline: int | None,
    ) -> int | None:
        self.log(f"[{self.name}] connecting to {redact_url(self.url)}")
        ws = await self.connect(self.url)
        self.stats.connects += 1
        try:
            await self._subscribe(ws)
            return await self._receive(ws, stop=stop, deadline=deadline)
        finally:
            await self._close(ws)

    async def _subscribe(self, ws: WebSocketLike) -> None:
        for message in self.subscriptions:
            await ws.send(json.dumps(message))
            self.stats.subscriptions_sent += 1
            if self.subscribe_pause_s:
                await self.sleep(self.subscribe_pause_s)
        count = sum(int(m.get("InstrumentCount", 1)) for m in self.subscriptions)
        self.log(
            f"[{self.name}] sent {len(self.subscriptions)} subscription message(s), "
            f"{count} instrument(s)"
        )

    async def _receive(
        self,
        ws: WebSocketLike,
        *,
        stop: asyncio.Event | None,
        deadline: int | None,
    ) -> int | None:
        while True:
            if stop is not None and stop.is_set():
                raise _Stop
            if deadline is not None and self.mono_ns() >= deadline:
                raise _Stop

            # The recv wait must be bounded by the *sooner* of the staleness budget
            # and the time left in the session. Checking the deadline only between
            # recvs is not enough: on a market that has not opened, `recv()` never
            # returns, so a loop that blocks on it indefinitely can never notice the
            # deadline it was given. That is an unconditional hang, and it is exactly
            # what `--connect-test` (which disables the watchdog) walks into.
            budget = self.stale_after_s
            if deadline is not None:
                remaining_s = (deadline - self.mono_ns()) / 1e9
                if remaining_s <= 0:
                    raise _Stop
                budget = remaining_s if budget is None else min(budget, remaining_s)

            try:
                if budget is None:
                    chunk = await ws.recv()
                else:
                    chunk = await asyncio.wait_for(ws.recv(), timeout=budget)
            except asyncio.TimeoutError:
                # Two unrelated events raise the same exception here: a stale feed
                # and a finished session. They want opposite handling -- reconnect
                # versus stop -- so ask the clock which it was rather than inferring
                # it from whichever budget happened to be smaller.
                if deadline is not None and self.mono_ns() >= deadline:
                    raise _Stop from None
                self.stats.stale_timeouts += 1
                # Not an error yet on a market that has not opened -- so the
                # message says which case it is, and the operator can tell a
                # quiet feed from a suspended one without reading the Parquet.
                state = "no data since connect" if not self.stats.had_data else "data stopped"
                self.log(
                    f"[{self.name}] STALE: {state} for {budget:.0f}s, closing and reconnecting"
                )
                return None

            reason_code = self._handle(chunk)
            if reason_code is not None:
                return reason_code

    def _handle(self, chunk: bytes | str) -> int | None:
        """Log, decode and store one received chunk. Returns a disconnect code, if any."""
        # Both clocks first, before any work: this timestamp is the only clock the
        # depth feed has, so anything done before reading it becomes latency
        # baked into the data.
        recv_wall_ns = self.wall_ns()
        recv_mono_ns = self.mono_ns()

        if isinstance(chunk, str):
            # Neither feed sends text frames. Recording it verbatim is the only
            # way to find out what it was.
            chunk = chunk.encode()

        # -- step 2: raw log, unconditionally, before anything can go wrong ---
        if self.rawlog.append(chunk, recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns) >= 0:
            self.stats.raw_records += 1

        self.stats.chunks += 1
        self.stats.bytes_in += len(chunk)
        if self.stats.first_data_wall_ns is None:
            self.stats.first_data_wall_ns = recv_wall_ns
        self.stats.last_data_wall_ns = recv_wall_ns
        self.policy.on_data()

        # -- step 3: decode, best effort -------------------------------------
        try:
            frames = self.decoder.feed(chunk)
        except ProtocolError as exc:
            # The buffer cannot be resynchronised, so the stream position is
            # lost. The bytes are already safe on disk; drop the connection to
            # get a clean buffer rather than decoding garbage for hours.
            self.stats.decode_errors += 1
            self.log(f"[{self.name}] DECODE ERROR, dropping connection: {exc}")
            return None
        self.stats.frames += len(frames)

        if self.on_frames is not None and frames:
            self.on_frames(frames, recv_wall_ns, recv_mono_ns)

        # -- step 4: Parquet, guarded ----------------------------------------
        if self.store_append is not None and frames:
            try:
                self.stats.rows += self.store_append(frames, recv_wall_ns, recv_mono_ns)
            except Exception as exc:  # noqa: BLE001 - never let the store end a session
                self.stats.store_errors += 1
                self.log(f"[{self.name}] STORE ERROR (raw log unaffected): {type(exc).__name__}: {exc}")

        for frame in frames:
            if isinstance(frame, Disconnect):
                self.stats.disconnect_frames += 1
                self.stats.last_reason_code = frame.reason_code
                self.log(f"[{self.name}] disconnect frame: {frame.reason_code} {frame.reason}")
                return frame.reason_code
        return None

    async def _close(self, ws: WebSocketLike) -> None:
        """Close politely, then hard. Never raise: this runs in a finally block.

        The polite disconnect message matters more than it looks: it is what tells
        the server to release the connection slot, and there are only five.
        """
        if self.send_disconnect_on_close:
            try:
                await ws.send(json.dumps(DISCONNECT_MESSAGE))
            except Exception:  # noqa: BLE001
                pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "url": redact_url(self.url),
            "subscription_messages": len(self.subscriptions),
            "stale_after_s": self.stale_after_s,
            "stats": self.stats.manifest(),
            "decode": self.decoder.stats.summary(),
            "decode_clean": self.decoder.stats.clean,
            "pending_bytes": self.decoder.pending_bytes,
            "reconnect": self.policy.manifest(),
            "rawlog": self.rawlog.manifest(),
        }


@dataclass(slots=True)
class ChainPollStats:
    polls: int = 0
    bytes_in: int = 0
    errors: int = 0
    raw_records: int = 0
    last_error: str | None = None
    by_expiry: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        line = f"{self.polls} polls / {self.bytes_in} bytes"
        if self.errors:
            line += f"; {self.errors} errors (last: {self.last_error})"
        return line

    def manifest(self) -> dict:
        return {
            "polls": self.polls,
            "bytes_in": self.bytes_in,
            "errors": self.errors,
            "raw_records": self.raw_records,
            "last_error": self.last_error,
            "by_expiry": dict(self.by_expiry),
        }


@dataclass
class ChainPollChannel:
    """Poll the REST option chain and write each raw JSON response to the raw log.

    Deliberately *not* routed through ``store.py``. The ``/optionchain`` response
    carries no security ids, and partitioning ~760 legs per poll by instrument
    would emit tens of thousands of tiny Parquet files a day. Writing the
    response bytes into a raw log channel instead gets durability from exactly
    the same mechanism as the WebSocket feeds, keeps the vendor's reply
    byte-for-byte auditable, and defers the Parquet conversion to an offline
    script that can be rewritten as often as necessary.

    ``fetch(expiry) -> bytes`` is injected, so the poller is tested without HTTP.
    ``min_interval_s`` is enforced here rather than trusted to a caller: the
    documented limit is one unique request per 3 s, and exceeding it gets the
    whole REST surface throttled, not just this endpoint.
    """

    name: str
    expiries: Sequence[str]
    fetch: Callable[[str], Awaitable[bytes]]
    rawlog: RawLogWriter
    min_interval_s: float = 3.5
    backoff: Backoff = field(default_factory=lambda: Backoff(base=2.0, maximum=60.0))
    log: Logger = _noop
    wall_ns: Callable[[], int] = time.time_ns
    mono_ns: Callable[[], int] = time.monotonic_ns
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    stats: ChainPollStats = field(default_factory=ChainPollStats)

    def __post_init__(self) -> None:
        if self.min_interval_s < proto.OPTION_CHAIN_MIN_INTERVAL_S:
            raise ValueError(
                f"min_interval_s={self.min_interval_s} is below the documented "
                f"{proto.OPTION_CHAIN_MIN_INTERVAL_S}s option-chain limit"
            )
        if not self.expiries:
            raise ValueError("chain poller needs at least one expiry")

    async def run(
        self,
        *,
        stop: asyncio.Event | None = None,
        duration_s: float | None = None,
        max_polls: int | None = None,
    ) -> ChainPollStats:
        deadline = None if duration_s is None else self.mono_ns() + int(duration_s * 1e9)
        i = 0
        while True:
            if stop is not None and stop.is_set():
                return self.stats
            if deadline is not None and self.mono_ns() >= deadline:
                return self.stats
            if max_polls is not None and self.stats.polls >= max_polls:
                return self.stats

            expiry = self.expiries[i % len(self.expiries)]
            i += 1
            started = self.mono_ns()
            ok = await self._poll(expiry)

            if ok:
                self.backoff.reset()
                wait = self.min_interval_s - (self.mono_ns() - started) / 1e9
            else:
                wait = max(self.min_interval_s, self.backoff.next_delay())
            if wait > 0:
                await self.sleep(wait)

    async def _poll(self, expiry: str) -> bool:
        try:
            payload = await self.fetch(expiry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a poller must outlive one bad reply
            self.stats.errors += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"[{self.name}] poll failed for {expiry}: {self.stats.last_error}")
            return False

        recv_wall_ns = self.wall_ns()
        recv_mono_ns = self.mono_ns()
        # The expiry is prefixed as a JSON envelope because the vendor reply does
        # not say which expiry it answers, and a month of unlabelled chains is a
        # month of guessing.
        record = (
            b'{"expiry":'
            + json.dumps(expiry).encode()
            + b',"recv_wall_ns":'
            + str(recv_wall_ns).encode()
            + b',"body":'
            + payload
            + b"}"
        )
        if self.rawlog.append(record, recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns) >= 0:
            self.stats.raw_records += 1
        self.stats.polls += 1
        self.stats.bytes_in += len(payload)
        self.stats.by_expiry[expiry] = self.stats.by_expiry.get(expiry, 0) + 1
        return True

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "expiries": list(self.expiries),
            "min_interval_s": self.min_interval_s,
            "stats": self.stats.manifest(),
            "rawlog": self.rawlog.manifest(),
        }


# -- construction helpers -----------------------------------------------------


def depth_channel(
    *,
    instruments: Sequence[proto.InstrumentKey],
    token: str,
    client_id: str,
    rawlog: RawLogWriter,
    connect: Connector,
    **kwargs: Any,
) -> FeedChannel:
    """Wire a 20-level depth channel from a resolved instrument list."""
    if len(instruments) > proto.DEPTH_MAX_INSTRUMENTS:
        raise ValueError(
            f"{len(instruments)} instruments exceeds the {proto.DEPTH_MAX_INSTRUMENTS} "
            f"per-connection depth cap"
        )
    return FeedChannel(
        name=kwargs.pop("name", "depth"),
        url=depth_url(token=token, client_id=client_id),
        subscriptions=depth_subscriptions(instruments),
        connect=connect,
        rawlog=rawlog,
        decoder=depth_decoder(),
        **kwargs,
    )


def feed_channel(
    *,
    instruments: Sequence[proto.InstrumentKey],
    token: str,
    client_id: str,
    rawlog: RawLogWriter,
    connect: Connector,
    request_code: int = proto.CAPTURE_REQUEST_CODE,
    **kwargs: Any,
) -> FeedChannel:
    """Wire a general-feed channel from a resolved instrument list."""
    if len(instruments) > proto.FEED_MAX_INSTRUMENTS:
        raise ValueError(
            f"{len(instruments)} instruments exceeds the {proto.FEED_MAX_INSTRUMENTS} "
            f"per-connection general-feed cap"
        )
    return FeedChannel(
        name=kwargs.pop("name", "feed"),
        url=market_feed_url(token=token, client_id=client_id),
        subscriptions=feed_subscriptions(instruments, request_code=request_code),
        connect=connect,
        rawlog=rawlog,
        decoder=market_feed_decoder(),
        **kwargs,
    )


def store_appender(writer: Any) -> Callable[[Sequence[Frame], int, int], int]:
    """Adapt ``store.CaptureWriter.extend`` to ``FeedChannel.store_append``."""

    def append(frames: Sequence[Frame], recv_wall_ns: int, recv_mono_ns: int) -> int:
        return writer.extend(frames, recv_wall_ns=recv_wall_ns, recv_mono_ns=recv_mono_ns)

    return append


def fanout_appender(
    appenders: Iterable[Callable[[Sequence[Frame], int, int], int]],
) -> Callable[[Sequence[Frame], int, int], int]:
    """Send every frame to several writers; each ignores what is not its table.

    The general feed emits snapshots and, on the depth-5 code, book frames, and
    ``CaptureWriter`` records anything that is not its own payload as a skip. So
    fanning out is how one socket populates two tables without the channel
    knowing which table a frame belongs to.
    """
    targets = list(appenders)

    def append(frames: Sequence[Frame], recv_wall_ns: int, recv_mono_ns: int) -> int:
        return sum(target(frames, recv_wall_ns, recv_mono_ns) for target in targets)

    return append
