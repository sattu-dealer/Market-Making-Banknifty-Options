"""Capture universe: which instruments to subscribe on which socket, and why.

``instruments.resolve()`` cannot be reused for capture, and that is a design
finding rather than an inconvenience. It exists to pick contracts for the
*quoting study*, so it deliberately excludes the near-expiry regime --
``min_days_to_expiry: 3`` and ``roll_days_before_expiry: 3`` in
``config/instruments.yaml``, documented in its own docstring as keeping the
study out of pin risk. Harvesting wants the exact opposite: on 2026-08-24 the
August contract is 1 DTE and is *the* thing being recorded, and both filters
would discard it. ``resolve()`` also returns a single expiry's options, whereas
capture needs front *and* next at different bands on different channels.

So this module resolves by index into the live expiry list with no roll filter
at all, and it is organised around the one fact that actually constrains the
design: **the 20-level depth feed accepts 50 instruments per connection, and a
user gets 5 connections.** That is a connection limit, not a rate limit, so it
cannot be worked around by slowing down.

Three properties are enforced in code rather than trusted to a config file:

1. **Caps bind at resolve time, not at 09:15.** Every channel declares its
   protocol, the protocol declares its cap, and a universe that exceeds it is
   truncated with the truncation *reported*, never silently accepted. The plan
   this was written from contained the arithmetic slip that motivated the rule:
   "±12 strikes (25 strikes x 2 = 48)" is really 50, so +2 futures is 52 and
   breaks the cap. A machine that checks the multiplication does not make that
   mistake twice.
2. **Drop order is a consequence, not a second code path.** Groups fill in
   declared order and stop when the cap binds; within a group, strikes are
   ordered by distance from the money, so a partial group truncates from the
   furthest strike inward. That reproduces "furthest strikes first, then
   next-expiry, then the wide band" without a dedicated dropper, and groups
   marked ``required`` raise instead of truncating -- which is how "never the
   front future, never the indices, never the ATM +/-4 core" becomes checkable.
3. **Indices can never reach the depth channel.** An index has no order book;
   subscribing one on the depth feed spends a scarce slot on nothing. Asserted,
   because it is the kind of error a config edit introduces silently.

Index instruments are built here rather than through ``instruments._to_contract``
for a concrete reason: the master carries ``SM_EXPIRY_DATE`` of ``0001-01-01``
for an index, which is outside the pandas nanosecond datetime range and raises
``OutOfBoundsDatetime``. They get an explicit sentinel expiry instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import yaml

from .instruments import (
    SEGMENT_CODE,
    Contract,
    SpotEstimate,
    _to_contract,
    estimate_spot_detail,
    read_master,
)
from .protocol import DEPTH_MAX_INSTRUMENTS, FEED_MAX_INSTRUMENTS, MAX_WEBSOCKET_CONNECTIONS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "capture.yaml"

#: Indices never expire. ``date.max`` rather than ``None`` so every code path
#: that sorts or compares expiries keeps working, and an index sorts last.
NO_EXPIRY = date.max

#: Subscribing an index needs exchange segment 0 (``IDX_I``). Resolved at import
#: so removing that entry from ``instruments.SEGMENT_CODE`` fails here rather than
#: producing a subscription the server silently ignores. Note it is *falsy*.
IDX_SEGMENT_CODE = SEGMENT_CODE["IDX_I"]

#: Per-connection instrument caps by channel protocol. ``option_chain`` is REST
#: and covers every strike in one response, so it has no instrument cap at all.
PROTOCOL_CAPS: Mapping[str, int | None] = {
    "depth_20": DEPTH_MAX_INSTRUMENTS,
    "market_feed": FEED_MAX_INSTRUMENTS,
    "option_chain": None,
}

#: Protocols that consume a WebSocket connection from the budget of five.
SOCKET_PROTOCOLS = frozenset({"depth_20", "market_feed"})

VALID_KINDS = frozenset({"options", "future", "index"})
VALID_EXPIRY_SELECTORS = frozenset({"front", "next", "third"})
_EXPIRY_INDEX = {"front": 0, "next": 1, "third": 2}


class UniverseError(ValueError):
    """The configured universe cannot be resolved against this master."""


# -- configuration ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Group:
    """One ordered slice of instruments, appended to a channel until the cap binds.

    ``required`` groups are the enforcement mechanism for the plan's protected
    set: they raise rather than truncate, so a config change that would silently
    evict the front future or the at-the-money core fails loudly at resolve time.
    """

    name: str
    kind: str
    expiry: str | None = None
    strike_band: int | None = None
    option_types: tuple[str, ...] = ("CE", "PE")
    security_ids: tuple[int, ...] = ()
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise UniverseError(f"group {self.name!r}: kind {self.kind!r} not in {sorted(VALID_KINDS)}")
        if self.kind == "index":
            if not self.security_ids:
                raise UniverseError(f"group {self.name!r}: index groups need security_ids")
        else:
            if self.expiry not in VALID_EXPIRY_SELECTORS:
                raise UniverseError(
                    f"group {self.name!r}: expiry {self.expiry!r} not in "
                    f"{sorted(VALID_EXPIRY_SELECTORS)}"
                )
        if self.kind == "options":
            if self.strike_band is None or self.strike_band < 0:
                raise UniverseError(f"group {self.name!r}: options groups need strike_band >= 0")
            if not self.option_types:
                raise UniverseError(f"group {self.name!r}: options groups need option_types")

    @property
    def expiry_index(self) -> int | None:
        return None if self.expiry is None else _EXPIRY_INDEX[self.expiry]

    def describe(self) -> str:
        if self.kind == "index":
            return f"{self.name}: indices {list(self.security_ids)}"
        if self.kind == "future":
            return f"{self.name}: {self.expiry} future"
        types = "+".join(self.option_types)
        return f"{self.name}: {self.expiry} options ATM +/-{self.strike_band} {types}"


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """A configured channel: one socket (or one REST poller) and its groups."""

    name: str
    protocol: str
    groups: tuple[Group, ...]
    cap: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOL_CAPS:
            raise UniverseError(
                f"channel {self.name!r}: protocol {self.protocol!r} not in {sorted(PROTOCOL_CAPS)}"
            )
        limit = PROTOCOL_CAPS[self.protocol]
        if limit is not None:
            if self.cap is None:
                object.__setattr__(self, "cap", limit)
            elif self.cap > limit:
                raise UniverseError(
                    f"channel {self.name!r}: cap {self.cap} exceeds the documented "
                    f"{self.protocol} limit of {limit}"
                )
        # An index has no order book, so a depth slot spent on one buys nothing
        # and there are only 50 of them.
        if self.protocol == "depth_20":
            offenders = [g.name for g in self.groups if g.kind == "index"]
            if offenders:
                raise UniverseError(
                    f"channel {self.name!r}: indices have no order book and cannot be "
                    f"subscribed on the 20-level depth feed (groups {offenders})"
                )


def _group_from_config(raw: Mapping[str, Any]) -> Group:
    try:
        name = str(raw["name"])
        kind = str(raw["kind"])
    except KeyError as exc:
        raise UniverseError(f"group is missing {exc.args[0]!r}: {dict(raw)}") from exc
    expiry = raw.get("expiry")
    types = raw.get("option_types", ["CE", "PE"])
    return Group(
        name=name,
        kind=kind,
        expiry=None if expiry is None else str(expiry),
        strike_band=None if raw.get("strike_band") is None else int(raw["strike_band"]),
        option_types=tuple(str(t).upper() for t in types),
        security_ids=tuple(int(s) for s in raw.get("security_ids", ())),
        required=bool(raw.get("required", False)),
    )


def load_capture_config(path: Path | str = _DEFAULT_CONFIG) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict):
        raise UniverseError(f"{path}: capture config must be a mapping")
    return config


def channel_specs(config: Mapping[str, Any]) -> tuple[ChannelSpec, ...]:
    """Parse and validate the ``channels`` block, including the connection budget."""
    raw_channels = config.get("channels")
    if not raw_channels:
        raise UniverseError("capture config has no channels")

    specs: list[ChannelSpec] = []
    for raw in raw_channels:
        groups = tuple(_group_from_config(g) for g in raw.get("groups", ()))
        spec = ChannelSpec(
            name=str(raw["name"]),
            protocol=str(raw["protocol"]),
            groups=groups,
            cap=None if raw.get("cap") is None else int(raw["cap"]),
            enabled=bool(raw.get("enabled", True)),
        )
        specs.append(spec)

    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise UniverseError(f"duplicate channel names: {names}")

    sockets = sum(1 for s in specs if s.enabled and s.protocol in SOCKET_PROTOCOLS)
    reserve = int(config.get("reserve_connections", 2))
    if sockets + reserve > MAX_WEBSOCKET_CONNECTIONS:
        raise UniverseError(
            f"{sockets} socket channel(s) plus {reserve} held in reserve exceeds the "
            f"{MAX_WEBSOCKET_CONNECTIONS}-connection limit; a sixth connection kills the "
            f"first one, which is the one that is working"
        )
    return tuple(specs)


# -- resolution ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupFill:
    """What one group actually contributed, and what the cap cost it."""

    group: Group
    requested: int
    included: int
    duplicates: int
    dropped: tuple[Contract, ...]

    @property
    def truncated(self) -> bool:
        return bool(self.dropped)

    def describe(self) -> str:
        line = f"{self.group.describe()} -> {self.included}"
        if self.duplicates:
            line += f" (+{self.duplicates} already present)"
        if self.dropped:
            strikes = sorted({c.strike for c in self.dropped if c.strike is not None})
            span = f"{strikes[0]:.0f}..{strikes[-1]:.0f}" if strikes else "n/a"
            line += f", DROPPED {len(self.dropped)} at strikes {span}"
        return line

    def manifest(self) -> dict:
        return {
            "name": self.group.name,
            "kind": self.group.kind,
            "expiry": self.group.expiry,
            "strike_band": self.group.strike_band,
            "option_types": list(self.group.option_types),
            "required": self.group.required,
            "requested": self.requested,
            "included": self.included,
            "duplicates": self.duplicates,
            "dropped": [c.security_id for c in self.dropped],
        }


def _contract_manifest(c: Contract) -> dict:
    """The full instrument identity behind one ``security_id``, JSON-safe.

    This is the load-bearing record. On disk a captured row is keyed only by
    ``security_id`` (both a Parquet column and the partition key), and the sole
    mapping from that integer to a strike/expiry/right lives in the instrument
    master CSV -- which ``instruments.ensure_master`` overwrites in place and
    which the vendor prunes of expired contracts. So the morning after a BANKNIFTY
    expiry, every option this session recorded becomes unresolvable from the
    master alone: a 20-level book of an unknown strike. Persisting the mapping
    into the session manifest makes each capture self-describing forever, with no
    dependency on a mutable external file. Dates are ISO strings so the manifest
    stays plain JSON; ``NO_EXPIRY`` (an index) serialises as null rather than
    ``9999-12-31`` so a reader need not know the sentinel.
    """
    return {
        "security_id": c.security_id,
        "display_name": c.display_name,
        "instrument": c.instrument,
        "expiry": None if c.expiry == NO_EXPIRY else c.expiry.isoformat(),
        "strike": c.strike,
        "option_type": c.option_type,
        "lot_size": c.lot_size,
        "tick_size": c.tick_size,
        "freeze_qty": c.freeze_qty,
        "exchange_segment": c.exchange_segment,
        "segment_code": c.segment_code,
    }


@dataclass(frozen=True, slots=True)
class ChannelUniverse:
    """One channel's resolved subscription list, in subscription order."""

    name: str
    protocol: str
    cap: int | None
    contracts: tuple[Contract, ...]
    fills: tuple[GroupFill, ...]
    enabled: bool = True

    @property
    def slots_used(self) -> int:
        return len(self.contracts)

    @property
    def slots_spare(self) -> int | None:
        return None if self.cap is None else self.cap - len(self.contracts)

    @property
    def truncated(self) -> bool:
        return any(f.truncated for f in self.fills)

    @property
    def feed_keys(self) -> tuple[tuple[int, str], ...]:
        """``(segment_code, security_id)`` pairs, as the subscription builders want."""
        return tuple(c.feed_key for c in self.contracts)

    @property
    def instruments(self) -> list[tuple[int, str]]:
        """Mutable list form, for ``protocol.depth_subscriptions`` / ``feed_subscriptions``."""
        return [c.feed_key for c in self.contracts]

    def by_security_id(self) -> dict[int, Contract]:
        return {c.security_id: c for c in self.contracts}

    def describe(self) -> str:
        head = f"{self.name} [{self.protocol}] {self.slots_used}"
        if self.cap is not None:
            head += f"/{self.cap} ({self.slots_spare} spare)"
        if not self.enabled:
            head += " DISABLED"
        lines = [head]
        lines.extend(f"    {f.describe()}" for f in self.fills)
        return "\n".join(lines)

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "cap": self.cap,
            "enabled": self.enabled,
            "slots_used": self.slots_used,
            "slots_spare": self.slots_spare,
            "truncated": self.truncated,
            "groups": [f.manifest() for f in self.fills],
            "security_ids": [c.security_id for c in self.contracts],
            "contracts": [_contract_manifest(c) for c in self.contracts],
            "subscriptions": [list(k) for k in self.feed_keys],
        }


