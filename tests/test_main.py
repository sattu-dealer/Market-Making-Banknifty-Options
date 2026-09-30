"""Tests for `main.py`: the capture-window planner and session discovery.

The window planner is the only piece of this project whose failure mode is a
*lost trading session*. Everything else can be re-run offline; a morning that
was not recorded does not come back. So it is tested against explicit wall-clock
instants rather than against `datetime.now`, including the two cases that
actually matter -- launched the night before, and launched after the close.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import main as m  # noqa: E402

IST = m.IST


def cal(**overrides) -> m.Calendar:
    """A calendar with the shipped defaults, overridable per test."""
    base = {
        "holidays": frozenset(),
        "holidays_verified": False,
        "capture_start": dtime(8, 58),
        "capture_stop": dtime(15, 35),
        "continuous_start": dtime(9, 15),
        "continuous_end": dtime(15, 30),
        "preflight_lead_s": 600.0,
    }
    return m.Calendar(**{**base, **overrides})


def ist(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


# --- the calendar dates these tests are built on --------------------------------


def test_reference_weekdays_are_what_the_tests_assume():
    """Guard the fixtures themselves.

    Every case below encodes an assumption about which 2026 dates are weekends.
    If that is wrong the suite would pass while testing nothing, so assert it.
    """
    assert date(2026, 8, 22).weekday() == 5, "2026-08-22 should be a Saturday"
    assert date(2026, 8, 23).weekday() == 6, "2026-08-23 should be a Sunday"
    assert date(2026, 8, 24).weekday() == 0, "2026-08-24 should be a Monday"
    assert date(2026, 8, 25).weekday() == 1, "2026-08-25 should be a Tuesday"
    assert date(2026, 8, 28).weekday() == 4, "2026-08-28 should be a Friday"


# --- trading-day rule -----------------------------------------------------------


def test_weekends_are_not_trading_days():
    c = cal()
    assert not c.is_trading_day(date(2026, 8, 22))
    assert not c.is_trading_day(date(2026, 8, 23))
    assert c.is_trading_day(date(2026, 8, 24))


def test_a_listed_holiday_is_not_a_trading_day():
    c = cal(holidays=frozenset({date(2026, 8, 24)}), holidays_verified=True)
    assert not c.is_trading_day(date(2026, 8, 24))
    assert c.is_trading_day(date(2026, 8, 25))


# --- window planning ------------------------------------------------------------


def test_sunday_night_plans_monday_open():
    """Tonight's actual case: launched Sunday 23:00, must land on Monday's open."""
    window = m.plan_window(cal(), ist(2026, 8, 23, 23, 0))
    assert window.day == date(2026, 8, 24)
    assert window.start == ist(2026, 8, 24, 8, 58)
    assert window.stop == ist(2026, 8, 24, 15, 35)
    assert not window.partial


def test_seven_am_on_a_trading_day_waits_for_the_same_days_open():
    """The user's stated case: started at 7 AM, must wait -- not skip to tomorrow."""
    now = ist(2026, 8, 24, 7, 0)
    window = m.plan_window(cal(), now)
    assert window.day == date(2026, 8, 24)
    assert window.start == ist(2026, 8, 24, 8, 58)
    assert not window.partial
    assert window.wait_s(now) == pytest.approx(118 * 60)


def test_midsession_launch_starts_immediately_and_is_flagged_partial():
    now = ist(2026, 8, 24, 11, 30)
    window = m.plan_window(cal(), now)
    assert window.day == date(2026, 8, 24)
    assert window.partial, "the open is already gone; the caller must be told"
    assert window.wait_s(now) == 0.0
    # Duration runs from *now*, not from the nominal start, or capture would be
    # handed a deadline that already expired.
    assert window.duration_s(now) == pytest.approx((4 * 60 + 5) * 60)


def test_after_the_close_rolls_to_the_next_trading_day():
    window = m.plan_window(cal(), ist(2026, 8, 24, 16, 0))
    assert window.day == date(2026, 8, 25)
    assert not window.partial


