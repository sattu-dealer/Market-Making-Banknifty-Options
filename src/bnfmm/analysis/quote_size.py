"""How fixed per-order brokerage makes quote size a decision variable.

Avellaneda-Stoikov (2008) derives an optimal *spread* from inventory risk and
order-arrival intensity. It assumes no fixed per-trade cost, so it has nothing
to say about size: in the model, quoting one lot and quoting twenty are the same
decision scaled.

A flat Rs 20 per executed order breaks that. Statutory charges scale with
quantity, brokerage does not, so per-unit cost falls hyperbolically with order
size -- at a Rs 1,000 premium, breakeven per unit is Rs 3.94 at one lot and
Rs 2.45 at twenty. That is a 38% reduction in the hurdle rate from size alone,
pushing toward large quotes, while inventory risk and adverse selection push the
other way and the exchange freeze quantity caps the top.

This module computes the cost half of that trade-off. The risk half needs the
fill simulator, and the two are combined in the writeup.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..sim.costs import CostModel, Product


@dataclass(frozen=True)
class SizeRow:
    """Round-trip cost of quoting ``n_lots``, per unit and in ticks."""

    n_lots: int
    quantity: float
    round_trip: float
    per_unit: float
    breakeven_ticks: float
    breakeven_rupees: float
    brokerage_share: float
    """Fraction of the round-trip cost that is brokerage plus its GST."""


def freeze_lot_cap(freeze_qty: float, lot_size: float) -> int:
    """Largest whole number of lots a single order may carry.

    The exchange freeze quantity is a hard per-order ceiling, so it caps quote
    size regardless of what the cost curve wants. On BANKNIFTY, 601 units over a
    30-unit lot gives exactly 20 lots.
    """
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if freeze_qty <= 0:
        raise ValueError(f"freeze_qty must be positive, got {freeze_qty}")
    return int(freeze_qty // lot_size)


def size_ladder(
    model: CostModel,
    product: Product,
    price: float,
    lot_size: float,
    tick_size: float,
    lot_counts: tuple[int, ...] = (1, 2, 3, 5, 10, 20),
) -> list[SizeRow]:
    """Per-unit round-trip cost across a ladder of quote sizes.

    A round trip is two orders (one to open, one to close), so a flat schedule
    charges brokerage twice regardless of size. Both legs are priced at
    ``price``; see :meth:`CostModel.breakeven_ticks` for why that is acceptable.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")

    rows: list[SizeRow] = []
    for n in lot_counts:
        quantity = lot_size * n
        breakdown = model.round_trip_cost(product, price, price, quantity)
        total = breakdown.total
        # Brokerage's true weight includes the GST it attracts, which is the
        # number that matters to a trader and is easy to understate by 18%.
        gst_rate = model._gst_rate if "brokerage" in model._gst_applies_to else 0.0
        brokerage_with_gst = breakdown.brokerage * (1.0 + gst_rate)
        per_unit = total / quantity
        rows.append(
            SizeRow(
                n_lots=n,
                quantity=quantity,
                round_trip=total,
                per_unit=per_unit,
                breakeven_ticks=per_unit / tick_size,
                breakeven_rupees=per_unit,
                brokerage_share=(brokerage_with_gst / total) if total else 0.0,
            )
        )
    return rows


def pct_cap_breakpoint(model: CostModel, product: Product) -> float | None:
    """Executed value at which a percentage-capped brokerage stops binding.

    Returns ``None`` for a genuinely flat schedule, where the question does not
    arise. Above the breakpoint the flat fee applies; below it the percentage
    does. Useful for checking whether a broker's "whichever is lower" rule is
    actually relevant at the sizes being quoted -- on Zerodha futures the
    breakpoint is Rs 66,667 of turnover, i.e. well under one BANKNIFTY lot, so
    the flat fee binds in practice.
    """
    spec = model.broker.spec(product)
    if spec.pct <= 0.0:
        return None
    return spec.flat / spec.pct
