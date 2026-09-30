"""Tests for the capture universe resolver.

Three claims are being tested, and only the first is about code being correct in
the ordinary sense.

1. **The arithmetic.** The plan this config was written from contained the slip
   that motivated the whole module -- "+/-12 strikes (25 strikes x 2 = 48)" is
   really 50 legs, so with two futures it is 52 and over the 50-slot depth cap.
   The shipped ``config/capture.yaml`` uses +/-11 = 46 legs + 2 futures = 48, and
   that multiplication is asserted here against the real resolver rather than
   trusted to a comment.

2. **Caps bind at resolve time.** A universe that does not fit must fail or
   truncate *visibly* at 08:50. Silent truncation is the expensive failure: a
   strike that was never subscribed and a strike that never traded look identical
   in the Parquet a month later.

3. **The protected set is protected.** "Never the front future, never the
   indices, never the at-the-money core" is a sentence in a plan until
   ``required`` turns it into an exception.

Everything runs against the synthetic master from ``conftest``, because the real
36 MB master is gitignored and goes stale daily.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from bnfmm.data import protocol as proto
from bnfmm.data import universe as uni
from .conftest import ASOF, REPO_ROOT, build_master

CONFIG_PATH = REPO_ROOT / "config" / "capture.yaml"

#: A 111-strike ladder at 100-point spacing, wide enough for the general feed's
#: +/-50 band. The default conftest ladder is 16 strikes, which would clamp every
#: band and hide exactly the arithmetic these tests exist to check.
WIDE_STRIKES = tuple(float(k) for k in range(52_000, 63_100, 100))

FRONT_EXPIRY = ASOF + timedelta(days=2)
NEXT_EXPIRY = ASOF + timedelta(days=37)


@pytest.fixture
def wide_master():
    return build_master(strikes=WIDE_STRIKES)


@pytest.fixture(scope="module")
def shipped_config() -> dict:
    return uni.load_capture_config(CONFIG_PATH)


def config_of(*channels: dict, **top) -> dict:
    """A minimal capture config around hand-written channel definitions."""
    return {
        "underlying": "BANKNIFTY",
        "exchange": "NSE",
        "segment": "D",
        "reserve_connections": top.pop("reserve_connections", 2),
        "channels": list(channels),
        **top,
    }


def depth_channel(*groups: dict, **kw) -> dict:
    return {"name": "depth", "protocol": "depth_20", "groups": list(groups), **kw}


def future_group(expiry="front", **kw) -> dict:
    return {"name": f"{expiry}_future", "kind": "future", "expiry": expiry, **kw}


def option_group(name, expiry="front", band=4, **kw) -> dict:
    return {
        "name": name,
        "kind": "options",
        "expiry": expiry,
        "strike_band": band,
        "option_types": ["CE", "PE"],
        **kw,
    }


# -- the shipped config, which is the thing that runs tomorrow ----------------


def test_the_shipped_config_loads_and_validates(shipped_config):
    specs = uni.channel_specs(shipped_config)
    assert [s.name for s in specs] == ["depth", "feed", "chain"]
    assert [s.protocol for s in specs] == ["depth_20", "market_feed", "option_chain"]
    # Two sockets plus two held in reserve is four of five. The reserve is what
    # makes reconnect-after-suspend possible without waiting for the server to
    # forget a half-open socket.
    sockets = sum(1 for s in specs if s.protocol in uni.SOCKET_PROTOCOLS)
    assert sockets + shipped_config["reserve_connections"] <= proto.MAX_WEBSOCKET_CONNECTIONS


def test_the_depth_band_is_48_of_50_not_52(wide_master, shipped_config):
    """The corrected arithmetic, asserted against the resolver rather than a comment.

    2 futures + (2*11 + 1) strikes x 2 legs = 2 + 46 = 48, leaving 2 slots spare.
    The rejected +/-12 version is checked immediately below.
    """
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.cap == proto.DEPTH_MAX_INSTRUMENTS == 50
    assert depth.slots_used == 48
    assert depth.slots_spare == 2
    assert depth.truncated is False
    assert universe.truncated is False

    strikes = {c.strike for c in depth.contracts if c.strike is not None}
    assert len(strikes) == 23, "2*11 + 1"
    assert len([c for c in depth.contracts if c.instrument == "OPTIDX"]) == 46
    assert len([c for c in depth.contracts if c.instrument == "FUTIDX"]) == 2


def test_plus_minus_twelve_would_overflow_the_depth_cap(wide_master, shipped_config):
    """The original slip, reproduced: 25 strikes x 2 + 2 futures = 52 > 50.

    It truncates rather than raising because ``front_band`` is deliberately not
    ``required`` -- it is the group the drop order is designed to eat. What matters
    is that the loss is *reported*, which is the assertion on ``dropped``.
    """
    config = copy.deepcopy(shipped_config)
    band = next(g for g in config["channels"][0]["groups"] if g["name"] == "front_band")
    band["strike_band"] = 12

    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.slots_used == 50, "the cap holds"
    assert depth.truncated is True
    assert universe.truncated is True

    fill = next(f for f in depth.fills if f.group.name == "front_band")
    assert len(fill.dropped) == 2
    assert "DROPPED" in fill.describe()
    assert "TRUNCATED" in universe.describe()
    # Furthest first, and both legs of that strike together: 12 strikes out at
    # 100-point spacing, ties broken low-first, so the top-ranked casualty is the
    # upper one.
    assert {c.strike for c in fill.dropped} == {59_000.0}
    assert {c.option_type for c in fill.dropped} == {"CE", "PE"}


def test_the_general_feed_band_is_wide_because_breadth_is_free(wide_master, shipped_config):
    """5000 per connection, so the only reason to be narrow would be disk."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    feed = universe.channel("feed")
    assert feed.cap == proto.FEED_MAX_INSTRUMENTS == 5000
    # 2 indices + 2 futures + 101 front strikes x 2 + 61 next strikes x 2.
    assert feed.slots_used == 2 + 2 + 202 + 122 == 328
    assert feed.truncated is False


