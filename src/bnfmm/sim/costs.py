"""Indian F&O statutory cost stack, plus per-order brokerage.

A market maker's gross edge is a handful of ticks per round trip. On BANKNIFTY
futures one tick is Rs 6.00 per lot while STT alone is ~Rs 867 per round-trip
lot, so this module decides the *sign* of the strategy PnL, not its second
decimal place. It is therefore config-driven (``config/costs.yaml``) and tested
against hand-computed values.

Two distinctions in here are easy to get wrong and both change conclusions:

- **Statutory charges scale with quantity; brokerage does not.** Brokerage is
  per executed *order*. Use :class:`OrderCost` for anything the simulator does;
  :meth:`CostModel.fill_cost` charges per call and is for standalone arithmetic
  where one fill is one order.
- **Brokerage schedules differ by product and by broker.** Dhan charges a flat
  Rs 20 on both F&O products; a percentage cap applies at some brokers on some
  products and not others. Hence :class:`BrokerProfile` rather than a boolean.

Sign convention: every function returns a POSITIVE cost in rupees. Callers
subtract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml

Side = Literal["buy", "sell"]

_DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "costs.yaml"


class Product(str, Enum):
    """Which fee schedule applies. Options are charged on premium, futures on
    notional turnover -- the distinction dominates the economics."""

    FUTURES = "futures"
    OPTIONS = "options"


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised cost of a single fill, in rupees. All components are >= 0."""

    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    ipft: float = 0.0
    brokerage: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.stt
            + self.exchange_txn
            + self.sebi_fee
            + self.stamp_duty
            + self.ipft
            + self.brokerage
            + self.gst
        )

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        if not isinstance(other, CostBreakdown):
            return NotImplemented
        return CostBreakdown(
            stt=self.stt + other.stt,
            exchange_txn=self.exchange_txn + other.exchange_txn,
            sebi_fee=self.sebi_fee + other.sebi_fee,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            ipft=self.ipft + other.ipft,
            brokerage=self.brokerage + other.brokerage,
            gst=self.gst + other.gst,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "stt": self.stt,
            "exchange_txn": self.exchange_txn,
            "sebi_fee": self.sebi_fee,
            "stamp_duty": self.stamp_duty,
            "ipft": self.ipft,
            "brokerage": self.brokerage,
            "gst": self.gst,
            "total": self.total,
        }


# Components charged on the turnover/premium base, in the order they appear in
# a contract note. GST and brokerage are handled separately.
_TURNOVER_COMPONENTS = ("stt", "exchange_txn", "sebi_fee", "stamp_duty", "ipft")


@dataclass(frozen=True)
class BrokerageSpec:
    """One product's brokerage rule for one broker.

    ``flat`` is the per-order fee. ``pct``, when positive, caps it at a
    percentage of executed value -- the charge is ``min(flat, pct * value)``.
    Most Indian F&O schedules are genuinely flat, so ``pct`` defaults to zero
    and the cap is skipped entirely rather than silently evaluating to 0.
    """

    flat: float = 0.0
    pct: float = 0.0

    def charge(self, executed_value: float) -> float:
        if executed_value <= 0.0:
            return 0.0
        if self.pct <= 0.0:
            return self.flat
        return min(self.flat, self.pct * executed_value)


@dataclass(frozen=True)
class BrokerProfile:
    """A named broker's brokerage schedule across products."""

    name: str
    label: str
    specs: Mapping[str, BrokerageSpec]

    def spec(self, product: Product) -> BrokerageSpec:
        key = Product(product).value
        try:
            return self.specs[key]
        except KeyError:
            raise ValueError(
                f"broker profile {self.name!r} has no schedule for {key!r}"
            ) from None

    @classmethod
    def from_config(cls, name: str, cfg: Mapping[str, Any]) -> "BrokerProfile":
        specs = {}
        for product in Product:
            raw = cfg.get(product.value)
            if raw is None:
                continue
            specs[product.value] = BrokerageSpec(
                flat=float(raw.get("flat", 0.0)), pct=float(raw.get("pct", 0.0))
            )
        return cls(name=name, label=str(cfg.get("label", name)), specs=specs)