@dataclass(frozen=True, slots=True)
class CaptureUniverse:
    """Everything a capture session subscribes to, resolved for one date.

    Written into the session manifest verbatim, because a month of Parquet whose
    subscription list has to be *inferred* is a month of Parquet that cannot be
    audited: a strike that never traded and a strike that was never subscribed
    look identical downstream.
    """

    asof: date
    spot: float
    atm_strike: float
    expiries: tuple[date, ...]
    channels: tuple[ChannelUniverse, ...]
    spot_detail: SpotEstimate | None = None
    strike_step: float | None = None
    lot_size: int | None = None
    freeze_qty: int | None = None

    def channel(self, name: str) -> ChannelUniverse:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(f"no channel named {name!r}; have {[c.name for c in self.channels]}")

    @property
    def enabled_channels(self) -> tuple[ChannelUniverse, ...]:
        return tuple(c for c in self.channels if c.enabled)

    @property
    def front_expiry(self) -> date:
        return self.expiries[0]

    @property
    def next_expiry(self) -> date | None:
        return self.expiries[1] if len(self.expiries) > 1 else None

    @property
    def truncated(self) -> bool:
        return any(c.truncated for c in self.channels)

    def all_contracts(self) -> dict[int, Contract]:
        """Every distinct instrument across every channel, keyed by security id."""
        merged: dict[int, Contract] = {}
        for ch in self.channels:
            for c in ch.contracts:
                merged.setdefault(c.security_id, c)
        return merged

    def describe(self) -> str:
        dte = (self.front_expiry - self.asof).days
        lines = [
            f"capture universe for {self.asof}",
            f"  spot estimate      : {self.spot:,.2f}"
            + (f"  [{self.spot_detail.describe()}]" if self.spot_detail else ""),
            f"  ATM strike         : {self.atm_strike:,.0f}"
            + (f" (step {self.strike_step:,.0f})" if self.strike_step else ""),
            f"  front expiry       : {self.front_expiry} ({dte} DTE)",
            f"  next expiry        : {self.next_expiry}",
            f"  distinct instrument: {len(self.all_contracts())}",
        ]
        lines.extend(ch.describe() for ch in self.channels)
        if self.truncated:
            lines.append("  ** universe was TRUNCATED by a channel cap; see groups above **")
        return "\n".join(lines)

    def manifest(self) -> dict:
        return {
            "asof": self.asof.isoformat(),
            "spot": self.spot,
            "spot_detail": (
                None
                if self.spot_detail is None
                else {
                    "spot": self.spot_detail.spot,
                    "call_estimate": self.spot_detail.call_estimate,
                    "put_estimate": self.spot_detail.put_estimate,
                    "disagreement": self.spot_detail.disagreement,
                    "expiry": (
                        None if self.spot_detail.expiry is None else self.spot_detail.expiry.isoformat()
                    ),
                    "n_contracts": self.spot_detail.n_contracts,
                }
            ),
            "atm_strike": self.atm_strike,
            "strike_step": self.strike_step,
            "lot_size": self.lot_size,
            "freeze_qty": self.freeze_qty,
            "expiries": [e.isoformat() for e in self.expiries],
            "truncated": self.truncated,
            "channels": [ch.manifest() for ch in self.channels],
        }


