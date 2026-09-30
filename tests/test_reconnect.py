"""Tests for the reconnect policy.

The thing under test is not "does it retry". It is the rule that inverts the
usual convention: **every disconnect reason Dhan documents is fatal, and every
code it does not document is retryable.** That rule is only defensible if it is
enforced by construction rather than by a hand-maintained list, because the
failure mode is silent and expensive -- a retry loop that trips ``805``
repeatedly kills the sockets that are still working, on a day that cannot be
re-run.

So the central test here is not a table of codes; it is
:func:`test_every_documented_reason_is_fatal_by_construction`, which asserts the
fatal set *is* the documented set. Add a code to ``DISCONNECT_REASONS`` next year
and it becomes fatal automatically, with no second place to remember.

The rest is arithmetic (the backoff sequence and the attempt budget, both of
which have to be checkable without waiting) and the boundary between a vendor
verdict and an exhausted budget, which the manifest must not conflate.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bnfmm.data import protocol as proto
from bnfmm.data import reconnect as rc

# -- the verdict --------------------------------------------------------------


def test_every_documented_reason_is_fatal_by_construction():
    """The fatal set is the documented set -- not a copy of it that can drift.

    Written as an identity rather than a list of five codes on purpose. A future
    vendor code added to ``DISCONNECT_REASONS`` must inherit the fatal treatment
    without anyone remembering to update a second place.
    """
    assert proto.FATAL_DISCONNECTS == frozenset(proto.DISCONNECT_REASONS)
    for code in sorted(proto.DISCONNECT_REASONS):
        verdict = rc.should_reconnect(code)
        assert verdict.retry is False, code
        assert verdict.fatal_code == code
        assert verdict.needs_operator is True
        # The operator gets the vendor's own words, not a paraphrase.
        assert proto.DISCONNECT_REASONS[code] in verdict.reason
        assert str(code) in verdict.reason


def test_a_bare_close_is_retryable():
    """No disconnect frame is the ordinary internet: reset, Wi-Fi blip, proxy."""
    verdict = rc.should_reconnect(None)
    assert verdict.retry is True
    assert verdict.fatal_code is None
    assert verdict.needs_operator is False


@pytest.mark.parametrize("code", [0, 1, 42, 804, 810, 900, 99999, -1])
def test_an_undocumented_code_is_retryable(code):
    """Unknown is not the same as hopeless.

    A hard stop on a code the vendor shipped last week costs the session; a few
    bounded retries that name the code cost almost nothing.
    """
    assert code not in proto.DISCONNECT_REASONS
    verdict = rc.should_reconnect(code)
    assert verdict.retry is True
    assert verdict.fatal_code is None
    assert str(code) in verdict.reason


def test_805_is_fatal_and_names_the_connection_cap():
    """The dangerous one: retrying into 805 drops the *oldest* socket, i.e. a live one."""
    verdict = rc.should_reconnect(805)
    assert verdict.retry is False
    assert verdict.needs_operator is True
    assert "connection" in verdict.reason.lower()


def test_entitlement_failure_is_only_806():
    assert rc.ENTITLEMENT_DISCONNECT == 806
    assert rc.is_entitlement_failure(806) is True
    for other in (None, 805, 807, 808, 809, 42):
        assert rc.is_entitlement_failure(other) is False


def test_806_is_distinguishable_from_the_other_fatals():
    """"You have not paid" and "you are broken" need different operator actions."""
    assert rc.should_reconnect(806).fatal_code == 806
    assert rc.is_entitlement_failure(rc.should_reconnect(806).fatal_code)
    assert not rc.is_entitlement_failure(rc.should_reconnect(807).fatal_code)


def test_verdict_is_truthy_iff_it_retries():
    """So ``if should_reconnect(code):`` cannot read backwards at a call site."""
    assert bool(rc.should_reconnect(None)) is True
    assert bool(rc.should_reconnect(806)) is False


# -- backoff ------------------------------------------------------------------


def test_unjittered_sequence_is_the_documented_doubling():
    b = rc.Backoff(base=1.0, maximum=60.0, multiplier=2.0, jitter=0.0)
    assert b.sequence(9) == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


def test_sequence_does_not_mutate_the_instance():
    """``sequence`` is for logs and tests; only ``next_delay`` advances state."""
    b = rc.Backoff()
    b.sequence(5)
    assert b.attempts == 0
    assert b.next_delay() == pytest.approx(b.unjittered(1), rel=b.jitter + 1e-9)


def test_unjittered_is_one_based():
    b = rc.Backoff()
    with pytest.raises(ValueError):
        b.unjittered(0)
    with pytest.raises(ValueError):
        b.unjittered(-3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base": 0.0},
        {"base": -1.0},
        {"maximum": 0.5},  # below base
        {"multiplier": 0.9},
        {"jitter": 1.0},
        {"jitter": -0.1},
    ],
)
def test_nonsense_backoff_config_raises_at_construction(kwargs):
    """A bad delay curve must fail at 08:50, not at the first drop at 11:04."""
    with pytest.raises(ValueError):
        rc.Backoff(**kwargs)


def test_jitter_stays_inside_the_documented_band():
    b = rc.Backoff(base=4.0, maximum=4.0, jitter=0.25, seed=7)
    delays = [b.next_delay() for _ in range(200)]
    assert all(3.0 <= d <= 5.0 for d in delays)
    # Multiplicative, so it must actually vary -- a constant would still pass the
    # bound check above while defeating the whole purpose.
    assert len(set(delays)) > 100


def test_jitter_is_deterministic_for_a_seed():
    """Seeded per instance, never from the module generator, so tests can assert."""
    a = rc.Backoff(seed=11)
    b = rc.Backoff(seed=11)
    assert [a.next_delay() for _ in range(6)] == [b.next_delay() for _ in range(6)]


def test_different_seeds_diverge():
    """Three channels dropping on one Wi-Fi blip must not retry in lockstep.

    ``capture.py`` seeds each channel with its index for exactly this reason; if
    the seed were ignored the server would see a burst rather than three
    independent reconnects.
    """
    a = [rc.Backoff(seed=0).next_delay() for _ in range(4)]
    b = [rc.Backoff(seed=1).next_delay() for _ in range(4)]
    assert a != b


def test_zero_jitter_is_exact():
    b = rc.Backoff(base=2.0, multiplier=3.0, jitter=0.0)
    assert [b.next_delay() for _ in range(3)] == [2.0, 6.0, 18.0]


def test_reset_returns_to_the_base_delay():
    b = rc.Backoff(jitter=0.0)
    for _ in range(5):
        b.next_delay()
    assert b.attempts == 5
    b.reset()
    assert b.attempts == 0
    assert b.next_delay() == b.base


def test_iterating_a_backoff_yields_advancing_delays():
    b = rc.Backoff(jitter=0.0)
    got = []
    for delay in b:
        got.append(delay)
        if len(got) == 4:
            break
    assert got == [1.0, 2.0, 4.0, 8.0]


# -- the policy ---------------------------------------------------------------


def test_policy_retries_a_bare_close_with_a_delay():
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(jitter=0.0))
    attempt = policy.on_disconnect(None)
    assert attempt.retry is True
    assert bool(attempt) is True
    assert attempt.delay_s == 1.0
    assert attempt.attempt == 1
    assert policy.total_reconnects == 1
    assert policy.stopped is False


def test_a_fatal_code_stops_the_policy_and_latches():
    policy = rc.ReconnectPolicy()
    attempt = policy.on_disconnect(807)
    assert attempt.retry is False
    assert attempt.needs_operator is True
    assert attempt.exhausted is False
    assert policy.stopped is True
    assert policy.fatal_code == 807
    assert "807" in policy.fatal_reason


def test_a_fatal_verdict_consumes_no_attempt_and_no_reconnect():
    """Otherwise a hard stop inflates the counters that describe the session.

    ``total_reconnects`` is read straight into the session manifest, and a
    manifest claiming a reconnect that never happened is worse than one that
    undercounts, because it implies the channel recovered.
    """
    policy = rc.ReconnectPolicy()
    policy.on_disconnect(806)
    assert policy.backoff.attempts == 0
    assert policy.total_reconnects == 0


def test_data_arriving_resets_the_attempt_budget():
    """A six-hour session with a drop every hour must never exhaust its budget."""
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(jitter=0.0), max_attempts=3)
    for _ in range(50):
        attempt = policy.on_disconnect(None)
        assert attempt.retry is True
        assert attempt.delay_s == 1.0, "each drop is the first after data"
        policy.on_data()
    assert policy.stopped is False
    assert policy.total_reconnects == 50


def test_repeated_failures_without_data_exhaust_the_budget():
    """Bounded on purpose: a channel retrying forever with no data looks alive."""
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(jitter=0.0), max_attempts=3)
    assert [policy.on_disconnect(None).delay_s for _ in range(3)] == [1.0, 2.0, 4.0]
    final = policy.on_disconnect(None)
    assert final.retry is False
    assert final.exhausted is True
    assert final.needs_operator is True
    assert "giving up" in final.reason
    assert policy.stopped is True
    # An exhausted budget is not a vendor code, and the manifest must not invent one.
    assert policy.fatal_code is None
    assert final.fatal_code is None


def test_exhaustion_takes_long_enough_to_outlast_an_outage():
    """The budget is a duration, and the duration is the thing that matters.

    Twenty attempts at 1,2,4,...,60 s is roughly a quarter of an hour: long
    enough that a broadband outage or a laptop resume does not end the session,
    short enough that a genuinely dead channel is reported inside it. Both bounds
    are asserted because getting either wrong is a lost session.
    """
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(jitter=0.0))
    total = 0.0
    while True:
        attempt = policy.on_disconnect(None)
        if not attempt.retry:
            break
        total += attempt.delay_s
    assert 10 * 60 <= total <= 20 * 60, total
    assert policy.backoff.attempts == rc.DEFAULT_MAX_ATTEMPTS


def test_delays_never_exceed_the_configured_maximum():
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(maximum=5.0), max_attempts=40)
    for _ in range(30):
        attempt = policy.on_disconnect(None)
        if attempt.retry:
            assert attempt.delay_s <= 5.0 * (1.0 + policy.backoff.jitter)


def test_undocumented_codes_are_retried_but_still_bounded():
    """Retryable, not unlimited: the code is reported once the budget is spent."""
    policy = rc.ReconnectPolicy(backoff=rc.Backoff(jitter=0.0), max_attempts=2)
    assert policy.on_disconnect(811).retry is True
    assert policy.on_disconnect(811).retry is True
    final = policy.on_disconnect(811)
    assert final.retry is False
    assert "811" in final.reason


def test_manifest_is_json_ready_and_carries_the_stop_reason():
    policy = rc.ReconnectPolicy()
    policy.on_disconnect(None)
    policy.on_disconnect(805)
    manifest = policy.manifest()
    assert json.loads(json.dumps(manifest)) == manifest
    assert manifest["stopped"] is True
    assert manifest["fatal_code"] == 805
    assert manifest["total_reconnects"] == 1
    assert manifest["max_attempts"] == rc.DEFAULT_MAX_ATTEMPTS
    assert manifest["base_delay_s"] == rc.DEFAULT_BASE_DELAY_S


def test_manifest_of_a_clean_channel_says_nothing_happened():
    manifest = rc.ReconnectPolicy().manifest()
    assert manifest["stopped"] is False
    assert manifest["fatal_reason"] is None
    assert manifest["total_reconnects"] == 0


# -- the constraints that keep the policy testable ----------------------------


SOURCE = Path(rc.__file__).read_text()


def test_the_module_never_sleeps_and_never_reads_a_clock():
    """Nothing here waits. ``channels.py`` awaits the returned delay instead.

    This is what lets a fifteen-minute retry budget be exercised in microseconds,
    which is the only reason the arithmetic above is tested at all. Checked by
    walking the AST rather than grepping, because the module's own prose says the
    words "sleep" and "clock".
    """
    called = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    for banned in ("sleep", "time", "time_ns", "monotonic", "monotonic_ns", "now", "perf_counter"):
        assert banned not in called, banned


def test_the_module_imports_no_asyncio_and_no_socket_library():
    """A pure verdict cannot depend on the transport it is deciding about."""
    imported = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "random", "dataclasses", "typing"}, imported


def test_jitter_comes_from_an_instance_generator_not_the_module_one():
    """``random.random()`` at module scope would make two channels share a stream.

    It would also make every test above flaky, which is how this class of bug
    normally gets discovered -- late, and in something unrelated.
    """
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "random":
                # ``random.Random(...)`` is the constructor; anything else is the
                # shared module-level generator.
                assert node.func.attr == "Random", ast.unparse(node)


def test_the_server_silence_timeout_is_re_exported():
    """``capture.py`` sizes its keepalive against this, so it must travel with the policy."""
    assert rc.SERVER_SILENCE_TIMEOUT_S == proto.SERVER_SILENCE_TIMEOUT_S
    assert rc.SERVER_SILENCE_TIMEOUT_S == 40.0
    # The base retry delay must be well under it, or the first reconnect attempt
    # lands after the server has already given up on the previous socket.
    assert rc.DEFAULT_BASE_DELAY_S < rc.SERVER_SILENCE_TIMEOUT_S
