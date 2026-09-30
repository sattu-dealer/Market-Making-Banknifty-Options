"""Reconnect policy: when to retry a dropped feed, and how long to wait.

Split out of ``capture.py`` and kept free of ``asyncio`` and of any clock read
for one reason: **this is the component whose bugs are only visible on a day
that cannot be re-run.** A retry loop that hammers a socket the server has
already refused, or that gives up on a transient TCP reset, costs a session.
Both are table-testable if the decision is a pure function, and neither is
testable if it is a ``while True`` with a ``sleep`` inside it.

So the split is:

- :func:`should_reconnect` -- a pure verdict over the observed close.
- :class:`Backoff` -- a deterministic delay sequence, jittered from a seeded
  generator so a test can assert exact numbers.
- :class:`ReconnectPolicy` -- the two composed, plus the attempt bookkeeping.

Nothing here sleeps. ``channels.py`` awaits the returned delay, which is what
lets the whole policy be exercised in microseconds.

**The counter-intuitive rule, stated once.** Every code Dhan documents is
treated as *fatal*, and every code it does not document is treated as
*retryable*. That is backwards from the usual "retry unless told otherwise",
and it is deliberate:

- 806/807/808/809 are authorisation and entitlement failures. Reconnecting with
  the same credentials cannot succeed; it only burns the connection budget.
- **805 is the dangerous one.** "No. of active websocket connections exceeded"
  is raised on a *new* connection, and the vendor SDK's own comment says the
  server drops the **oldest** socket. A laptop that suspends leaves half-open
  sockets the server may still count, so a naive retry loop after resume can
  trip 805 repeatedly and kill the sockets that are actually working. The
  correct response is to stop, tell the operator, and leave the surviving
  channels alone.
- A bare close with no disconnect frame is the ordinary internet: a TCP reset,
  a Wi-Fi hiccup, a proxy timeout. Those are exactly what backoff is for.
- An *undocumented* numeric code is unknown, and unknown is not the same as
  hopeless. Retrying a handful of times and reporting the code costs little; a
  hard stop on a code the vendor added last week costs the session.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterator

from .protocol import (
    DISCONNECT_REASONS,
    ENTITLEMENT_DISCONNECT,
    FATAL_DISCONNECTS,
    SERVER_SILENCE_TIMEOUT_S,
)

__all__ = [
    "SERVER_SILENCE_TIMEOUT_S",
    "Attempt",
    "Backoff",
    "ReconnectPolicy",
    "Verdict",
    "is_entitlement_failure",
    "should_reconnect",
]

DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 60.0
DEFAULT_MULTIPLIER = 2.0
DEFAULT_JITTER = 0.25

#: Cap on retries for a close we could not attribute to a documented reason.
#: Bounded because an endlessly retrying channel with no data is indistinguishable
#: from a working one in a log tail, which is how a dead session goes unnoticed.
DEFAULT_MAX_ATTEMPTS = 20


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether to retry, and the sentence that goes in the operator log."""

    retry: bool
    reason: str
    fatal_code: int | None = None
    needs_operator: bool = False

    def __bool__(self) -> bool:
        return self.retry


def should_reconnect(reason_code: int | None) -> Verdict:
    """Decide whether a dropped connection is worth retrying.

    ``reason_code`` is the code from a decoded ``Disconnect`` frame, or ``None``
    when the socket simply closed without one -- which is the common case, since
    a TCP reset carries no application payload.
    """
    if reason_code is None:
        return Verdict(True, "socket closed without a disconnect frame")

    if reason_code in FATAL_DISCONNECTS:
        text = DISCONNECT_REASONS[reason_code]
        # 805 and 806 both need a human: one to check for stale sockets, the
        # other to check the subscription. The rest need new credentials, which
        # is also a human.
        return Verdict(
            False,
            f"disconnect {reason_code}: {text}",
            fatal_code=reason_code,
            needs_operator=True,
        )

    return Verdict(True, f"disconnect {reason_code}: undocumented code, treating as transient")


def is_entitlement_failure(reason_code: int | None) -> bool:
    """True for the one code that means "you have not paid", not "you are broken"."""
    return reason_code == ENTITLEMENT_DISCONNECT


