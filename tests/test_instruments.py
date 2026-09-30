"""Tests for instrument-master resolution.

The point of this module is that no BANKNIFTY contract fact is hardcoded, so
these tests are mostly about the *filters*: expired rows, roll windows, wrong
underlyings, unit conversion. Each is a failure mode that would silently
resolve the wrong contract and quietly corrupt every downstream statistic
rather than raising.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pandas as pd
import pytest

from bnfmm.data import instruments as ins

from .conftest import ASOF, FREEZE_QTY, FUT_TICK, LOT_SIZE, OPT_TICK, build_master

SPOT = 57785.0


# --- futures resolution -------------------------------------------------------


def test_front_future_skips_the_roll_window(master, instrument_config) -> None:
    """A contract 2 days from expiry is not the front contract under a 3-day
    roll rule -- its liquidity has already migrated to the next month."""
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert instrument_config["futures"]["roll_days_before_expiry"] == 3
    assert universe.front_future.days_to_expiry(ASOF) == 37


def test_expired_contracts_are_excluded(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert all(f.expiry >= ASOF for f in universe.all_futures)
    assert len(universe.all_futures) == 3, "the expired row must be dropped"


def test_all_futures_are_ordered_by_expiry(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    expiries = [f.expiry for f in universe.all_futures]
    assert expiries == sorted(expiries)


def test_next_contract_selection(master, instrument_config) -> None:
    config = copy.deepcopy(instrument_config)
    config["futures"]["contract"] = "next"
    universe = ins.resolve(master, config, asof=ASOF)
    assert universe.front_future.days_to_expiry(ASOF) == 65


def test_all_futures_inside_roll_window_raises(instrument_config) -> None:
    """Better to fail loudly than to quote a contract that is about to expire."""
    master = build_master(future_offsets=(1, 2), option_offsets=())
    with pytest.raises(ValueError, match="roll window"):
        ins.resolve(master, instrument_config, asof=ASOF)


def test_no_live_futures_raises(instrument_config) -> None:
    master = build_master(future_offsets=(), option_offsets=(37,))
    with pytest.raises(ValueError, match="no live"):
        ins.resolve(master, instrument_config, asof=ASOF)


def test_unknown_underlying_raises(master, instrument_config) -> None:
    config = copy.deepcopy(instrument_config)
    config["underlying"] = "SENSEX"
    with pytest.raises(ValueError, match="no SENSEX rows"):
        ins.resolve(master, config, asof=ASOF)


def test_other_underlyings_segments_and_exchanges_are_excluded(
    master, instrument_config
) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    ids = {f.security_id for f in universe.all_futures}
    assert not (ids & {99_001, 99_002, 99_003})


# --- unit conversion ----------------------------------------------------------


def test_tick_size_is_converted_from_paise_to_rupees(master, instrument_config) -> None:
    """The master quotes ticks in paise. Off by 100x here and the breakeven
    calculation -- the whole viability finding -- is off by 100x too."""
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.front_future.tick_size == pytest.approx(FUT_TICK)
    raw = master.loc[master.INSTRUMENT == "FUTIDX", "TICK_SIZE"].iloc[0]
    assert raw == pytest.approx(FUT_TICK * ins.PAISE_PER_RUPEE)
    assert all(o.tick_size == pytest.approx(OPT_TICK) for o in universe.options)


def test_lot_size_and_freeze_quantity_are_read_from_the_master(
    master, instrument_config
) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.front_future.lot_size == LOT_SIZE
    assert universe.front_future.freeze_qty == FREEZE_QTY


def test_missing_freeze_qty_degrades_to_zero(instrument_config) -> None:
    master = build_master()
    master["SM_FREEZE_QTY"] = float("nan")
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.front_future.freeze_qty == 0


# --- spot bootstrap -----------------------------------------------------------


def test_estimate_spot_is_exact_for_pure_intrinsic_bands() -> None:
    master = build_master(spot=SPOT, extrinsic=0.0)
    options = master[master.INSTRUMENT == "OPTIDX"]
    assert ins.estimate_spot(options) == pytest.approx(SPOT)


def test_estimate_spot_survives_extrinsic_value() -> None:
    """Time value inflates the deep-ITM call estimate and deflates the deep-ITM
    put estimate by the same amount, so the median across both cancels it.

    That cancellation is why the estimator uses calls *and* puts rather than
    whichever side is convenient.
    """
    master = build_master(spot=SPOT, extrinsic=25.0)
    options = master[master.INSTRUMENT == "OPTIDX"]
    estimate = ins.estimate_spot(options)
    assert estimate == pytest.approx(SPOT, abs=1.0)


def test_estimate_spot_is_accurate_enough_to_pick_the_atm_strike() -> None:
    """The only requirement on this bootstrap: land within half a strike step."""
    for extrinsic in (0.0, 10.0, 40.0, 100.0):
        master = build_master(spot=SPOT, extrinsic=extrinsic)
        options = master[master.INSTRUMENT == "OPTIDX"]
        assert abs(ins.estimate_spot(options) - SPOT) < 50.0


def test_estimate_spot_returns_none_without_band_columns() -> None:
    options = build_master()
    options = options.drop(columns=["SM_UPPER_LIMIT"])
    assert ins.estimate_spot(options) is None


def test_estimate_spot_returns_none_on_empty_chain() -> None:
    empty = build_master().iloc[0:0]
    assert ins.estimate_spot(empty) is None


def test_estimate_spot_uses_only_the_nearest_expiry() -> None:
    """Regression: pooling expiries biased the real 2026-08-23 estimate 345
    points high, because each expiry's bands reference that expiry's forward
    and long-dated bands carry thousands of points of time value.

    Here the far expiry is given a deliberately corrupt band. A correct
    estimator never looks at it.
    """
    near = build_master(spot=SPOT, option_offsets=(30,), future_offsets=(30,), noise=False)
    far = build_master(spot=SPOT + 5000.0, option_offsets=(180,), future_offsets=(180,), noise=False)
    combined = pd.concat([near, far], ignore_index=True)
    options = combined[combined.INSTRUMENT == "OPTIDX"]

    detail = ins.estimate_spot_detail(options)
    assert detail is not None
    assert detail.expiry == ASOF + timedelta(days=30)
    assert detail.spot == pytest.approx(SPOT)


def test_estimate_spot_reports_call_and_put_estimates_separately() -> None:
    """Calls and puts are computed from opposite ends of the chain, so their
    agreement is a free consistency check on the whole method."""
    master = build_master(spot=SPOT, option_offsets=(30,), extrinsic=0.0)
    options = master[master.INSTRUMENT == "OPTIDX"]
    detail = ins.estimate_spot_detail(options)
    assert detail is not None
    assert detail.call_estimate == pytest.approx(SPOT)
    assert detail.put_estimate == pytest.approx(SPOT)
    assert detail.disagreement == pytest.approx(0.0)
    assert detail.n_contracts == 10  # depth=5 per side


def test_estimate_spot_warns_when_calls_and_puts_disagree(caplog) -> None:
    """A large disagreement means bands have stopped tracking intrinsic value.
    That must be visible, not silently averaged away."""
    master = build_master(spot=SPOT, option_offsets=(30,), noise=False)
    options = master[master.INSTRUMENT == "OPTIDX"].copy()
    # Inflate the put bands only, breaking the symmetry the median relies on.
    is_put = options.OPTION_TYPE == "PE"
    options.loc[is_put, "SM_UPPER_LIMIT"] += 2000.0
    options.loc[is_put, "SM_LOWER_LIMIT"] += 2000.0

    with caplog.at_level("WARNING"):
        detail = ins.estimate_spot_detail(options)
    assert detail is not None
    assert detail.disagreement == pytest.approx(2000.0, abs=1.0)
    assert "disagree" in caplog.text


def test_estimate_spot_depth_is_configurable() -> None:
    master = build_master(spot=SPOT, option_offsets=(30,), noise=False)
    options = master[master.INSTRUMENT == "OPTIDX"]
    assert ins.estimate_spot_detail(options, depth=3).n_contracts == 6
    assert ins.estimate_spot_detail(options, depth=8).n_contracts == 16


def test_spot_detail_is_attached_to_the_universe(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.spot_detail is not None
    assert universe.spot_detail.spot == universe.spot_estimate
    assert "disagree" in universe.spot_detail.describe()


def test_universe_without_options_still_resolves(instrument_config) -> None:
    """Futures-only resolution must work, since the spot bootstrap depends on
    the option chain and a futures-only master has no bands to read."""
    master = build_master(option_offsets=())
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.options == ()
    assert universe.atm_strike is None
    assert universe.spot_estimate is None


# --- option chain -------------------------------------------------------------


def test_atm_strike_is_the_nearest_listed_strike(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.atm_strike == 57800.0  # spot 57,785 on a 100-point grid


def test_chain_is_a_symmetric_band_around_atm(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    band = instrument_config["options"]["strike_band"]
    strikes = sorted({o.strike for o in universe.options})
    assert len(strikes) == 2 * band + 1
    assert strikes[0] == 57800.0 - band * 100
    assert strikes[-1] == 57800.0 + band * 100
    assert len(universe.options) == (2 * band + 1) * 2  # CE and PE per strike
    assert {o.option_type for o in universe.options} == {"CE", "PE"}


def test_chain_skips_an_expiry_that_is_too_close(master, instrument_config) -> None:
    """Option expiries are 2 and 37 days out; min_days_to_expiry is 3."""
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert {o.days_to_expiry(ASOF) for o in universe.options} == {37}


def test_chain_takes_the_nearest_expiry_when_the_guard_is_disabled(
    master, instrument_config
) -> None:
    config = copy.deepcopy(instrument_config)
    config["options"]["min_days_to_expiry"] = 0
    universe = ins.resolve(master, config, asof=ASOF)
    assert {o.days_to_expiry(ASOF) for o in universe.options} == {2}


def test_option_type_filter_is_honoured(master, instrument_config) -> None:
    config = copy.deepcopy(instrument_config)
    config["options"]["option_types"] = ["CE"]
    universe = ins.resolve(master, config, asof=ASOF)
    assert {o.option_type for o in universe.options} == {"CE"}


def test_futures_carry_no_strike_or_option_type(master, instrument_config) -> None:
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.front_future.strike is None
    assert universe.front_future.option_type is None


# --- feed plumbing ------------------------------------------------------------


def test_feed_key_matches_the_dhan_websocket_convention(master, instrument_config) -> None:
    """Dhan expects ``(exchange_segment_code, security_id_as_string)``; the
    string is not incidental, the SDK compares it as text."""
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    segment, security_id = universe.front_future.feed_key
    assert segment == ins.SEGMENT_CODE["NSE_FNO"] == 2
    assert security_id == str(universe.front_future.security_id)
    assert isinstance(security_id, str)


def test_feed_keys_lead_with_the_front_future(master, instrument_config) -> None:
    """Subscription order matters: the depth feed caps concurrent instruments,
    so the future must be first in line if the list is ever truncated."""
    universe = ins.resolve(master, instrument_config, asof=ASOF)
    assert universe.feed_keys[0] == universe.front_future.feed_key
    assert len(universe.feed_keys) == 1 + len(universe.options)
    assert len(set(universe.feed_keys)) == len(universe.feed_keys)


def test_days_to_expiry() -> None:
    contract = ins.Contract(
        security_id=1,
        display_name="X",
        instrument="FUTIDX",
        expiry=ASOF + timedelta(days=10),
        lot_size=30,
        tick_size=0.2,
        freeze_qty=601,
    )
    assert contract.days_to_expiry(ASOF) == 10
    assert contract.days_to_expiry(ASOF + timedelta(days=10)) == 0
    assert contract.days_to_expiry(ASOF + timedelta(days=11)) == -1


def test_summary_reports_the_spec_fields_a_reader_needs(master, instrument_config) -> None:
    text = ins.resolve(master, instrument_config, asof=ASOF).summary()
    for expected in ("spot estimate", "front future", "lot size", "futures tick", "ATM strike"):
        assert expected in text
    assert "57,785" in text
    assert "Rs 0.20" in text


# --- master download ----------------------------------------------------------


@pytest.fixture
def download_config(tmp_path, monkeypatch, instrument_config) -> dict:
    monkeypatch.setattr(ins, "_REPO_ROOT", tmp_path)
    config = copy.deepcopy(instrument_config)
    config["instrument_master"]["local_path"] = "data/reference/master.csv"
    return config


def _fake_get(payload: bytes, calls: list):
    class Response:
        content = payload

        def raise_for_status(self) -> None:
            return None

    def get(url, timeout=None):
        calls.append(url)
        return Response()

    return get


def test_fresh_master_is_not_redownloaded(download_config, tmp_path, monkeypatch) -> None:
    path = tmp_path / "data" / "reference" / "master.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached")
    calls: list = []
    monkeypatch.setattr("requests.get", _fake_get(b"new", calls))

    assert ins.ensure_master(download_config) == path
    assert calls == []
    assert path.read_bytes() == b"cached"


def test_stale_master_is_redownloaded(download_config, tmp_path, monkeypatch) -> None:
    import os
    import time

    path = tmp_path / "data" / "reference" / "master.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stale")
    old = time.time() - 3600 * 48
    os.utime(path, (old, old))

    calls: list = []
    monkeypatch.setattr("requests.get", _fake_get(b"fresh", calls))
    ins.ensure_master(download_config)
    assert len(calls) == 1
    assert path.read_bytes() == b"fresh"


def test_force_redownloads_a_fresh_master(download_config, tmp_path, monkeypatch) -> None:
    path = tmp_path / "data" / "reference" / "master.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached")
    calls: list = []
    monkeypatch.setattr("requests.get", _fake_get(b"forced", calls))
    ins.ensure_master(download_config, force=True)
    assert len(calls) == 1
    assert path.read_bytes() == b"forced"


def test_failed_download_leaves_no_partial_master(
    download_config, tmp_path, monkeypatch
) -> None:
    """A truncated master would parse fine and resolve the wrong contracts, so
    the download must be atomic rather than streamed into place."""

    def exploding_get(url, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("requests.get", exploding_get)
    with pytest.raises(RuntimeError):
        ins.ensure_master(download_config)
    assert not (tmp_path / "data" / "reference" / "master.csv").exists()
    assert not list((tmp_path / "data" / "reference").glob("*.tmp"))


def test_round_trip_through_csv(download_config, tmp_path, monkeypatch, instrument_config) -> None:
    """read_master must recover the same universe the in-memory frame gives,
    i.e. dtypes survive the CSV boundary."""
    frame = build_master()
    payload = frame.to_csv(index=False).encode()
    calls: list = []
    monkeypatch.setattr("requests.get", _fake_get(payload, calls))

    path = ins.ensure_master(download_config)
    from_disk = ins.resolve(ins.read_master(path), instrument_config, asof=ASOF)
    in_memory = ins.resolve(frame, instrument_config, asof=ASOF)
    assert from_disk.front_future == in_memory.front_future
    assert from_disk.atm_strike == in_memory.atm_strike
    assert from_disk.options == in_memory.options
    assert from_disk.spot_estimate == pytest.approx(in_memory.spot_estimate)