def _index_contract(row: pd.Series) -> Contract:
    """Build an index ``Contract`` without touching ``SM_EXPIRY_DATE``.

    The master stores ``0001-01-01`` for indices, which ``pd.to_datetime`` cannot
    represent, so ``instruments._to_contract`` raises on these rows. Tick and lot
    are meaningless for an index and are recorded as such rather than invented.
    """
    return Contract(
        security_id=int(row["SECURITY_ID"]),
        display_name=str(row["DISPLAY_NAME"]).strip(),
        instrument="INDEX",
        expiry=NO_EXPIRY,
        lot_size=0,
        tick_size=0.0,
        freeze_qty=0,
        strike=None,
        option_type=None,
        exchange_segment="IDX_I",
    )


def index_contracts(master: pd.DataFrame, security_ids: Sequence[int]) -> list[Contract]:
    """Resolve index security ids from the master, preserving the requested order."""
    wanted = list(dict.fromkeys(int(s) for s in security_ids))
    rows = master[(master.SEGMENT.astype(str) == "I") & master.SECURITY_ID.isin(wanted)]
    found = {int(r["SECURITY_ID"]): _index_contract(r) for _, r in rows.iterrows()}
    missing = [s for s in wanted if s not in found]
    if missing:
        raise UniverseError(
            f"index security id(s) {missing} not found in segment I of the master; "
            f"the BANKNIFTY spot index is 25 and NIFTY 50 is 13"
        )
    return [found[s] for s in wanted]