@dataclass(slots=True)
class Backoff:
    """Exponential backoff with bounded multiplicative jitter.

    Deterministic by construction: the jitter comes from a ``random.Random``
    instance seeded at build time, never from the module-level generator, so a
    test asserts exact delays and two channels in one process do not share
    (and thus perturb) each other's sequence.

    Jitter is multiplicative in ``[1 - jitter, 1 + jitter]`` rather than additive
    so it scales with the delay, and it exists to stop three channels that dropped
    on the same Wi-Fi blip from retrying in lockstep -- which would look to the
    server like a burst rather than three reconnects.
    """

    base: float = DEFAULT_BASE_DELAY_S
    maximum: float = DEFAULT_MAX_DELAY_S
    multiplier: float = DEFAULT_MULTIPLIER
    jitter: float = DEFAULT_JITTER
    seed: int = 0
    attempts: int = 0
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.base <= 0:
            raise ValueError("base must be positive")
        if self.maximum < self.base:
            raise ValueError("maximum must be at least base")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be at least 1.0")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")
        self._rng = random.Random(self.seed)

    def unjittered(self, attempt: int) -> float:
        """The delay before ``attempt`` (1-based) ignoring jitter, for tests and docs."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        return min(self.base * self.multiplier ** (attempt - 1), self.maximum)

    def next_delay(self) -> float:
        """Advance one attempt and return the delay to wait, in seconds."""
        self.attempts += 1
        delay = self.unjittered(self.attempts)
        if self.jitter:
            delay *= 1.0 + self._rng.uniform(-self.jitter, self.jitter)
        return delay

    def reset(self) -> None:
        """Called after a connection has proved itself, not merely opened.

        "Proved itself" means data arrived. Resetting on *connect* turns a
        server that accepts and immediately closes into a tight loop at the base
        delay, which is the failure mode backoff is supposed to prevent.
        """
        self.attempts = 0

    def sequence(self, n: int) -> list[float]:
        """First ``n`` delays without mutating this instance -- for logs and tests."""
        return [self.unjittered(i) for i in range(1, n + 1)]

    def __iter__(self) -> Iterator[float]:
        while True:
            yield self.next_delay()


@dataclass(slots=True)
class Attempt:
    """The outcome of asking the policy what to do after a drop."""

    retry: bool
    delay_s: float
    attempt: int
    reason: str
    fatal_code: int | None = None
    needs_operator: bool = False
    exhausted: bool = False

    def __bool__(self) -> bool:
        return self.retry


@dataclass(slots=True)
class ReconnectPolicy:
    """:func:`should_reconnect` plus backoff plus an attempt ceiling.

    One instance per channel. ``on_data`` is the success signal, deliberately
    distinct from "connected" -- see :meth:`Backoff.reset`.
    """

    backoff: Backoff = field(default_factory=Backoff)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    total_reconnects: int = 0
    fatal_reason: str | None = None
    fatal_code: int | None = None

    @property
    def stopped(self) -> bool:
        return self.fatal_reason is not None

    def on_data(self) -> None:
        """Record that the current connection delivered bytes."""
        self.backoff.reset()

    def on_disconnect(self, reason_code: int | None) -> Attempt:
        """Verdict, delay and attempt number for a connection that just dropped."""
        verdict = should_reconnect(reason_code)
        if not verdict.retry:
            self.fatal_reason = verdict.reason
            self.fatal_code = verdict.fatal_code
            return Attempt(
                retry=False,
                delay_s=0.0,
                attempt=self.backoff.attempts,
                reason=verdict.reason,
                fatal_code=verdict.fatal_code,
                needs_operator=verdict.needs_operator,
            )

        if self.backoff.attempts >= self.max_attempts:
            reason = (
                f"{verdict.reason}; giving up after {self.backoff.attempts} "
                f"consecutive attempts without data"
            )
            self.fatal_reason = reason
            return Attempt(
                retry=False,
                delay_s=0.0,
                attempt=self.backoff.attempts,
                reason=reason,
                needs_operator=True,
                exhausted=True,
            )

        delay = self.backoff.next_delay()
        self.total_reconnects += 1
        return Attempt(
            retry=True,
            delay_s=delay,
            attempt=self.backoff.attempts,
            reason=verdict.reason,
        )

    def manifest(self) -> dict:
        return {
            "total_reconnects": self.total_reconnects,
            "consecutive_attempts": self.backoff.attempts,
            "max_attempts": self.max_attempts,
            "base_delay_s": self.backoff.base,
            "max_delay_s": self.backoff.maximum,
            "stopped": self.stopped,
            "fatal_reason": self.fatal_reason,
            "fatal_code": self.fatal_code,
        }
