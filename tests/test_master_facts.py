"""BANKNIFTY contract facts, asserted against the real instrument master.

These are the Phase 0 findings pinned as executable claims. Every one of them
was read from the exchange master on 2026-08-23, not assumed from memory or
from a blog post:

  * BANKNIFTY has **no weekly options** -- withdrawn by NSE, so the brief's
    "weekly and monthly may list simultaneously" concern is moot.
  * Expiry day is **Tuesday**, not the Thursday most references still state.
  * Lot size **30**; futures tick **Rs 0.20**; options tick **Rs 0.05**.
  * ``TICK_SIZE`` in the master is denominated in **paise**.

A FAILURE HERE IS NOT NECESSARILY A BUG. NSE revises these by circular. A
failure means the finding has expired and the affected DECISIONS.md entry plus
any published number needs re-deriving -- which is exactly why they are tests
and not prose.

Skipped when the master has not been downloaded (it is 36 MB and gitignored):

    python scripts/refresh_master.py
"""

from __future__ import annotations

import pandas as pd
import pytest

from bnfmm.data import instruments as ins

from .conftest import REPO_ROOT

MASTER_PATH = REPO_ROOT / "data" / "reference" / "api-scrip-master-detailed.csv"

pytestmark = [
    pytest.mark.master,
    pytest.mark.skipif(
        not MASTER_PATH.exists(),
        reason="instrument master not downloaded; run scripts/refresh_master.py",
    ),
]

_USECOLS = [
    "EXCH_ID",
    "SEGMENT",
    "SECURITY_ID",
    "INSTRUMENT",
    "UNDERLYING_SYMBOL",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "SM_EXPIRY_DATE",
    "STRIKE_PRICE",
    "OPTION_TYPE",
    "TICK_SIZE",
    "EXPIRY_FLAG",
    "SM_UPPER_LIMIT",
    "SM_LOWER_LIMIT",
    "SM_FREEZE_QTY",
]


@pytest.fixture(scope="module")
def master() -> pd.DataFrame:
    return pd.read_csv(MASTER_PATH, usecols=_USECOLS, low_memory=False)


@pytest.fixture(scope="module")
def banknifty(master: pd.DataFrame) -> pd.DataFrame:
    df = master[
        (master.EXCH_ID == "NSE")
        & (master.SEGMENT == "D")
        & (master.UNDERLYING_SYMBOL.astype(str).str.upper() == "BANKNIFTY")
    ].copy()
    df["_expiry"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")
    return df


def test_master_is_a_plausible_size(master: pd.DataFrame) -> None:
    assert len(master) > 100_000, "truncated download?"


def test_banknifty_has_no_weekly_options(banknifty: pd.DataFrame) -> None:
    """EXPIRY_FLAG: M = monthly, W = weekly. A W row here would invalidate
    `options.expiry: nearest_monthly` in config/instruments.yaml."""
    options = banknifty[banknifty.INSTRUMENT == "OPTIDX"]
    flags = set(options.EXPIRY_FLAG.dropna().astype(str).str.upper().unique())
    assert "W" not in flags, f"weekly BANKNIFTY options have relisted: {flags}"
    assert "M" in flags


def test_banknifty_expiry_day_is_tuesday(banknifty: pd.DataFrame) -> None:
    live = banknifty[banknifty["_expiry"].notna()]
    weekdays = set(live["_expiry"].dt.dayofweek.unique())
    assert weekdays == {1}, "expiry day changed (0=Mon .. 6=Sun)"


def test_futures_lot_size_and_tick(banknifty: pd.DataFrame) -> None:
    futures = banknifty[banknifty.INSTRUMENT == "FUTIDX"]
    assert set(futures.LOT_SIZE.unique()) == {30}
    assert set(futures.TICK_SIZE.unique()) == {20.0}, "paise, i.e. Rs 0.20"


def test_options_tick_is_finer_than_futures(banknifty: pd.DataFrame) -> None:
    options = banknifty[banknifty.INSTRUMENT == "OPTIDX"]
    assert set(options.TICK_SIZE.unique()) == {5.0}, "paise, i.e. Rs 0.05"


def test_tick_size_is_denominated_in_paise(master: pd.DataFrame) -> None:
    """Cross-check against a published value: NSE quotes index options in Rs
    0.05 increments, and the master reports 5.0 for them. Rupees would be an
    absurd Rs 5 tick on a Rs 1 option."""
    nifty_options = master[
        (master.UNDERLYING_SYMBOL.astype(str).str.upper() == "NIFTY")
        & (master.INSTRUMENT == "OPTIDX")
    ]
    assert set(nifty_options.TICK_SIZE.unique()) == {5.0}
    assert ins.PAISE_PER_RUPEE == 100.0


def test_freeze_quantity_caps_quote_size(banknifty: pd.DataFrame) -> None:
    """The exchange rejects single orders above this quantity, so it is a hard
    ceiling on quote size -- relevant to the strategy layer, not cosmetic."""
    futures = banknifty[banknifty.INSTRUMENT == "FUTIDX"]
    freeze = set(futures.SM_FREEZE_QTY.dropna().astype(int).unique())
    assert freeze == {601}
    assert 601 > 30, "freeze qty is in units, not lots"


def test_resolution_against_the_real_master(instrument_config) -> None:
    """End-to-end: the shipped config resolves a coherent universe from the
    real file, with no network access."""
    universe = ins.resolve(ins.read_master(MASTER_PATH), instrument_config)
    assert universe.front_future.instrument == "FUTIDX"
    assert universe.front_future.lot_size == 30
    assert universe.front_future.tick_size == pytest.approx(0.20)
    assert universe.front_future.days_to_expiry() > 3
    assert universe.spot_estimate is not None
    # A sanity band, deliberately wide: this asserts the estimator is not
    # broken, not that the index sits at any particular level.
    assert 20_000 < universe.spot_estimate < 200_000
    assert universe.atm_strike is not None
    assert abs(universe.atm_strike - universe.spot_estimate) <= 250
    band = instrument_config["options"]["strike_band"]
    assert len(universe.options) == (2 * band + 1) * 2


def test_spot_bootstrap_agrees_across_calls_and_puts(instrument_config) -> None:
    """The real check on the spot estimator, on real data.

    Calls and puts are derived from opposite ends of the chain with no shared
    inputs. On the 2026-08-23 master they agree to ~5 points on a ~57,800 index
    -- 0.01%, and well inside the 100-point strike step the estimate is used to
    resolve. Pooling expiries instead of restricting to the nearest one moved
    this to 345 points, which is what the restriction exists to prevent.
    """
    universe = ins.resolve(ins.read_master(MASTER_PATH), instrument_config)
    detail = universe.spot_detail
    assert detail is not None
    assert detail.expiry is not None
    assert detail.disagreement is not None
    assert detail.disagreement < 0.001 * detail.spot, detail.describe()
    # The estimate must be good enough to pick the right strike on a 100-point
    # grid, which is the only thing it is used for.
    assert abs(detail.spot - universe.atm_strike) <= 50.0