def test_the_chain_channel_subscribes_nothing_and_costs_no_socket(wide_master, shipped_config):
    """It is REST: one request covers every strike, so it has no instrument list."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    chain = universe.channel("chain")
    assert chain.contracts == ()
    assert chain.cap is None
    assert chain.slots_spare is None
    assert uni.PROTOCOL_CAPS["option_chain"] is None
    assert "option_chain" not in uni.SOCKET_PROTOCOLS


def test_indices_reach_the_feed_and_only_the_feed(wide_master, shipped_config):
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    feed_ids = [c.security_id for c in universe.channel("feed").contracts]
    assert feed_ids[:2] == [25, 13], "declared order preserved: Nifty Bank, then Nifty 50"

    depth = universe.channel("depth")
    assert all(seg == proto.NSE_FNO for seg, _ in depth.feed_keys)
    assert all(c.instrument != "INDEX" for c in depth.contracts)


def test_futures_are_subscribed_first_so_a_cap_can_never_reach_them(wide_master, shipped_config):
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    depth = universe.channel("depth")
    assert [c.instrument for c in depth.contracts[:2]] == ["FUTIDX", "FUTIDX"]
    assert depth.contracts[0].expiry == FRONT_EXPIRY
    assert depth.contracts[1].expiry == NEXT_EXPIRY


def test_the_atm_core_is_claimed_before_the_wider_band(wide_master, shipped_config):
    """Overlap is deduplicated, so the wide group contributes only what is new."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    depth = universe.channel("depth")
    core = next(f for f in depth.fills if f.group.name == "front_atm_core")
    band = next(f for f in depth.fills if f.group.name == "front_band")
    assert core.included == 18, "9 strikes x 2"
    assert band.requested == 46
    assert band.duplicates == 18
    assert band.included == 28
    assert core.included + band.included == 46


def test_expiry_day_still_resolves_the_expiring_contract(wide_master, shipped_config):
    """``>= asof``, not ``> asof`` -- which is what makes Aug 25 capturable at all.

    ``instruments.resolve()`` cannot be reused here precisely because its
    ``min_days_to_expiry: 3`` filter would discard the contract that is the point
    of the exercise.
    """
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=FRONT_EXPIRY)
    assert universe.front_expiry == FRONT_EXPIRY
    assert (universe.front_expiry - FRONT_EXPIRY).days == 0, "0 DTE"