@dataclass(slots=True)
class _Chain:
    """The front/next option and futures rows, resolved once and shared."""

    expiries: tuple[date, ...]
    options_by_expiry: dict[date, pd.DataFrame]
    futures_by_expiry: dict[date, Contract]
    strikes_by_expiry: dict[date, list[float]]
    spot: float
    spot_detail: SpotEstimate | None
    atm_strike: float
    strike_step: float | None = None
    lot_size: int | None = None
    freeze_qty: int | None = None

    def expiry_at(self, selector: str) -> date:
        idx = _EXPIRY_INDEX[selector]
        if idx >= len(self.expiries):
            raise UniverseError(
                f"expiry selector {selector!r} needs at least {idx + 1} live expiries, "
                f"master has {len(self.expiries)}: {[e.isoformat() for e in self.expiries]}"
            )
        return self.expiries[idx]


def _resolve_chain(
    master: pd.DataFrame,
    config: Mapping[str, Any],
    asof: date,
    spot_override: float | None,
) -> _Chain:
    underlying = str(config.get("underlying", "BANKNIFTY")).upper()
    exchange = str(config.get("exchange", "NSE"))
    segment = str(config.get("segment", "D"))

    scope = master[
        (master.EXCH_ID.astype(str) == exchange)
        & (master.SEGMENT.astype(str) == segment)
        & (master.UNDERLYING_SYMBOL.astype(str).str.upper() == underlying)
    ].copy()
    if scope.empty:
        raise UniverseError(f"no {underlying} rows in {exchange}/{segment} of the master")

    scope["_expiry"] = pd.to_datetime(scope["SM_EXPIRY_DATE"], errors="coerce")
    scope = scope.dropna(subset=["_expiry"])
    # Expired rows persist in the master. Keeping ">= asof" rather than
    # "> asof" is what makes expiry day itself capturable, and it is also what
    # rolls the front month automatically on the following morning -- the
    # calendar does the roll, not a config edit.
    scope = scope[scope["_expiry"].dt.date >= asof]
    if scope.empty:
        raise UniverseError(f"every {underlying} contract in the master expired before {asof}")

    options_df = scope[scope.INSTRUMENT.astype(str) == "OPTIDX"]
    futures_df = scope[scope.INSTRUMENT.astype(str) == "FUTIDX"]
    if options_df.empty:
        raise UniverseError(f"no live {underlying} options in the master (stale download?)")

    expiries = tuple(sorted(options_df["_expiry"].dt.date.unique()))

    options_by_expiry = {e: options_df[options_df["_expiry"].dt.date == e] for e in expiries}
    futures_by_expiry: dict[date, Contract] = {}
    for _, row in futures_df.sort_values("_expiry").iterrows():
        futures_by_expiry[row["_expiry"].date()] = _to_contract(row)

    detail = None if spot_override is not None else estimate_spot_detail(options_df)
    if spot_override is not None:
        spot = float(spot_override)
    elif detail is not None:
        spot = detail.spot
    else:
        raise UniverseError(
            "could not estimate spot from option circuit bands and no --spot was given"
        )

    strikes_by_expiry = {
        e: sorted(df.STRIKE_PRICE.dropna().astype(float).unique())
        for e, df in options_by_expiry.items()
    }
    front_strikes = strikes_by_expiry[expiries[0]]
    if not front_strikes:
        raise UniverseError(f"front expiry {expiries[0]} has no strikes in the master")
    atm = min(front_strikes, key=lambda k: abs(k - spot))

    step = None
    if len(front_strikes) > 1:
        diffs = [b - a for a, b in zip(front_strikes, front_strikes[1:]) if b > a]
        step = float(pd.Series(diffs).mode().iloc[0]) if diffs else None

    front_opts = options_by_expiry[expiries[0]]
    lot = int(float(front_opts.LOT_SIZE.dropna().iloc[0])) if not front_opts.empty else None
    freeze = (
        int(float(front_opts.SM_FREEZE_QTY.dropna().iloc[0]))
        if "SM_FREEZE_QTY" in front_opts.columns and front_opts.SM_FREEZE_QTY.notna().any()
        else None
    )

    return _Chain(
        expiries=expiries,
        options_by_expiry=options_by_expiry,
        futures_by_expiry=futures_by_expiry,
        strikes_by_expiry=strikes_by_expiry,
        spot=spot,
        spot_detail=detail,
        atm_strike=float(atm),
        strike_step=step,
        lot_size=lot,
        freeze_qty=freeze,
    )


