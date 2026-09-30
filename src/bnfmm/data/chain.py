"""REST option chain: fetch, and a parser for the offline conversion.

Two halves that are deliberately not coupled.

The **fetch** half is the second independent safety net for a capture session.
It is plain JSON over HTTPS, so it cannot fail for any reason the binary
decoders can -- a completely wrong reading of the wire format still leaves
whole-chain top-of-book, IV, greeks and OI at 3-second resolution. It also
reaches coverage the 50-slot depth budget never can: every strike of an expiry
in one reply.

The **parse** half exists now only so the shape of the reply is pinned by a
test and the offline converter has a home. Capture writes the response bytes
verbatim into a raw-log channel and does no parsing at all, because a parser bug
at 09:15 must not be able to cost a row of data.

One vendor detail worth naming, because it is silent when wrong:
``UnderlyingScrip`` must be the **index** security id (BANKNIFTY = 25, read from
segment ``I`` of the master) when ``UnderlyingSeg`` is ``IDX_I``. Passing a
futures security id there is accepted and returns nothing useful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

API_BASE = "https://api.dhan.co/v2"
OPTION_CHAIN_PATH = "/optionchain"
EXPIRY_LIST_PATH = "/optionchain/expirylist"

#: Segment string the option-chain endpoint expects for an index underlying.
IDX_SEGMENT = "IDX_I"

DEFAULT_TIMEOUT_S = 15.0


class ChainError(RuntimeError):
    """The option-chain endpoint returned something unusable."""


def chain_request(underlying_scrip: int, underlying_segment: str, expiry: date | str) -> dict:
    """Build the ``POST /optionchain`` body."""
    return {
        "UnderlyingScrip": int(underlying_scrip),
        "UnderlyingSeg": str(underlying_segment),
        "Expiry": expiry.isoformat() if isinstance(expiry, date) else str(expiry),
    }


def expiry_list_request(underlying_scrip: int, underlying_segment: str) -> dict:
    """Build the ``POST /optionchain/expirylist`` body."""
    return {
        "UnderlyingScrip": int(underlying_scrip),
        "UnderlyingSeg": str(underlying_segment),
    }


def fetch_chain_bytes(
    session_headers: Mapping[str, str],
    *,
    underlying_scrip: int,
    underlying_segment: str = IDX_SEGMENT,
    expiry: date | str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    api_base: str = API_BASE,
) -> bytes:
    """POST the option-chain request and return the response body **unparsed**.

    Returning bytes rather than a dict is the whole point: those bytes go
    straight to the raw log, so what is archived is exactly what the vendor sent,
    not this module's interpretation of it. Blocking on purpose -- callers run it
    in a thread.
    """
    import requests

    response = requests.post(
        api_base + OPTION_CHAIN_PATH,
        headers={**dict(session_headers), "Content-Type": "application/json"},
        json=chain_request(underlying_scrip, underlying_segment, expiry),
        timeout=timeout_s,
    )
    if response.status_code != 200:
        raise ChainError(f"HTTP {response.status_code}: {response.text[:300]}")
    return response.content


def fetch_expiry_list(
    session_headers: Mapping[str, str],
    *,
    underlying_scrip: int,
    underlying_segment: str = IDX_SEGMENT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    api_base: str = API_BASE,
) -> list[str]:
    """List the expiries the vendor believes are live, as ``YYYY-MM-DD`` strings.

    Worth calling once at startup: it cross-checks the expiries resolved from the
    downloaded master against the ones the API will actually answer for, and a
    disagreement means the master is stale.
    """
    import requests

    response = requests.post(
        api_base + EXPIRY_LIST_PATH,
        headers={**dict(session_headers), "Content-Type": "application/json"},
        json=expiry_list_request(underlying_scrip, underlying_segment),
        timeout=timeout_s,
    )
    if response.status_code != 200:
        raise ChainError(f"HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    data = body.get("data", body)
    if isinstance(data, Mapping):
        data = data.get("data", [])
    if not isinstance(data, Sequence):
        raise ChainError(f"unexpected expiry list shape: {type(data).__name__}")
    return [str(x) for x in data]


@dataclass(frozen=True, slots=True)
class ChainLeg:
    """One strike/side row of a chain reply, with the fields worth keeping."""

    strike: float
    option_type: str
    last_price: float | None
    top_bid_price: float | None
    top_bid_quantity: int | None
    top_ask_price: float | None
    top_ask_quantity: int | None
    implied_volatility: float | None
    oi: int | None
    volume: int | None
    previous_close_price: float | None = None
    previous_oi: int | None = None
    delta: float | None = None
    theta: float | None = None
    gamma: float | None = None
    vega: float | None = None

    @property
    def spread(self) -> float | None:
        if self.top_bid_price is None or self.top_ask_price is None:
            return None
        return self.top_ask_price - self.top_bid_price

    @property
    def mid(self) -> float | None:
        if self.top_bid_price is None or self.top_ask_price is None:
            return None
        if self.top_bid_price <= 0 or self.top_ask_price <= 0:
            return None
        return (self.top_bid_price + self.top_ask_price) / 2.0


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """One poll: the underlying's last price and every leg the reply carried."""

    expiry: str
    last_price: float | None
    legs: tuple[ChainLeg, ...]
    recv_wall_ns: int | None = None

    def by_strike(self) -> dict[float, dict[str, ChainLeg]]:
        out: dict[float, dict[str, ChainLeg]] = {}
        for leg in self.legs:
            out.setdefault(leg.strike, {})[leg.option_type] = leg
        return out

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(sorted({leg.strike for leg in self.legs}))


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _int(value: Any) -> int | None:
    out = _num(value)
    return None if out is None else int(out)