def test_the_calendar_rolls_the_front_month_with_no_config_edit(shipped_config):
    """The morning after an expiry, ``front`` must mean the next one by itself.

    Three option expiries here, not the default two, because the shipped config
    also has a ``next`` band -- and after a roll the old ``next`` has become
    ``front``, so a two-expiry master would (correctly) raise for want of a third.
    """
    master = build_master(strikes=WIDE_STRIKES, option_offsets=(2, 37, 65))
    day_after = FRONT_EXPIRY + timedelta(days=1)
    universe = uni.resolve_capture_universe(master, shipped_config, asof=day_after)
    assert universe.front_expiry == NEXT_EXPIRY
    assert universe.next_expiry == ASOF + timedelta(days=65)
    depth = universe.channel("depth")
    assert all(c.expiry >= NEXT_EXPIRY for c in depth.contracts)
    assert not any(c.expiry == FRONT_EXPIRY for c in depth.contracts)
    assert depth.slots_used == 48, "the roll costs no slots"


def test_a_selector_beyond_the_available_expiries_says_how_many_there_are(shipped_config):
    """The two-expiry case the test above sidesteps: it must raise, not resolve.

    A config asking for a September band on a master that only knows about August
    is a stale master, and resolving it quietly would spend the session's depth
    slots on the wrong month.
    """
    master = build_master(strikes=WIDE_STRIKES, option_offsets=(2, 37))
    day_after = FRONT_EXPIRY + timedelta(days=1)
    with pytest.raises(uni.UniverseError, match="needs at least 2 live expiries"):
        uni.resolve_capture_universe(master, shipped_config, asof=day_after)


def test_a_selector_with_no_matching_expiry_says_how_many_there_are(wide_master):
    config = config_of(depth_channel(future_group("third")))
    with pytest.raises(uni.UniverseError, match="third"):
        uni.resolve_capture_universe(wide_master, config, asof=ASOF)


# -- caps, truncation and the protected set ----------------------------------


def test_a_required_group_that_does_not_fit_raises(wide_master):
    """"Never the ATM core" becomes an exception rather than a convention.

    The greedy group is on the *next* expiry so that it cannot satisfy the front
    core by accident -- overlap is deduplicated, so a wide front band would
    include the core and there would be nothing to raise about.
    """
    config = config_of(
        depth_channel(
            option_group("wide", expiry="next", band=40),  # 81 x 2 = 162, eats the cap
            option_group("core", expiry="front", band=4, required=True),
        )
    )
    with pytest.raises(uni.UniverseError, match="required group 'core'"):
        uni.resolve_capture_universe(wide_master, config, asof=ASOF)


def test_the_error_says_which_group_to_narrow(wide_master):
    """A resolve-time failure at 08:50 is only useful if it names the fix."""
    config = config_of(
        depth_channel(
            option_group("wide", expiry="next", band=40),
            future_group("front", required=True),
        )
    )
    with pytest.raises(uni.UniverseError) as excinfo:
        uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    message = str(excinfo.value)
    assert "front_future" in message
    assert "Narrow an earlier group" in message


def test_a_wide_group_absorbs_the_core_rather_than_colliding_with_it(wide_master):
    """The converse, and the reason the two tests above use the next expiry.

    A ``required`` core that a preceding group has already subscribed is satisfied,
    not violated: the instruments are in the universe either way, which is all
    ``required`` claims.
    """
    config = config_of(
        depth_channel(
            option_group("wide", band=40),
            option_group("core", band=4, required=True),
        )
    )
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    core = universe.channel("depth").fills[1]
    assert core.duplicates == 18
    assert core.dropped == ()
    atm = universe.atm_strike
    got = {(c.strike, c.option_type) for c in universe.channel("depth").contracts}
    assert all((atm + 100 * i, side) in got for i in range(-4, 5) for side in ("CE", "PE"))