def _band_strikes(strikes: Sequence[float], atm: float, band: int) -> list[float]:
    """The ``2*band + 1`` strikes centred on ``atm``, ordered by distance from it.

    Banding by *index* into the ladder rather than by price keeps the band
    meaningful where strike spacing changes (BANKNIFTY interleaves 100-point
    strikes near the money with 500-point strikes far out), and ordering by
    distance is what makes truncation drop the furthest strike first.
    """
    if not strikes:
        return []
    nearest = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - atm), i))
    lo = max(0, nearest - band)
    hi = min(len(strikes), nearest + band + 1)
    window = list(strikes[lo:hi])
    window.sort(key=lambda k: (abs(k - atm), k))
    return window


def _group_contracts(group: Group, chain: _Chain, master: pd.DataFrame) -> list[Contract]:
    if group.kind == "index":
        return index_contracts(master, group.security_ids)

    expiry = chain.expiry_at(group.expiry or "front")

    if group.kind == "future":
        contract = chain.futures_by_expiry.get(expiry)
        if contract is None:
            available = sorted(chain.futures_by_expiry)
            raise UniverseError(
                f"group {group.name!r}: no {group.expiry} future for expiry {expiry}; "
                f"master has futures expiring {[d.isoformat() for d in available]}"
            )
        return [contract]

    chain_df = chain.options_by_expiry[expiry]
    strikes = _band_strikes(chain.strikes_by_expiry[expiry], chain.atm_strike, group.strike_band or 0)
    if not strikes:
        raise UniverseError(f"group {group.name!r}: no strikes for expiry {expiry}")

    wanted_types = set(group.option_types)
    types_col = chain_df.OPTION_TYPE.astype(str).str.upper()
    strike_col = chain_df.STRIKE_PRICE.astype(float)
    selected = chain_df[strike_col.isin(set(strikes)) & types_col.isin(wanted_types)]

    contracts = [_to_contract(r) for _, r in selected.iterrows()]
    rank = {k: i for i, k in enumerate(strikes)}
    type_rank = {t: i for i, t in enumerate(group.option_types)}
    # Distance from the money first, so a truncated group loses its furthest
    # strikes; then the declared option-type order, so CE and PE of one strike
    # stay adjacent and a truncation never leaves an unpaired leg mid-band.
    contracts.sort(
        key=lambda c: (
            rank.get(c.strike or 0.0, len(rank)),
            type_rank.get(c.option_type or "", len(type_rank)),
            c.security_id,
        )
    )
    return contracts