@dataclass
class CostModel:
    """Computes the statutory and brokerage cost of fills from a rate table.

    Parameters
    ----------
    rates:
        Parsed ``config/costs.yaml``.
    profile:
        Name of the broker profile in ``rates["brokerage"]["profiles"]``.
        Defaults to the config's ``default_profile`` (``member``, i.e. zero
        brokerage). Every reported result pairs ``member`` with ``dhan``; see
        :data:`reported_profiles`.
    """

    rates: dict[str, Any]
    profile: str | None = None
    broker: BrokerProfile = field(init=False)
    _gst_rate: float = field(init=False)
    _gst_applies_to: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        gst = self.rates.get("gst", {})
        self._gst_rate = float(gst.get("rate", 0.0))
        self._gst_applies_to = frozenset(gst.get("applies_to", ()))
        for product in Product:
            if product.value not in self.rates:
                raise ValueError(f"costs config missing schedule for {product.value!r}")

        brokerage = self.rates.get("brokerage", {})
        profiles = brokerage.get("profiles", {})
        name = self.profile or brokerage.get("default_profile")
        if not name:
            raise ValueError("costs config has no brokerage.default_profile")
        if name not in profiles:
            raise ValueError(
                f"unknown broker profile {name!r}; config has {sorted(profiles)}"
            )
        self.profile = name
        self.broker = BrokerProfile.from_config(name, profiles[name])

    @classmethod
    def from_yaml(cls, path: Path | str = _DEFAULT_CONFIG, **kwargs: Any) -> "CostModel":
        with open(path, encoding="utf-8") as fh:
            return cls(rates=yaml.safe_load(fh), **kwargs)

    @classmethod
    def for_profile(cls, name: str, path: Path | str = _DEFAULT_CONFIG) -> "CostModel":
        """The shipped rate table under one named broker profile."""
        return cls.from_yaml(path, profile=name)

    @staticmethod
    def reported_profiles(path: Path | str = _DEFAULT_CONFIG) -> tuple[str, ...]:
        """Profile names every published figure must be reported under.

        Read from config rather than hardcoded so that the paired-reporting
        decision has exactly one home. See ``DECISIONS.md`` #13.
        """
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        return tuple(cfg.get("brokerage", {}).get("reported_profiles", ()))

    @property
    def label(self) -> str:
        """Human-readable regime label, for table headers and captions."""
        return self.broker.label

    def brokerage(self, product: Product, executed_value: float) -> float:
        """Brokerage on one executed order of ``executed_value`` rupees.

        Note the argument is the value of the *order*, not of a fill: with a
        ``pct`` cap the charge depends on cumulative executed value, which is
        why :class:`OrderCost` recomputes rather than accumulates it.
        """
        return self.broker.spec(product).charge(executed_value)

    def _statutory_charges(
        self, product: Product, side: Side, base_value: float
    ) -> dict[str, float]:
        """The five turnover-based components. No brokerage, no GST."""
        schedule = self.rates[Product(product).value]
        charges: dict[str, float] = {}
        for name in _TURNOVER_COMPONENTS:
            spec = schedule.get(name)
            if spec is None:
                charges[name] = 0.0
                continue
            applies = spec.get("side", "both")
            charges[name] = (
                float(spec["rate"]) * base_value if applies in (side, "both") else 0.0
            )
        return charges

    def _gst_on(self, charges: Mapping[str, float]) -> float:
        """GST on the taxable subset only -- never on STT or stamp duty."""
        return self._gst_rate * sum(
            v for name, v in charges.items() if name in self._gst_applies_to
        )

    def fill_cost(
        self,
        product: Product,
        side: Side,
        price: float,
        quantity: float,
    ) -> CostBreakdown:
        """Cost of one fill, charging brokerage as though it were a whole order.

        Parameters
        ----------
        product:
            Futures or options -- selects the fee schedule.
        side:
            ``"buy"`` or ``"sell"``. Several charges are one-sided (STT on sell,
            stamp duty on buy), so this is not cosmetic.
        price:
            Futures price, or option *premium*. Per unit, not per lot.
        quantity:
            Number of units (lot_size * n_lots), not number of lots.

        Notes
        -----
        For both products the charge base is ``price * quantity``. For futures
        that is contract turnover; for options it is premium turnover. The
        distinction lives in the rate table, not here -- options rates are
        expressed against premium and futures rates against notional.

        **Brokerage is charged in full on every call**, which is correct only
        when one fill is one order. Simulated quoting must go through
        :class:`OrderCost` instead, or a quote filling in five partials will be
        charged five times over.
        """
        if quantity < 0:
            raise ValueError(f"quantity must be non-negative, got {quantity}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        base_value = float(price) * float(quantity)
        charges = self._statutory_charges(product, side, base_value)
        charges["brokerage"] = self.brokerage(product, base_value)
        charges["gst"] = self._gst_on(charges)
        return CostBreakdown(**charges)

    def order(self, product: Product, side: Side) -> "OrderCost":
        """An empty :class:`OrderCost` accumulator bound to this rate table."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        return OrderCost(model=self, product=Product(product), side=side)

    def round_trip_cost(
        self,
        product: Product,
        buy_price: float,
        sell_price: float,
        quantity: float,
    ) -> CostBreakdown:
        """Total cost of a buy and an offsetting sell of the same quantity."""
        return self.fill_cost(product, "buy", buy_price, quantity) + self.fill_cost(
            product, "sell", sell_price, quantity
        )

    def breakeven_ticks(
        self,
        product: Product,
        price: float,
        lot_size: float,
        tick_size: float,
        n_lots: float = 1.0,
    ) -> float:
        """Ticks of spread capture needed to cover a round trip's costs.

        This is the number that decides whether market making is viable in an
        instrument at all. Compare it against the instrument's actual quoted
        spread in ticks: if breakeven exceeds the spread, no quoting strategy of
        any sophistication can be profitable at these fee rates.

        Both legs are priced at ``price``; the resulting cost is very slightly
        conservative for a profitable round trip (STT on the sell leg scales
        with the higher sell price) but the error is far below the precision of
        the rate table.

        Each leg is charged brokerage separately, which is correct here: a round
        trip is two orders. It is *not* the right model for one quote that fills
        in pieces -- see :class:`OrderCost`.
        """
        if tick_size <= 0:
            raise ValueError(f"tick_size must be positive, got {tick_size}")
        if lot_size <= 0:
            raise ValueError(f"lot_size must be positive, got {lot_size}")
        quantity = lot_size * n_lots
        cost = self.round_trip_cost(product, price, price, quantity).total
        rupees_per_tick = tick_size * quantity
        return cost / rupees_per_tick


@dataclass
class OrderCost:
    """Accumulates the cost of a single order across its fills.

    Brokerage is levied per *executed order*, not per fill. A resting quote for
    twenty lots that is filled in five partials pays Rs 20 once, not Rs 100 --
    and partial fills are the normal case for a market maker, not an edge case.
    Charging per fill would inflate the cost of precisely the orders this
    project exists to measure.

    Two consequences shape the design:

    - Statutory charges scale with executed quantity, so they accrue per fill.
    - Brokerage may depend on cumulative executed value (a ``pct`` cap), so it
      is **recomputed** from the running total on every query rather than
      accumulated. For a flat schedule this is a constant; for a capped one it
      grows until the cap binds.

    GST is applied last, over accumulated statutory components plus brokerage.
    Because GST is linear this gives the same answer as summing per-fill GST,
    without needing brokerage to be known per fill.

    An order that never fills costs nothing -- brokerage is on *executed*
    orders, and cancelling a resting quote is free.
    """

    model: CostModel
    product: Product
    side: Side
    filled_quantity: float = 0.0
    executed_value: float = 0.0
    n_fills: int = 0
    _statutory: dict[str, float] = field(
        default_factory=lambda: {name: 0.0 for name in _TURNOVER_COMPONENTS}
    )

    def add_fill(self, price: float, quantity: float) -> None:
        """Record one (partial) fill of this order."""
        if quantity < 0:
            raise ValueError(f"quantity must be non-negative, got {quantity}")
        if quantity == 0:
            return
        base_value = float(price) * float(quantity)
        charges = self.model._statutory_charges(self.product, self.side, base_value)
        for name, value in charges.items():
            self._statutory[name] += value
        self.filled_quantity += float(quantity)
        self.executed_value += base_value
        self.n_fills += 1

    @property
    def brokerage(self) -> float:
        if self.n_fills == 0:
            return 0.0
        return self.model.brokerage(self.product, self.executed_value)

    @property
    def average_price(self) -> float | None:
        if self.filled_quantity == 0:
            return None
        return self.executed_value / self.filled_quantity

    def breakdown(self) -> CostBreakdown:
        """Total cost of the order as filled so far."""
        charges = dict(self._statutory)
        charges["brokerage"] = self.brokerage
        charges["gst"] = self.model._gst_on(charges)
        return CostBreakdown(**charges)

    @property
    def total(self) -> float:
        return self.breakdown().total