def test_truncation_drops_the_furthest_strikes_first(wide_master):
    config = config_of(depth_channel(option_group("band", band=11), cap=10))
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.slots_used == 10
    kept = sorted({c.strike for c in depth.contracts})
    atm = universe.atm_strike
    assert kept == [atm - 200, atm - 100, atm, atm + 100, atm + 200]
    dropped = next(iter(depth.fills)).dropped
    assert all(abs(c.strike - atm) > 200 for c in dropped)


def test_truncation_keeps_both_legs_of_every_retained_strike(wide_master):
    """A lone CE is worse than no CE: put-call parity needs the pair.

    The whole fair-value approach of the plan is built on ``C(K) - P(K)``, so a
    truncation that orphans one leg quietly removes a strike from the parity
    estimator while appearing to include it.
    """
    for cap in (4, 10, 20, 30):
        config = config_of(depth_channel(option_group("band", band=11), cap=cap))
        universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
        by_strike: dict[float, set[str]] = {}
        for c in universe.channel("depth").contracts:
            by_strike.setdefault(c.strike, set()).add(c.option_type)
        assert all(sides == {"CE", "PE"} for sides in by_strike.values()), cap


def test_an_odd_number_of_remaining_slots_does_not_orphan_a_leg(wide_master):
    """The hostile case for the pairing guarantee: a cap that splits a pair.

    With one future taking a slot out of an odd cap, the option band is offered an
    even number of slots only by luck. The resolver must round down to whole pairs
    rather than fill the last slot with half of one.
    """
    config = config_of(depth_channel(future_group("front"), option_group("band", band=11), cap=8))
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    legs = [c for c in depth.contracts if c.option_type is not None]
    assert len(legs) % 2 == 0
    by_strike: dict[float, set[str]] = {}
    for c in legs:
        by_strike.setdefault(c.strike, set()).add(c.option_type)
    assert all(sides == {"CE", "PE"} for sides in by_strike.values())
    assert depth.slots_used <= 8


def test_a_channel_never_exceeds_its_cap(wide_master):
    for cap in (1, 2, 3, 7, 25, 50):
        config = config_of(
            depth_channel(
                future_group("front"),
                future_group("next"),
                option_group("band", band=11),
                cap=cap,
            )
        )
        universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
        assert universe.channel("depth").slots_used <= cap, cap


def test_duplicate_instruments_are_counted_not_subscribed_twice(wide_master):
    """Two of the fifty depth slots spent on one contract is invisible on the wire."""
    config = config_of(
        depth_channel(
            future_group("front"),
            {"name": "front_future_again", "kind": "future", "expiry": "front"},
        )
    )
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.slots_used == 1
    again = depth.fills[1]
    assert again.requested == 1
    assert again.included == 0
    assert again.duplicates == 1
    assert "already present" in again.describe()


