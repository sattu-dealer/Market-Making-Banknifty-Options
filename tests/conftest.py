"""Shared fixtures.

The BANKNIFTY contract facts below (lot 30, futures tick Rs 0.20, options tick
Rs 0.05, spot ~57,785 on 2026-08-23) were read from the exchange instrument
master, not assumed. They are repeated here as fixture constants so the cost
tests exercise realistic magnitudes -- a cost model that is only tested at
price=100 will not reveal that STT dominates the tick value.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bnfmm.data import instruments as ins
from bnfmm.sim.costs import CostModel

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- BANKNIFTY spec, as resolved from the instrument master on 2026-08-23 ----
LOT_SIZE = 30
FUT_TICK = 0.20
OPT_TICK = 0.05
FUT_PRICE = 57785.0
OPT_PREMIUM = 1000.0  # representative nearest-monthly ATM premium
FREEZE_QTY = 601

ASOF = date(2026, 8, 23)

COSTS_PATH = REPO_ROOT / "config" / "costs.yaml"


@pytest.fixture(scope="session")
def costs() -> CostModel:
    """The shipped cost table, exchange-member case (zero brokerage)."""
    return CostModel.from_yaml(COSTS_PATH)


@pytest.fixture(scope="session")
def retail_costs() -> CostModel:
    """Dhan retail: flat Rs 20 per executed order on both F&O products."""
    return CostModel.for_profile("dhan", COSTS_PATH)


@pytest.fixture(scope="session")
def zerodha_costs() -> CostModel:
    """Comparison profile only. The one schedule here with a real pct cap."""
    return CostModel.for_profile("zerodha", COSTS_PATH)


@pytest.fixture(scope="session")
def instrument_config() -> dict:
    return ins.load_config(REPO_ROOT / "config" / "instruments.yaml")


# --- synthetic instrument master ---------------------------------------------
# Built by hand rather than sampled from the real CSV: the real file is 36 MB,
# gitignored, and goes stale daily, so tests that depend on it cannot be
# regression guards. Tests against the real master live in test_master_facts.py
# and skip when it is absent.

_MASTER_COLUMNS = [
    "EXCH_ID",
    "SEGMENT",
    "UNDERLYING_SYMBOL",
    "INSTRUMENT",
    "SM_EXPIRY_DATE",
    "SECURITY_ID",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "TICK_SIZE",
    "SM_FREEZE_QTY",
    "STRIKE_PRICE",
    "OPTION_TYPE",
    "SM_UPPER_LIMIT",
    "SM_LOWER_LIMIT",
]


def future_row(security_id: int, expiry: date, **overrides) -> dict:
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "UNDERLYING_SYMBOL": "BANKNIFTY",
        "INSTRUMENT": "FUTIDX",
        "SM_EXPIRY_DATE": expiry.isoformat(),
        "SECURITY_ID": security_id,
        "DISPLAY_NAME": f"BANKNIFTY {expiry:%b %Y} FUT",
        "LOT_SIZE": LOT_SIZE,
        "TICK_SIZE": FUT_TICK * 100,  # master quotes paise
        "SM_FREEZE_QTY": FREEZE_QTY,
        "STRIKE_PRICE": float("nan"),
        "OPTION_TYPE": float("nan"),
        "SM_UPPER_LIMIT": float("nan"),
        "SM_LOWER_LIMIT": float("nan"),
    }
    row.update(overrides)
    return row


def option_row(
    security_id: int,
    expiry: date,
    strike: float,
    option_type: str,
    spot: float,
    extrinsic: float = 0.0,
    **overrides,
) -> dict:
    """One option row whose circuit band is centred on its intrinsic value.

    ``estimate_spot`` infers the underlying from ``mid(band) ~ |spot - strike|``
    on deep-ITM contracts, so the band is what the test is really specifying.
    ``extrinsic`` lets a test add time value and check the estimator degrades
    gracefully rather than exactly.
    """
    intrinsic = (spot - strike) if option_type == "CE" else (strike - spot)
    band_mid = max(intrinsic, 0.0) + extrinsic
    half_width = 0.10 * max(band_mid, 1.0)
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "D",
        "UNDERLYING_SYMBOL": "BANKNIFTY",
        "INSTRUMENT": "OPTIDX",
        "SM_EXPIRY_DATE": expiry.isoformat(),
        "SECURITY_ID": security_id,
        "DISPLAY_NAME": f"BANKNIFTY {expiry:%b %Y} {strike:.0f} {option_type}",
        "LOT_SIZE": LOT_SIZE,
        "TICK_SIZE": OPT_TICK * 100,
        "SM_FREEZE_QTY": FREEZE_QTY,
        "STRIKE_PRICE": float(strike),
        "OPTION_TYPE": option_type,
        "SM_UPPER_LIMIT": band_mid + half_width,
        "SM_LOWER_LIMIT": band_mid - half_width,
    }
    row.update(overrides)
    return row


def index_row(security_id: int, display_name: str, **overrides) -> dict:
    """One segment-``I`` index row, with the master's real expiry sentinel.

    The live master stores ``0001-01-01`` for indices, which is outside the pandas
    nanosecond datetime range -- the reason ``universe.py`` builds index contracts
    by hand instead of through ``instruments._to_contract``. Reproducing the
    sentinel here is what makes that code path actually exercised rather than
    merely written.
    """
    row = {
        "EXCH_ID": "NSE",
        "SEGMENT": "I",
        "UNDERLYING_SYMBOL": display_name,
        "INSTRUMENT": "INDEX",
        "SM_EXPIRY_DATE": "0001-01-01",
        "SECURITY_ID": security_id,
        "DISPLAY_NAME": display_name,
        "LOT_SIZE": 0,
        "TICK_SIZE": 0.0,
        "SM_FREEZE_QTY": 0,
        "STRIKE_PRICE": float("nan"),
        "OPTION_TYPE": float("nan"),
        "SM_UPPER_LIMIT": float("nan"),
        "SM_LOWER_LIMIT": float("nan"),
    }
    row.update(overrides)
    return row


def build_master(
    *,
    spot: float = 57785.0,
    asof: date = ASOF,
    future_offsets: tuple[int, ...] = (2, 37, 65),
    option_offsets: tuple[int, ...] = (2, 37),
    strikes: tuple[float, ...] | None = None,
    extrinsic: float = 0.0,
    include_expired: bool = True,
    include_indices: bool = True,
    noise: bool = True,
) -> pd.DataFrame:
    """A miniature instrument master with a BANKNIFTY futures and option chain.

    ``future_offsets`` / ``option_offsets`` are days from ``asof``. Defaults
    mirror the real 2026-08-23 master: a front future 2 days out (inside the
    3-day roll window), then two further months.
    """
    if strikes is None:
        strikes = tuple(float(k) for k in range(57000, 58600, 100))

    rows: list[dict] = []
    sid = 10_000

    if include_expired:
        # Expired contracts persist in the real master indefinitely.
        rows.append(future_row(sid, asof - timedelta(days=30)))
        sid += 1

    if include_indices:
        # Segment I, so every derivative filter must reject them -- and the
        # capture universe must still be able to find them by security id.
        rows.append(index_row(25, "NIFTY BANK"))
        rows.append(index_row(13, "NIFTY 50"))

    for off in future_offsets:
        rows.append(future_row(sid, asof + timedelta(days=off)))
        sid += 1

    for off in option_offsets:
        expiry = asof + timedelta(days=off)
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                rows.append(
                    option_row(sid, expiry, strike, opt_type, spot, extrinsic=extrinsic)
                )
                sid += 1

    if noise:
        # Rows the filters must reject: wrong underlying, wrong segment, wrong
        # exchange. Without these, a filter bug looks like a passing test.
        rows.append(
            future_row(99_001, asof + timedelta(days=37), UNDERLYING_SYMBOL="NIFTY")
        )
        rows.append(future_row(99_002, asof + timedelta(days=37), SEGMENT="E"))
        rows.append(future_row(99_003, asof + timedelta(days=37), EXCH_ID="BSE"))

    return pd.DataFrame(rows, columns=_MASTER_COLUMNS)


@pytest.fixture
def master() -> pd.DataFrame:
    return build_master()