def _fill_channel(spec: ChannelSpec, chain: _Chain, master: pd.DataFrame) -> ChannelUniverse:
    chosen: list[Contract] = []
    seen: set[int] = set()
    fills: list[GroupFill] = []

    for group in spec.groups:
        candidates = _group_contracts(group, chain, master)
        fresh = [c for c in candidates if c.security_id not in seen]
        duplicates = len(candidates) - len(fresh)

        room = None if spec.cap is None else max(0, spec.cap - len(chosen))
        take = fresh if room is None else fresh[:room]
        dropped = tuple(fresh[len(take) :])

        if dropped and group.kind == "options" and len(group.option_types) > 1:
            # Never keep half a strike. The cap falls wherever it falls, so the
            # cut can land between the CE and the PE of one strike -- and a lone
            # CE is worse than no CE, because put-call parity (the fair-value
            # estimator's whole basis) needs the pair. Give the slot back rather
            # than spend it on an unusable leg.
            #
            # Done by strike rather than by rounding the room down to a multiple
            # of two: a preceding group may already hold one leg of a strike, in
            # which case the lone remaining leg in ``fresh`` is a *repair* and
            # must not be discarded. A strike is orphaned precisely when some of
            # its legs are taken and some dropped.
            split = {c.strike for c in dropped}
            keep_ids = {c.security_id for c in take if c.strike not in split}
            if len(keep_ids) != len(take):
                take = [c for c in take if c.security_id in keep_ids]
                dropped = tuple(c for c in fresh if c.security_id not in keep_ids)

        if dropped and group.required:
            raise UniverseError(
                f"channel {spec.name!r}: required group {group.name!r} does not fit -- "
                f"{len(dropped)} of {len(fresh)} instruments would be dropped by the "
                f"cap of {spec.cap}. Narrow an earlier group rather than this one."
            )

        for c in take:
            seen.add(c.security_id)
            chosen.append(c)
        fills.append(
            GroupFill(
                group=group,
                requested=len(candidates),
                included=len(take),
                duplicates=duplicates,
                dropped=dropped,
            )
        )

    if spec.cap is not None and len(chosen) > spec.cap:  # pragma: no cover - belt and braces
        raise UniverseError(
            f"channel {spec.name!r}: internal error, {len(chosen)} exceeds cap {spec.cap}"
        )

    return ChannelUniverse(
        name=spec.name,
        protocol=spec.protocol,
        cap=spec.cap,
        contracts=tuple(chosen),
        fills=tuple(fills),
        enabled=spec.enabled,
    )