def test_exactly_at_the_stop_rolls_forward():
    """The boundary: at 15:35:00 there is nothing left to record today."""
    window = m.plan_window(cal(), ist(2026, 8, 24, 15, 35))
    assert window.day == date(2026, 8, 25)


def test_one_second_before_the_stop_still_captures_today():
    now = ist(2026, 8, 24, 15, 34) + timedelta(seconds=59)
    window = m.plan_window(cal(), now)
    assert window.day == date(2026, 8, 24)
    assert window.partial
    assert window.duration_s(now) == pytest.approx(1.0)


def test_saturday_plans_monday():
    window = m.plan_window(cal(), ist(2026, 8, 22, 10, 0))
    assert window.day == date(2026, 8, 24)


def test_friday_evening_plans_monday():
    window = m.plan_window(cal(), ist(2026, 8, 28, 18, 0))
    assert window.day == date(2026, 8, 31)
    assert date(2026, 8, 31).weekday() == 0


def test_holidays_are_skipped_when_planning_forward():
    c = cal(
        holidays=frozenset({date(2026, 8, 24), date(2026, 8, 25)}),
        holidays_verified=True,
    )
    window = m.plan_window(c, ist(2026, 8, 23, 23, 0))
    assert window.day == date(2026, 8, 26)


def test_no_trading_day_in_range_raises_rather_than_looping():
    """A mistakenly enormous holiday list must fail loudly, not spin."""
    every_day = frozenset(date(2026, 8, 23) + timedelta(days=i) for i in range(40))
    with pytest.raises(SystemExit, match="no trading day"):
        m.plan_window(cal(holidays=every_day, holidays_verified=True), ist(2026, 8, 23, 23, 0))


def test_duration_never_goes_negative():
    window = m.plan_window(cal(), ist(2026, 8, 24, 7, 0))
    assert window.duration_s(ist(2026, 8, 24, 23, 0)) == 0.0


def test_full_session_is_the_expected_length():
    """08:58 to 15:35 is 6h37m. If this changes, it should change deliberately."""
    now = ist(2026, 8, 24, 7, 0)
    window = m.plan_window(cal(), now)
    assert window.duration_s(now) == pytest.approx((6 * 60 + 37) * 60)


# --- calendar loading -----------------------------------------------------------


def test_load_calendar_reads_the_shipped_config():
    c = m.load_calendar()
    assert c.capture_start < c.continuous_start, "capture must start before continuous trading"
    assert c.continuous_end < c.capture_stop, "capture must outlast the close"
    assert c.preflight_lead_s > 0


def test_shipped_config_declares_holidays_unverified():
    """The shipped list is empty on purpose; the flag must say so.

    If someone fills the list in, this test flips to asserting the opposite --
    which is the point: `holidays_verified` must never be True by accident.
    """
    c = m.load_calendar()
    assert c.holidays_verified == bool(c.holidays)


def test_load_calendar_falls_back_when_the_file_is_absent(tmp_path):
    c = m.load_calendar(tmp_path / "nope.yaml")
    assert c.capture_start == dtime(8, 58)
    assert c.capture_stop == dtime(15, 35)
    assert not c.holidays_verified


def test_load_calendar_parses_holiday_strings(tmp_path):
    path = tmp_path / "cal.yaml"
    path.write_text(
        "holidays: ['2026-10-02', '2026-12-25']\n"
        "sessions:\n"
        "  capture_start: '09:05'\n"
        "  capture_stop: '15:40'\n"
        "preflight_lead_s: 120\n"
    )
    c = m.load_calendar(path)
    assert c.holidays == frozenset({date(2026, 10, 2), date(2026, 12, 25)})
    assert c.holidays_verified
    assert c.capture_start == dtime(9, 5)
    assert c.capture_stop == dtime(15, 40)
    assert c.preflight_lead_s == 120


# --- the --spot override --------------------------------------------------------