def _leg(strike: float, option_type: str, raw: Mapping[str, Any]) -> ChainLeg:
    greeks = raw.get("greeks") or {}
    return ChainLeg(
        strike=strike,
        option_type=option_type,
        last_price=_num(raw.get("last_price")),
        top_bid_price=_num(raw.get("top_bid_price")),
        top_bid_quantity=_int(raw.get("top_bid_quantity")),
        top_ask_price=_num(raw.get("top_ask_price")),
        top_ask_quantity=_int(raw.get("top_ask_quantity")),
        implied_volatility=_num(raw.get("implied_volatility")),
        oi=_int(raw.get("oi")),
        volume=_int(raw.get("volume")),
        previous_close_price=_num(raw.get("previous_close_price")),
        previous_oi=_int(raw.get("previous_oi")),
        delta=_num(greeks.get("delta")),
        theta=_num(greeks.get("theta")),
        gamma=_num(greeks.get("gamma")),
        vega=_num(greeks.get("vega")),
    )


def parse_chain(payload: bytes | str | Mapping[str, Any], *, expiry: str = "") -> ChainSnapshot:
    """Parse one ``/optionchain`` reply.

    Legs whose strike is unparseable are skipped rather than raising: this runs
    offline over a month of archived replies, and one malformed key must not stop
    the conversion. Empty ``ce``/``pe`` sub-objects -- which the vendor sends for
    strikes with no quotes -- are kept, because "quoted nothing" and "was not in
    the reply" are different facts.
    """
    if isinstance(payload, (bytes, bytearray)):
        body = json.loads(payload)
    elif isinstance(payload, str):
        body = json.loads(payload)
    else:
        body = payload

    if not isinstance(body, Mapping):
        raise ChainError(f"chain reply is not an object: {type(body).__name__}")

    # A capture record wraps the vendor reply in an envelope that names the
    # expiry, since the reply itself does not.
    if "body" in body and "expiry" in body:
        expiry = str(body.get("expiry") or expiry)
        recv = _int(body.get("recv_wall_ns"))
        inner = body["body"]
        snapshot = parse_chain(inner, expiry=expiry)
        return ChainSnapshot(
            expiry=snapshot.expiry,
            last_price=snapshot.last_price,
            legs=snapshot.legs,
            recv_wall_ns=recv,
        )

    data = body.get("data", body)
    if not isinstance(data, Mapping):
        raise ChainError(f"chain reply has no data object: {type(data).__name__}")

    strikes = data.get("oc") or {}
    if not isinstance(strikes, Mapping):
        raise ChainError(f"chain reply 'oc' is not an object: {type(strikes).__name__}")

    legs: list[ChainLeg] = []
    for raw_strike, sides in strikes.items():
        strike = _num(raw_strike)
        if strike is None or not isinstance(sides, Mapping):
            continue
        for key, option_type in (("ce", "CE"), ("pe", "PE")):
            side = sides.get(key)
            if isinstance(side, Mapping):
                legs.append(_leg(strike, option_type, side))

    legs.sort(key=lambda leg: (leg.strike, leg.option_type))
    return ChainSnapshot(
        expiry=expiry,
        last_price=_num(data.get("last_price")),
        legs=tuple(legs),
    )