def test_the_same_contract_on_two_channels_is_counted_once_overall(wide_master, shipped_config):
    """Dedup is per channel -- the front future belongs on both feeds."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    depth_ids = {c.security_id for c in universe.channel("depth").contracts}
    feed_ids = {c.security_id for c in universe.channel("feed").contracts}
    assert depth_ids & feed_ids, "the futures and near strikes are on both"
    assert len(universe.all_contracts()) == len(depth_ids | feed_ids)


# -- configuration validation -------------------------------------------------


def test_an_index_on_the_depth_feed_raises():
    """An index has no order book, and there are only fifty depth slots."""
    with pytest.raises(uni.UniverseError, match="no order book"):
        uni.ChannelSpec(
            name="depth",
            protocol="depth_20",
            groups=(uni.Group(name="indices", kind="index", security_ids=(25,)),),
        )


def test_a_cap_above_the_documented_limit_raises():
    with pytest.raises(uni.UniverseError, match="exceeds the documented"):
        uni.ChannelSpec(name="depth", protocol="depth_20", groups=(), cap=51)


def test_a_cap_defaults_to_the_protocol_limit():
    assert uni.ChannelSpec(name="d", protocol="depth_20", groups=()).cap == 50
    assert uni.ChannelSpec(name="f", protocol="market_feed", groups=()).cap == 5000
    assert uni.ChannelSpec(name="c", protocol="option_chain", groups=()).cap is None


def test_an_unknown_protocol_raises():
    with pytest.raises(uni.UniverseError, match="protocol"):
        uni.ChannelSpec(name="x", protocol="depth_5", groups=())


def test_the_connection_budget_counts_the_reserve_against_the_limit():
    """Five is the limit and a sixth kills the *first* socket -- i.e. a working one.

    Four sockets plus two in reserve is six, so it must fail at resolve time. The
    shipped config's three-of-five is the same arithmetic with room to spare, and
    is asserted separately above.
    """
    four = [
        {"name": f"d{i}", "protocol": "depth_20", "groups": [future_group()]} for i in range(4)
    ]
    with pytest.raises(uni.UniverseError, match="which is the one that is working"):
        uni.channel_specs(config_of(*four, reserve_connections=2))
    # The same four fit if the reserve is cut to one, which is the documented
    # scale-up trade: more depth coverage, less tolerance for a half-open socket.
    assert len(uni.channel_specs(config_of(*four, reserve_connections=1))) == 4


def test_the_reserve_is_what_makes_the_scale_up_a_decision():
    """Two depth sockets (100 instruments) still leaves two in reserve; three does not."""
    def sockets(n):
        return [
            {"name": f"d{i}", "protocol": "depth_20", "groups": [future_group()]}
            for i in range(n)
        ] + [{"name": "chain", "protocol": "option_chain", "groups": []}]

    assert len(uni.channel_specs(config_of(*sockets(3), reserve_connections=2))) == 4
    with pytest.raises(uni.UniverseError):
        uni.channel_specs(config_of(*sockets(4), reserve_connections=2))


def test_a_disabled_channel_does_not_consume_a_connection():
    three = [
        {"name": f"d{i}", "protocol": "depth_20", "groups": [future_group()], "enabled": i < 2}
        for i in range(3)
    ]
    specs = uni.channel_specs(config_of(*three, reserve_connections=2))
    assert [s.enabled for s in specs] == [True, True, False]


def test_duplicate_channel_names_raise():
    dup = {"name": "depth", "protocol": "depth_20", "groups": []}
    with pytest.raises(uni.UniverseError, match="duplicate channel names"):
        uni.channel_specs(config_of(dup, dict(dup)))


def test_a_config_with_no_channels_raises():
    with pytest.raises(uni.UniverseError, match="no channels"):
        uni.channel_specs({"channels": []})


def test_a_non_mapping_config_file_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(uni.UniverseError, match="must be a mapping"):
        uni.load_capture_config(path)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"name": "g", "kind": "spread"}, "kind"),
        ({"name": "g", "kind": "index"}, "security_ids"),
        ({"name": "g", "kind": "future", "expiry": "fourth"}, "expiry"),
        ({"name": "g", "kind": "future"}, "expiry"),
        ({"name": "g", "kind": "options", "expiry": "front"}, "strike_band"),
        ({"name": "g", "kind": "options", "expiry": "front", "strike_band": -1}, "strike_band"),
        (
            {
                "name": "g",
                "kind": "options",
                "expiry": "front",
                "strike_band": 4,
                "option_types": (),
            },
            "option_types",
        ),
    ],
)
def test_nonsense_groups_raise_at_construction(kwargs, match):
    with pytest.raises(uni.UniverseError, match=match):
        uni.Group(**kwargs)


def test_a_group_missing_a_key_names_the_key():
    with pytest.raises(uni.UniverseError, match="'kind'"):
        uni._group_from_config({"name": "g"})


def test_a_band_of_zero_is_the_atm_strike_only(wide_master):
    config = config_of(depth_channel(option_group("atm", band=0)))
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.slots_used == 2
    assert {c.strike for c in depth.contracts} == {universe.atm_strike}


def test_option_types_can_be_restricted_to_one_side(wide_master):
    config = config_of(
        depth_channel(option_group("calls", band=3, option_types=["CE"]))
    )
    universe = uni.resolve_capture_universe(wide_master, config, asof=ASOF)
    depth = universe.channel("depth")
    assert depth.slots_used == 7
    assert {c.option_type for c in depth.contracts} == {"CE"}


# -- banding ------------------------------------------------------------------


def test_band_strikes_are_ordered_by_distance_from_the_money():
    strikes = [100.0, 200.0, 300.0, 400.0, 500.0]
    assert uni._band_strikes(strikes, 300.0, 1) == [300.0, 200.0, 400.0]
    assert uni._band_strikes(strikes, 300.0, 2) == [300.0, 200.0, 400.0, 100.0, 500.0]


def test_band_strikes_clamps_at_the_ends_of_the_ladder():
    strikes = [100.0, 200.0, 300.0]
    assert uni._band_strikes(strikes, 100.0, 5) == [100.0, 200.0, 300.0]
    assert uni._band_strikes([], 100.0, 5) == []


def test_band_strikes_counts_ladder_positions_not_price():
    """BANKNIFTY interleaves 100-point strikes near the money with 500-point ones.

    Banding by price would make ``strike_band`` mean a different number of
    instruments depending on where spot sits, which is exactly what a
    slot-constrained feed cannot tolerate.
    """
    strikes = [50_000.0, 55_000.0, 57_700.0, 57_800.0, 57_900.0, 60_000.0, 65_000.0]
    got = uni._band_strikes(strikes, 57_800.0, 2)
    assert len(got) == 5
    assert got[0] == 57_800.0
    assert set(got) == {55_000.0, 57_700.0, 57_800.0, 57_900.0, 60_000.0}


def test_ties_are_broken_deterministically():
    """Equidistant strikes must not reorder between runs, or manifests differ."""
    strikes = [100.0, 200.0, 300.0]
    assert uni._band_strikes(strikes, 200.0, 1) == [200.0, 100.0, 300.0]


# -- indices ------------------------------------------------------------------


def test_index_contracts_resolve_in_the_requested_order(wide_master):
    got = uni.index_contracts(wide_master, [13, 25])
    assert [c.security_id for c in got] == [13, 25]
    assert [c.instrument for c in got] == ["INDEX", "INDEX"]


def test_an_index_carries_the_sentinel_expiry_not_the_master_value(wide_master):
    """The master says ``0001-01-01``, which pandas cannot represent at all.

    This is why ``universe.py`` builds index contracts by hand rather than through
    ``instruments._to_contract``: that path calls ``pd.to_datetime`` and raises
    ``OutOfBoundsDatetime``.
    """
    (index,) = uni.index_contracts(wide_master, [25])
    assert index.expiry == uni.NO_EXPIRY == date.max
    assert index.lot_size == 0
    assert index.tick_size == 0.0
    assert index.strike is None


def test_an_index_subscribes_on_segment_zero_which_is_falsy(wide_master):
    """Segment 0 is ``IDX_I``. Any ``if segment:`` on this path drops the index."""
    (index,) = uni.index_contracts(wide_master, [25])
    assert uni.IDX_SEGMENT_CODE == 0
    assert index.feed_key == (0, "25")
    messages = proto.feed_subscriptions([index.feed_key])
    assert messages[0]["InstrumentList"] == [{"ExchangeSegment": "IDX_I", "SecurityId": "25"}]


def test_a_missing_index_id_raises_and_names_the_right_ones(wide_master):
    with pytest.raises(uni.UniverseError, match="not found in segment I"):
        uni.index_contracts(wide_master, [25, 99_999])


def test_repeated_index_ids_are_deduplicated(wide_master):
    assert len(uni.index_contracts(wide_master, [25, 25, 13])) == 2


# -- resolution failures ------------------------------------------------------


def test_an_empty_master_scope_raises(shipped_config):
    empty = build_master(strikes=WIDE_STRIKES).iloc[:0]
    with pytest.raises(uni.UniverseError, match="no BANKNIFTY rows"):
        uni.resolve_capture_universe(empty, shipped_config, asof=ASOF)


def test_a_master_whose_contracts_all_expired_raises(wide_master, shipped_config):
    """The realistic form of "the master is stale", and it must not resolve silently."""
    with pytest.raises(uni.UniverseError, match="expired before"):
        uni.resolve_capture_universe(
            wide_master, shipped_config, asof=ASOF + timedelta(days=400)
        )


def test_a_missing_future_names_the_expiries_that_do_exist(shipped_config):
    """Futures and options can disagree, and the message has to be actionable."""
    master = build_master(strikes=WIDE_STRIKES, future_offsets=(2,))
    with pytest.raises(uni.UniverseError, match="master has futures expiring"):
        uni.resolve_capture_universe(master, shipped_config, asof=ASOF)


def test_the_spot_override_beats_the_circuit_band_bootstrap(wide_master, shipped_config):
    """By 08:50 the previous close is known, and it beats an inferred estimate."""
    universe = uni.resolve_capture_universe(
        wide_master, shipped_config, asof=ASOF, spot=54_321.0
    )
    assert universe.spot == 54_321.0
    assert universe.atm_strike == 54_300.0
    assert universe.spot_detail is None, "no estimate was needed, so none is claimed"


def test_without_an_override_the_bootstrap_is_recorded_for_audit(wide_master, shipped_config):
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    assert universe.spot_detail is not None
    assert universe.atm_strike == 57_800.0
    assert universe.strike_step == 100.0
    assert universe.lot_size == 30
    assert universe.freeze_qty == 601


# -- outputs the session depends on -------------------------------------------


def test_the_manifest_is_json_ready_and_records_what_was_subscribed(
    wide_master, shipped_config
):
    """A month of Parquet whose subscription list must be *inferred* is unauditable.

    A strike that never traded and a strike that was never subscribed look
    identical downstream, so the resolved list is written out verbatim.
    """
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    manifest = universe.manifest()
    assert json.loads(json.dumps(manifest)) == manifest
    assert manifest["asof"] == ASOF.isoformat()
    assert manifest["expiries"] == [FRONT_EXPIRY.isoformat(), NEXT_EXPIRY.isoformat()]
    assert manifest["atm_strike"] == 57_800.0
    assert manifest["truncated"] is False

    depth = next(c for c in manifest["channels"] if c["name"] == "depth")
    assert depth["slots_used"] == 48
    assert len(depth["security_ids"]) == 48
    assert len(depth["subscriptions"]) == 48
    assert all(len(s) == 2 for s in depth["subscriptions"])
    assert [g["name"] for g in depth["groups"]] == [
        "front_future",
        "next_future",
        "front_atm_core",
        "front_band",
    ]


def test_the_subscription_list_survives_the_round_trip_to_wire_messages(
    wide_master, shipped_config
):
    """The resolved universe must fit the subscription builders in one message."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    depth = universe.channel("depth")
    messages = proto.depth_subscriptions(depth.instruments)
    assert len(messages) == 1, "50 fit in a single depth subscribe"
    assert messages[0]["InstrumentCount"] == 48
    ids = {entry["SecurityId"] for entry in messages[0]["InstrumentList"]}
    assert ids == {str(c.security_id) for c in depth.contracts}

    feed = universe.channel("feed")
    feed_messages = proto.feed_subscriptions(feed.instruments)
    assert len(feed_messages) == 4, "328 instruments at 100 per message"
    assert sum(m["InstrumentCount"] for m in feed_messages) == 328


def test_describe_is_human_readable_and_states_the_dte(wide_master, shipped_config):
    """Printed at 08:50, before anything connects. It is the last chance to look."""
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    text = universe.describe()
    assert "capture universe for 2026-08-23" in text
    assert "2 DTE" in text
    assert "57,800" in text
    assert "depth [depth_20] 48/50 (2 spare)" in text
    assert "TRUNCATED" not in text


def test_asking_for_an_unknown_channel_lists_the_ones_that_exist(
    wide_master, shipped_config
):
    universe = uni.resolve_capture_universe(wide_master, shipped_config, asof=ASOF)
    with pytest.raises(KeyError, match="depth"):
        universe.channel("depth5")


def test_resolve_from_disk_reads_both_files(tmp_path, shipped_config):
    """The path ``capture.py`` and ``chain_poll.py`` actually take, minus the network."""
    master_path = tmp_path / "master.csv"
    build_master(strikes=WIDE_STRIKES).to_csv(master_path, index=False)
    universe = uni.resolve_capture_from_disk(
        CONFIG_PATH, asof=ASOF, master_path=master_path
    )
    assert universe.channel("depth").slots_used == 48
    assert universe.front_expiry == FRONT_EXPIRY