def resolve_capture_universe(
    master: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    asof: date | None = None,
    spot: float | None = None,
) -> CaptureUniverse:
    """Resolve every configured channel against the instrument master.

    ``spot`` overrides the circuit-band bootstrap, which matters on the morning
    of a capture: by 08:50 the previous close is known and a hand-supplied spot
    is strictly better than an estimate inferred from price bands.
    """
    asof = asof or date.today()
    chain = _resolve_chain(master, config, asof, spot)
    specs = channel_specs(config)
    channels = tuple(_fill_channel(spec, chain, master) for spec in specs)
    return CaptureUniverse(
        asof=asof,
        spot=chain.spot,
        atm_strike=chain.atm_strike,
        expiries=chain.expiries,
        channels=channels,
        spot_detail=chain.spot_detail,
        strike_step=chain.strike_step,
        lot_size=chain.lot_size,
        freeze_qty=chain.freeze_qty,
    )


def resolve_capture_from_disk(
    config_path: Path | str = _DEFAULT_CONFIG,
    *,
    asof: date | None = None,
    spot: float | None = None,
    master_path: Path | str | None = None,
    refresh: bool = False,
) -> CaptureUniverse:
    """Load the capture config and the master from disk, then resolve.

    ``refresh`` re-downloads the master via ``instruments.ensure_master``, which
    needs the *instruments* config rather than this one -- kept off by default so
    the resolve path does no network I/O unless asked.
    """
    config = load_capture_config(config_path)
    if master_path is None:
        rel = config.get("instrument_master_path", "data/reference/api-scrip-master-detailed.csv")
        master_path = _REPO_ROOT / rel
    if refresh:
        from .instruments import ensure_master, load_config as load_instruments_config

        master_path = ensure_master(load_instruments_config(), force=False)
    return resolve_capture_universe(read_master(master_path), config, asof=asof, spot=spot)