def test_spot_override_defaults_to_absent():
    """Unset must mean *unset*, not 0.0.

    `capture.py` treats `--spot` as "override the circuit-band bootstrap", and 0.0
    is a perfectly valid float that would resolve the ladder around strike zero.
    So the default has to be None and the forwarding has to be conditional.
    """
    args = m.build_parser().parse_args(["--mode", "record"])
    assert args.spot is None


def test_spot_override_parses_as_a_float():
    args = m.build_parser().parse_args(["--mode", "record", "--spot", "58250.5"])
    assert args.spot == pytest.approx(58250.5)


def test_spot_override_rejects_nonsense():
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(["--mode", "record", "--spot", "atm"])


def test_preflight_accepts_a_spot_override():
    """Guard the pass-through, not the arithmetic.

    The dry run inside `preflight` is what prints the ladder an operator signs off
    on at 08:48. If `--spot` reached the capture but not the dry run, preflight
    would confirm a universe nobody subscribes to -- worse than having no flag at
    all. A signature check is a cheap standing guard against a refactor quietly
    dropping the parameter; asserting the resolved ladder itself belongs in
    tests/test_universe.py, which owns the resolver.
    """
    import inspect

    assert "spot" in inspect.signature(m.preflight).parameters


# --- waiting --------------------------------------------------------------------


def test_sleep_until_returns_immediately_for_a_past_target():
    """Must not sleep, and must not raise, when the target has gone."""
    m.sleep_until(datetime.now(tz=IST) - timedelta(hours=1), label="past")


def test_announce_cadence_tightens_as_the_target_nears():
    assert m._announce_cadence(9 * 3600) > m._announce_cadence(30 * 60)
    assert m._announce_cadence(30 * 60) > m._announce_cadence(5 * 60)
    assert m._announce_cadence(5 * 60) > m._announce_cadence(30)


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0s"), (45, "45s"), (600, "10m"), (3600, "60m"), (9 * 3600, "9.0h")],
)
def test_human_readable_durations(seconds, expected):
    assert m._human(seconds) == expected


def test_human_clamps_negatives():
    assert m._human(-5) == "0s"


# --- pipeline status ------------------------------------------------------------


def test_pipeline_status_is_probed_not_declared():
    """The `read` stage exists today and `fills` does not.

    This is the guard on the honesty of the status table: it must reflect the
    actual importability of each module, so it cannot drift out of date as
    phases land.
    """
    by_name = {s.name: s for s in m.PIPELINE}
    assert by_name["read"].available, "store.read_capture exists and is Phase 1a"
    assert not by_name["fills"].available, "the fill simulator is Phase 3 and unwritten"


def test_every_pipeline_stage_names_a_phase_and_a_purpose():
    for stage in m.PIPELINE:
        assert stage.phase, stage.name
        assert stage.what_it_does, stage.name
        assert stage.module.startswith("bnfmm."), stage.name


def test_a_bogus_stage_reports_unavailable_rather_than_raising():
    stage = m.Stage("bogus", "bnfmm.does.not.exist", None, "9", "nothing")
    assert not stage.available


# --- discovery ------------------------------------------------------------------


def test_discover_returns_recorded_sessions_without_raising():
    """Runs against whatever is on disk. Asserts the shape, not the content.

    Deliberately does *not* assert a session-id format: ids are `YYYYMMDD-HHMMSS`
    only when auto-generated, and `--session-id` accepts any string, so pinning
    the length here would fail on a legitimately named run.
    """
    sessions = m.discover(include_connect_tests=True)
    for s in sessions:
        assert s.session_id, "a session with no id would be unselectable"
        assert s.total_rows >= 0
        assert s.raw_bytes >= 0
        assert s.day_label


def test_connect_tests_are_hidden_by_default():
    everything = m.discover(include_connect_tests=True)
    market_only = m.discover(include_connect_tests=False)
    assert len(market_only) <= len(everything)
    assert not any(s.connect_test for s in market_only)


def test_capture_roots_come_from_the_capture_config():
    raw, pq = m._capture_roots()
    assert raw.name == "raw"
    assert pq.name == "parquet"
