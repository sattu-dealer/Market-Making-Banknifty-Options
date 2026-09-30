"""Resolve BANKNIFTY contracts from the exchange instrument master.

Nothing about BANKNIFTY's contract spec is hardcoded here. NSE has repeatedly
changed its expiry day, weekly availability, lot size and tick size, so every
one of those is read from the master CSV at runtime. A constant would be a bug
with a delayed fuse.

The master is a public, no-auth CSV, so this module works before any broker
account exists.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "instruments.yaml"

# Tick sizes in the master are quoted in paise. Verified by cross-checking index
# options, which the master reports as 5.0 against NSE's published Rs 0.05.
PAISE_PER_RUPEE = 100.0

# Dhan exchange-segment codes, as used by the depth and quote WebSockets.
# ``IDX_I`` is 0, which is falsy -- so anything testing a segment code for truth
# rather than for None will silently mis-handle indices.
SEGMENT_CODE = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2}


@dataclass(frozen=True)
class Contract:
    """One tradeable contract, with spec fields normalised to rupees."""

    security_id: int
    display_name: str
    instrument: str  # FUTIDX | OPTIDX
    expiry: date
    lot_size: int
    tick_size: float  # rupees, converted from the master's paise
    freeze_qty: int  # exchange max order quantity -- caps quote size
    strike: float | None = None
    option_type: str | None = None  # CE | PE | None for futures
    exchange_segment: str = "NSE_FNO"

    @property
    def segment_code(self) -> int:
        return SEGMENT_CODE[self.exchange_segment]

    @property
    def feed_key(self) -> tuple[int, str]:
        """``(segment_code, security_id)`` as the Dhan WebSockets expect it."""
        return (self.segment_code, str(self.security_id))

    def days_to_expiry(self, asof: date | None = None) -> int:
        return (self.expiry - (asof or date.today())).days


@dataclass(frozen=True)
class SpotEstimate:
    """Underlying level inferred from option circuit bands, with diagnostics.

    The call-side and put-side estimates are computed independently from
    opposite ends of the chain, so their disagreement is a free internal
    consistency check: the two have no reason to agree unless the underlying
    assumption (band mid ~ intrinsic value) actually holds.
    """

    spot: float
    call_estimate: float | None
    put_estimate: float | None
    expiry: date | None
    n_contracts: int

    @property
    def disagreement(self) -> float | None:
        if self.call_estimate is None or self.put_estimate is None:
            return None
        return abs(self.call_estimate - self.put_estimate)

    def describe(self) -> str:
        parts = [f"{self.spot:,.2f}"]
        if self.disagreement is not None:
            parts.append(
                f"(calls {self.call_estimate:,.2f} / puts {self.put_estimate:,.2f}, "
                f"disagree {self.disagreement:,.2f})"
            )
        if self.expiry is not None:
            parts.append(f"from {self.expiry} chain, n={self.n_contracts}")
        return " ".join(parts)


@dataclass(frozen=True)
class BankniftyUniverse:
    """The resolved instrument set for one session."""

    asof: date
    spot_estimate: float | None
    front_future: Contract
    all_futures: tuple[Contract, ...]
    options: tuple[Contract, ...]
    atm_strike: float | None
    spot_detail: SpotEstimate | None = None

    @property
    def feed_keys(self) -> list[tuple[int, str]]:
        """Subscription list for the depth feed, front future first."""
        return [self.front_future.feed_key] + [o.feed_key for o in self.options]

    def summary(self) -> str:
        lines = [
            f"BANKNIFTY universe as of {self.asof}",
            f"  spot estimate      : {self.spot_detail.describe()}"
            if self.spot_detail
            else (
                f"  spot estimate      : {self.spot_estimate:,.2f}"
                if self.spot_estimate
                else "  spot estimate      : unavailable"
            ),
            f"  front future       : {self.front_future.display_name} "
            f"(id={self.front_future.security_id}, expiry={self.front_future.expiry}, "
            f"{self.front_future.days_to_expiry(self.asof)}d)",
            f"  lot size           : {self.front_future.lot_size}",
            f"  futures tick       : Rs {self.front_future.tick_size:.2f}",
            f"  freeze qty         : {self.front_future.freeze_qty}",
            f"  ATM strike         : {self.atm_strike}",
            f"  options subscribed : {len(self.options)}",
        ]
        return "\n".join(lines)


def load_config(path: Path | str = _DEFAULT_CONFIG) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def ensure_master(config: dict[str, Any], force: bool = False) -> Path:
    """Download the instrument master if absent or stale. Returns its path."""
    import requests

    spec = config["instrument_master"]
    path = _REPO_ROOT / spec["local_path"]
    max_age = float(spec.get("max_age_hours", 24)) * 3600

    if not force and path.exists() and (time.time() - path.stat().st_mtime) < max_age:
        logger.info("instrument master is fresh: %s", path)
        return path

    logger.info("downloading instrument master from %s", spec["url"])
    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(spec["url"], timeout=180)
    response.raise_for_status()
    # Write via a temp file so an interrupted download can't leave a truncated
    # master in place, which would silently resolve the wrong contracts.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(path)
    logger.info("wrote %s (%.1f MB)", path, len(response.content) / 1e6)
    return path


def read_master(path: Path | str) -> pd.DataFrame:
    """Load the master CSV, restricted to nothing -- callers filter."""
    return pd.read_csv(path, low_memory=False)


def _to_contract(row: pd.Series) -> Contract:
    strike = row.get("STRIKE_PRICE")
    option_type = row.get("OPTION_TYPE")
    is_option = str(row["INSTRUMENT"]).startswith("OPT")
    return Contract(
        security_id=int(row["SECURITY_ID"]),
        display_name=str(row["DISPLAY_NAME"]).strip(),
        instrument=str(row["INSTRUMENT"]),
        expiry=pd.to_datetime(row["SM_EXPIRY_DATE"]).date(),
        lot_size=int(float(row["LOT_SIZE"])),
        tick_size=float(row["TICK_SIZE"]) / PAISE_PER_RUPEE,
        freeze_qty=int(float(row["SM_FREEZE_QTY"])) if pd.notna(row.get("SM_FREEZE_QTY")) else 0,
        strike=float(strike) if is_option and pd.notna(strike) else None,
        option_type=str(option_type).strip() if is_option and pd.notna(option_type) else None,
    )


def estimate_spot_detail(
    options: pd.DataFrame,
    depth: int = 5,
    max_disagreement_frac: float = 0.005,
) -> SpotEstimate | None:
    """Infer the underlying level from deep-ITM option circuit bands.

    The master carries per-contract upper and lower price bands. For a deeply
    in-the-money option the price is almost entirely intrinsic, so
    ``mid(band) ~ |spot - strike|`` and hence ``spot ~ strike + mid`` for calls,
    ``spot ~ strike - mid`` for puts. Taking the median over the most deeply ITM
    contracts recovers spot to within a few points.

    Two restrictions matter, and getting either wrong quietly biases the answer
    by hundreds of points rather than raising:

    1. **One expiry only** -- the nearest one. Each expiry's option prices
       reference *that expiry's forward*, not spot, and long-dated contracts
       carry thousands of points of time value in their bands. Pooling across
       expiries mixes forwards and adds carry: on the 2026-08-23 master it
       shifted the estimate 345 points high.
    2. **Deeply ITM only.** Near the money the band is mostly extrinsic and the
       identity above does not hold at all.

    This exists so Phase 0 can locate the ATM strike without a live quote --
    i.e. before any broker account exists. It is a bootstrap, not a price
    source; once the feed is connected, use the feed. Its only requirement is
    landing within half a strike step.

    Returns ``None`` when the required columns or contracts are missing.
    """
    needed = {"SM_UPPER_LIMIT", "SM_LOWER_LIMIT", "STRIKE_PRICE", "OPTION_TYPE"}
    if not needed.issubset(options.columns):
        return None

    df = options.dropna(subset=list(needed)).copy()
    if df.empty:
        return None

    # Restrict to the nearest expiry. Done here rather than in the caller so a
    # future caller cannot reintroduce the cross-expiry bias.
    expiry: date | None = None
    if "SM_EXPIRY_DATE" in df.columns:
        parsed = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")
        if parsed.notna().any():
            expiry = parsed.min().date()
            df = df[parsed.dt.date == expiry]
    if df.empty:
        return None

    df["band_mid"] = (df.SM_UPPER_LIMIT.astype(float) + df.SM_LOWER_LIMIT.astype(float)) / 2.0
    opt_type = df.OPTION_TYPE.astype(str).str.upper()
    calls = df[opt_type == "CE"]
    puts = df[opt_type == "PE"]

    call_est: float | None = None
    put_est: float | None = None
    estimates: list[float] = []

    if not calls.empty:
        deep = calls.nsmallest(depth, "STRIKE_PRICE")
        vals = (deep.STRIKE_PRICE.astype(float) + deep.band_mid).tolist()
        estimates.extend(vals)
        call_est = float(pd.Series(vals).median())
    if not puts.empty:
        deep = puts.nlargest(depth, "STRIKE_PRICE")
        vals = (deep.STRIKE_PRICE.astype(float) - deep.band_mid).tolist()
        estimates.extend(vals)
        put_est = float(pd.Series(vals).median())

    if not estimates:
        return None

    spot = float(pd.Series(estimates).median())
    result = SpotEstimate(
        spot=spot,
        call_estimate=call_est,
        put_estimate=put_est,
        expiry=expiry,
        n_contracts=len(estimates),
    )
    # A large call/put disagreement means the band-mid-is-intrinsic assumption
    # has stopped holding. Warn rather than fail: the estimate is only used to
    # pick an ATM strike, and a wrong warning is cheaper than a hard stop.
    if result.disagreement is not None and result.disagreement > max_disagreement_frac * spot:
        logger.warning(
            "spot estimate: calls (%.2f) and puts (%.2f) disagree by %.2f -- "
            "circuit bands may no longer track intrinsic value",
            call_est,
            put_est,
            result.disagreement,
        )
    return result


def estimate_spot(options: pd.DataFrame, **kwargs: Any) -> float | None:
    """``estimate_spot_detail`` reduced to a single number, or None."""
    detail = estimate_spot_detail(options, **kwargs)
    return None if detail is None else detail.spot


def resolve(
    master: pd.DataFrame,
    config: dict[str, Any],
    asof: date | None = None,
) -> BankniftyUniverse:
    """Resolve the BANKNIFTY futures contract and option chain to subscribe to.

    Raises
    ------
    ValueError
        If no futures contract survives the roll filter, which means the master
        is stale or the underlying symbol changed.
    """
    asof = asof or date.today()
    underlying = str(config["underlying"]).upper()

    scope = master[
        (master.EXCH_ID == config["exchange"])
        & (master.SEGMENT == config["segment"])
        & (master.UNDERLYING_SYMBOL.astype(str).str.upper() == underlying)
    ].copy()
    if scope.empty:
        raise ValueError(f"no {underlying} rows in instrument master")

    # Bracket access throughout for this column: a leading underscore collides
    # with pandas' internal attribute namespace, so `scope._expiry` is a bug
    # waiting on a pandas upgrade.
    scope["_expiry"] = pd.to_datetime(scope["SM_EXPIRY_DATE"], errors="coerce")
    scope = scope.dropna(subset=["_expiry"])
    # Expired rows persist in the master; drop them or the "front" contract will
    # be one that stopped trading months ago.
    scope = scope[scope["_expiry"].dt.date >= asof]

    futures_df = scope[scope.INSTRUMENT == "FUTIDX"].sort_values("_expiry")
    if futures_df.empty:
        raise ValueError(f"no live {underlying} futures in master (stale download?)")

    futures = tuple(_to_contract(r) for _, r in futures_df.iterrows())

    fut_cfg = config.get("futures", {})
    roll_days = int(fut_cfg.get("roll_days_before_expiry", 0))
    # Skip contracts inside the roll window: the front contract's liquidity
    # migrates to the next one before expiry, and quoting into that decay would
    # contaminate every microstructure statistic downstream.
    eligible = [f for f in futures if f.days_to_expiry(asof) > roll_days]
    if not eligible:
        raise ValueError(
            f"all {underlying} futures within {roll_days}d roll window of {asof}"
        )
    front = eligible[0] if fut_cfg.get("contract", "front") == "front" else eligible[
        min(1, len(eligible) - 1)
    ]

    # --- option chain -----------------------------------------------------
    opt_cfg = config.get("options", {})
    options_df = scope[scope.INSTRUMENT == "OPTIDX"]
    spot_detail = estimate_spot_detail(options_df)
    spot = spot_detail.spot if spot_detail else None
    atm_strike: float | None = None
    chosen: list[Contract] = []

    if not options_df.empty and spot is not None:
        expiries = sorted(options_df["_expiry"].dt.date.unique())
        min_dte = int(opt_cfg.get("min_days_to_expiry", 0))
        # Near expiry the chain enters a pin-risk regime that is not
        # representative of normal quoting, so step to the next cycle.
        usable = [e for e in expiries if (e - asof).days >= min_dte]
        if usable:
            expiry = usable[0]
            chain = options_df[options_df["_expiry"].dt.date == expiry]
            strikes = sorted(chain.STRIKE_PRICE.dropna().astype(float).unique())
            if strikes:
                atm_strike = min(strikes, key=lambda k: abs(k - spot))
                atm_idx = strikes.index(atm_strike)
                band = int(opt_cfg.get("strike_band", 3))
                lo, hi = max(0, atm_idx - band), min(len(strikes), atm_idx + band + 1)
                wanted_strikes = set(strikes[lo:hi])
                wanted_types = {t.upper() for t in opt_cfg.get("option_types", ["CE", "PE"])}
                selected = chain[
                    chain.STRIKE_PRICE.astype(float).isin(wanted_strikes)
                    & chain.OPTION_TYPE.astype(str).str.upper().isin(wanted_types)
                ]
                chosen = [_to_contract(r) for _, r in selected.iterrows()]
                chosen.sort(key=lambda c: (c.strike or 0.0, c.option_type or ""))

    return BankniftyUniverse(
        asof=asof,
        spot_estimate=spot,
        front_future=front,
        all_futures=futures,
        options=tuple(chosen),
        atm_strike=atm_strike,
        spot_detail=spot_detail,
    )


def resolve_from_disk(
    config_path: Path | str = _DEFAULT_CONFIG,
    asof: date | None = None,
    refresh: bool = True,
) -> BankniftyUniverse:
    """Convenience path: load config, ensure the master, resolve."""
    config = load_config(config_path)
    path = ensure_master(config, force=False) if refresh else (
        _REPO_ROOT / config["instrument_master"]["local_path"]
    )
    return resolve(read_master(path), config, asof=asof)
