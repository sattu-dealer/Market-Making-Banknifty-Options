"""Tests for the Indian F&O cost stack.

Expected values are re-derived from the published rates written out as literals
here, rather than copied from the implementation -- otherwise the test only
proves the code agrees with itself. The headline totals are additionally pinned
as exact constants, because they are the numbers the Phase 0 viability finding
rests on and a silent drift in them would change the project's conclusion.
"""

from __future__ import annotations

import math

import pytest

from bnfmm.analysis.quote_size import freeze_lot_cap, pct_cap_breakpoint, size_ladder
from bnfmm.sim.costs import CostBreakdown, CostModel, Product

from .conftest import (
    COSTS_PATH,
    FREEZE_QTY,
    FUT_PRICE,
    FUT_TICK,
    LOT_SIZE,
    OPT_PREMIUM,
    OPT_TICK,
)

# Rates, transcribed from config/costs.yaml's source. Duplicated deliberately.
FUT_STT = 0.0005
FUT_TXN = 0.0000183
FUT_STAMP = 0.00002
OPT_STT = 0.0015
OPT_TXN = 0.0003553
OPT_STAMP = 0.00003
SEBI = 0.000001
IPFT = 0.000000001
GST = 0.18
DHAN_PER_ORDER = 20.0  # dhan.co/pricing, retrieved 2026-08-23: flat, all F&O

FUT_NOTIONAL = FUT_PRICE * LOT_SIZE  # 1,733,550 per lot
OPT_TURNOVER = OPT_PREMIUM * LOT_SIZE  # 30,000 premium per lot


# --- component-level correctness ---------------------------------------------


def test_futures_buy_leg_components(costs: CostModel) -> None:
    c = costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, LOT_SIZE)
    assert c.stt == 0.0, "STT is sell-side only on futures"
    assert c.exchange_txn == pytest.approx(FUT_NOTIONAL * FUT_TXN)
    assert c.sebi_fee == pytest.approx(FUT_NOTIONAL * SEBI)
    assert c.stamp_duty == pytest.approx(FUT_NOTIONAL * FUT_STAMP)
    assert c.ipft == pytest.approx(FUT_NOTIONAL * IPFT)
    assert c.brokerage == 0.0


def test_futures_sell_leg_charges_stt_on_full_notional(costs: CostModel) -> None:
    c = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE)
    assert c.stt == pytest.approx(FUT_NOTIONAL * FUT_STT)
    assert c.stt == pytest.approx(866.775), "Rs 867 of STT against a Rs 6.00 tick"
    assert c.stamp_duty == 0.0, "stamp duty is buy-side only"


def test_options_stt_is_on_premium_not_notional(costs: CostModel) -> None:
    """The structural reason options are a more plausible venue than futures.

    Same lot, same underlying level -- but the options STT base is the premium
    (Rs 30,000) rather than the contract notional (Rs 1.73 crore).
    """
    opt = costs.fill_cost(Product.OPTIONS, "sell", OPT_PREMIUM, LOT_SIZE)
    fut = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE)
    assert opt.stt == pytest.approx(OPT_TURNOVER * OPT_STT)
    assert opt.stt == pytest.approx(45.0)
    # Higher *rate* (0.15% vs 0.05%), far smaller base, ~19x less tax.
    assert opt.stt < fut.stt / 15


def test_gst_excludes_stt_and_stamp_duty(costs: CostModel) -> None:
    """GST is levied on brokerage and exchange-side charges, never on a tax."""
    c = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE)
    taxable = c.exchange_txn + c.sebi_fee + c.ipft + c.brokerage
    assert c.gst == pytest.approx(GST * taxable)
    # If STT were in the GST base this would be off by ~Rs 156, i.e. 26 ticks.
    assert c.gst < GST * (taxable + c.stt) - 100


def test_side_must_be_buy_or_sell(costs: CostModel) -> None:
    with pytest.raises(ValueError, match="side"):
        costs.fill_cost(Product.FUTURES, "SELL", FUT_PRICE, LOT_SIZE)  # type: ignore[arg-type]


def test_negative_quantity_rejected(costs: CostModel) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, -LOT_SIZE)


def test_zero_quantity_is_free(costs: CostModel) -> None:
    assert costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, 0).total == 0.0


def test_cost_is_linear_in_quantity(costs: CostModel) -> None:
    one = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE).total
    ten = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, 10 * LOT_SIZE).total
    assert ten == pytest.approx(10 * one)


def test_missing_product_schedule_rejected() -> None:
    with pytest.raises(ValueError, match="missing schedule"):
        CostModel(rates={"futures": {}, "gst": {"rate": 0.18}})


# --- breakdown bookkeeping ----------------------------------------------------


def test_total_is_the_sum_of_components(costs: CostModel) -> None:
    c = costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE)
    d = c.as_dict()
    parts = sum(v for k, v in d.items() if k != "total")
    assert d["total"] == pytest.approx(parts)


def test_breakdowns_add_componentwise() -> None:
    a = CostBreakdown(stt=1.0, gst=0.5)
    b = CostBreakdown(stt=2.0, stamp_duty=3.0)
    total = a + b
    assert (total.stt, total.gst, total.stamp_duty) == (3.0, 0.5, 3.0)
    assert total.total == pytest.approx(a.total + b.total)


def test_round_trip_is_buy_plus_sell(costs: CostModel) -> None:
    rt = costs.round_trip_cost(Product.FUTURES, FUT_PRICE, FUT_PRICE + 1.0, LOT_SIZE)
    legs = costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, LOT_SIZE) + costs.fill_cost(
        Product.FUTURES, "sell", FUT_PRICE + 1.0, LOT_SIZE
    )
    assert rt.total == pytest.approx(legs.total)


# --- the headline finding -----------------------------------------------------
# These four assertions are the Phase 0 instrument decision. Pinned exactly.


def test_futures_round_trip_cost_per_lot(costs: CostModel) -> None:
    rt = costs.round_trip_cost(Product.FUTURES, FUT_PRICE, FUT_PRICE, LOT_SIZE)
    assert rt.total == pytest.approx(980.41, abs=0.01)


def test_futures_breakeven_is_two_orders_of_magnitude_above_the_spread(
    costs: CostModel,
) -> None:
    """Retail market making in BANKNIFTY futures is structurally unviable.

    Breakeven is ~163 ticks (Rs 32.7) of captured spread per round trip against
    a quoted spread of roughly 5-25 ticks. No amount of quoting sophistication
    closes a 6x-plus gap -- this is a fee-structure fact, not a strategy result.
    """
    be = costs.breakeven_ticks(Product.FUTURES, FUT_PRICE, LOT_SIZE, FUT_TICK)
    assert be == pytest.approx(163.4, abs=0.1)
    assert be * FUT_TICK == pytest.approx(32.68, abs=0.01)


def test_options_round_trip_cost_per_lot(costs: CostModel) -> None:
    rt = costs.round_trip_cost(Product.OPTIONS, OPT_PREMIUM, OPT_PREMIUM, LOT_SIZE)
    assert rt.total == pytest.approx(71.13, abs=0.01)


def test_options_breakeven_is_within_reach_of_the_spread(costs: CostModel) -> None:
    """~47 ticks (Rs 2.37) against an ATM spread of roughly Rs 1-3.

    Still demanding, but the same order of magnitude as the spread, which is
    what makes options the defensible instrument for this study.
    """
    be = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK)
    assert be == pytest.approx(47.42, abs=0.01)
    assert be * OPT_TICK == pytest.approx(2.37, abs=0.01)


def test_options_are_materially_cheaper_to_quote_than_futures(costs: CostModel) -> None:
    fut = costs.breakeven_ticks(Product.FUTURES, FUT_PRICE, LOT_SIZE, FUT_TICK) * FUT_TICK
    opt = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK) * OPT_TICK
    assert fut / opt > 10, "the instrument switch rests on this ratio"


def test_delta_hedging_with_futures_costs_more_than_the_option_edge(
    costs: CostModel,
) -> None:
    """Finding 3: hedging is unaffordable, which is why the design has no
    hedging leg and manages delta by quote skew instead.

    One ATM option lot has delta ~0.5, so neutralising it takes half a futures
    lot -- and half a futures round trip already costs several times the option
    round trip it is protecting.
    """
    option_rt = costs.round_trip_cost(Product.OPTIONS, OPT_PREMIUM, OPT_PREMIUM, LOT_SIZE).total
    futures_rt = costs.round_trip_cost(Product.FUTURES, FUT_PRICE, FUT_PRICE, LOT_SIZE).total
    hedge_rt = 0.5 * futures_rt
    assert hedge_rt > 5 * option_rt
    # Expressed in the unit the strategy actually earns in: option ticks.
    assert hedge_rt / (OPT_TICK * LOT_SIZE) > 300


def test_breakeven_scales_with_premium_not_lot_count(costs: CostModel) -> None:
    """Breakeven is per-unit, so quoting more lots does not dilute the cost."""
    one = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK, n_lots=1)
    ten = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK, n_lots=10)
    assert one == pytest.approx(ten)
    # Halving the premium halves the cost base and therefore the breakeven.
    cheap = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM / 2, LOT_SIZE, OPT_TICK)
    assert cheap == pytest.approx(one / 2)


@pytest.mark.parametrize("bad", [0.0, -0.05])
def test_breakeven_rejects_nonpositive_tick(costs: CostModel, bad: float) -> None:
    with pytest.raises(ValueError, match="tick_size"):
        costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, bad)


def test_breakeven_rejects_nonpositive_lot_size(costs: CostModel) -> None:
    with pytest.raises(ValueError, match="lot_size"):
        costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, 0, OPT_TICK)


# --- brokerage profiles -------------------------------------------------------


def test_member_profile_is_the_default(costs: CostModel) -> None:
    assert costs.profile == "member"
    assert costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, LOT_SIZE).brokerage == 0.0


def test_dhan_charges_a_flat_fee_on_futures(retail_costs: CostModel) -> None:
    c = retail_costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, LOT_SIZE)
    assert c.brokerage == pytest.approx(DHAN_PER_ORDER)


def test_dhan_charges_the_same_flat_fee_on_options(retail_costs: CostModel) -> None:
    """Regression: Dhan F&O brokerage is FLAT Rs 20, with no percentage cap.

    The first version of config/costs.yaml applied min(Rs 20, 0.03%) to both
    products, which put options brokerage at Rs 9 on a Rs 30,000 premium -- Rs 11
    per order too low, and Rs 22 per round trip on a Rs 71 statutory bill. The
    "whichever is lower" rule is real, but it covers equity/ETF intraday and MTF,
    not F&O. See DECISIONS.md #12.
    """
    c = retail_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, LOT_SIZE)
    assert c.brokerage == pytest.approx(DHAN_PER_ORDER)
    assert c.brokerage != pytest.approx(9.0), "the 0.03% cap must not apply to F&O"


def test_dhan_brokerage_does_not_scale_with_size(retail_costs: CostModel) -> None:
    """A flat fee is flat: ten lots in one order still costs one fee."""
    one = retail_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, LOT_SIZE)
    ten = retail_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, 10 * LOT_SIZE)
    assert one.brokerage == pytest.approx(ten.brokerage)


def test_retail_brokerage_is_gst_bearing(retail_costs: CostModel) -> None:
    plain = retail_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, LOT_SIZE)
    assert plain.gst > GST * plain.brokerage * 0.99


def test_zerodha_futures_cap_binds_only_below_the_breakpoint(
    zerodha_costs: CostModel,
) -> None:
    """The pct branch is a real rule somewhere, so keep it exercised.

    Zerodha futures brokerage is min(Rs 20, 0.03% of turnover). The breakpoint
    is Rs 66,667 of turnover -- far below one BANKNIFTY futures lot (Rs 1.73
    crore), so in practice the flat fee always binds. The sub-lot cases below are
    not realistic sizes; they exist to prove the branch is wired correctly.
    """
    breakpoint_ = pct_cap_breakpoint(zerodha_costs, Product.FUTURES)
    assert breakpoint_ == pytest.approx(DHAN_PER_ORDER / 0.0003)

    below = zerodha_costs.fill_cost(Product.FUTURES, "buy", 1000.0, LOT_SIZE)
    assert below.brokerage == pytest.approx(0.0003 * 30_000)
    assert below.brokerage < DHAN_PER_ORDER

    at_real_size = zerodha_costs.fill_cost(Product.FUTURES, "buy", FUT_PRICE, LOT_SIZE)
    assert at_real_size.brokerage == pytest.approx(DHAN_PER_ORDER)


def test_zerodha_options_have_no_cap(zerodha_costs: CostModel) -> None:
    assert pct_cap_breakpoint(zerodha_costs, Product.OPTIONS) is None
    c = zerodha_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, LOT_SIZE)
    assert c.brokerage == pytest.approx(DHAN_PER_ORDER)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown broker profile"):
        CostModel.for_profile("definitely-not-a-broker", COSTS_PATH)


def test_reported_profiles_are_the_pair_the_project_publishes() -> None:
    """DECISIONS.md #13: every figure is a member/retail pair, never one number."""
    assert CostModel.reported_profiles(COSTS_PATH) == ("member", "dhan")


# --- the retail headline ------------------------------------------------------


def test_retail_options_breakeven_with_brokerage(retail_costs: CostModel) -> None:
    """Rs 3.94 per unit at one lot, against Rs 2.37 statutory-only.

    Brokerage is 66% on top of the statutory hurdle at one lot. It is the single
    largest cost component at retail size, and it was excluded from every number
    published before this test existed.
    """
    be = retail_costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK)
    assert be * OPT_TICK == pytest.approx(3.94, abs=0.01)
    assert be == pytest.approx(78.9, abs=0.1)


def test_retail_brokerage_dominates_options_breakeven(
    costs: CostModel, retail_costs: CostModel
) -> None:
    """Two Rs 20 orders on a Rs 71 statutory bill: not a rounding error."""
    member = costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK)
    retail = retail_costs.breakeven_ticks(Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK)
    assert retail > member
    assert retail / member == pytest.approx(1.66, abs=0.02)
    assert math.isfinite(retail)


def test_retail_futures_breakeven_barely_moves(retail_costs: CostModel) -> None:
    """The futures verdict does not depend on brokerage, which is the point.

    Rs 47 of brokerage on a Rs 980 statutory bill is under 5%. Finding 1 is a
    statutory-rate result and survives any brokerage assumption -- including a
    zero-brokerage exchange member.
    """
    be = retail_costs.breakeven_ticks(Product.FUTURES, FUT_PRICE, LOT_SIZE, FUT_TICK)
    assert be * FUT_TICK == pytest.approx(34.25, abs=0.02)
    assert be == pytest.approx(171.3, abs=0.2)


# --- order-level brokerage ----------------------------------------------------


def test_partial_fills_of_one_order_pay_brokerage_once(retail_costs: CostModel) -> None:
    """The defect this class exists to prevent.

    A resting quote for five lots that fills in five one-lot pieces is one
    executed order and pays Rs 20 once. Charging per fill would have inflated its
    cost by Rs 80 -- 32 ticks on a hurdle of 79.
    """
    order = retail_costs.order(Product.OPTIONS, "buy")
    for _ in range(5):
        order.add_fill(OPT_PREMIUM, LOT_SIZE)

    single = retail_costs.order(Product.OPTIONS, "buy")
    single.add_fill(OPT_PREMIUM, 5 * LOT_SIZE)

    assert order.brokerage == pytest.approx(DHAN_PER_ORDER)
    assert order.total == pytest.approx(single.total)
    # And strictly cheaper than five separate orders would have been.
    five_orders = 5 * retail_costs.fill_cost(Product.OPTIONS, "buy", OPT_PREMIUM, LOT_SIZE).total
    assert order.total < five_orders
    assert five_orders - order.total == pytest.approx(4 * DHAN_PER_ORDER * (1 + GST), abs=0.01)


def test_order_statutory_charges_still_scale_with_quantity(
    retail_costs: CostModel,
) -> None:
    """Only brokerage is fixed. Everything else is per unit."""
    small = retail_costs.order(Product.OPTIONS, "sell")
    small.add_fill(OPT_PREMIUM, LOT_SIZE)
    big = retail_costs.order(Product.OPTIONS, "sell")
    big.add_fill(OPT_PREMIUM, 10 * LOT_SIZE)
    assert big.breakdown().stt == pytest.approx(10 * small.breakdown().stt)
    assert big.brokerage == pytest.approx(small.brokerage)


def test_unfilled_order_is_free(retail_costs: CostModel) -> None:
    """Brokerage is on *executed* orders. Cancelling a resting quote costs nothing.

    Market making cancels and requotes constantly, so charging an unfilled order
    would swamp every other cost in the model.
    """
    order = retail_costs.order(Product.OPTIONS, "buy")
    assert order.total == 0.0
    assert order.brokerage == 0.0
    assert order.average_price is None
    order.add_fill(OPT_PREMIUM, 0)
    assert order.n_fills == 0, "a zero-quantity fill is not a fill"
    assert order.total == 0.0


def test_order_matches_fill_cost_for_a_single_fill(retail_costs: CostModel) -> None:
    """One fill of one order must agree with the standalone arithmetic path."""
    order = retail_costs.order(Product.FUTURES, "sell")
    order.add_fill(FUT_PRICE, LOT_SIZE)
    direct = retail_costs.fill_cost(Product.FUTURES, "sell", FUT_PRICE, LOT_SIZE)
    assert order.breakdown().as_dict() == pytest.approx(direct.as_dict())


def test_order_tracks_average_price(retail_costs: CostModel) -> None:
    order = retail_costs.order(Product.OPTIONS, "buy")
    order.add_fill(1000.0, LOT_SIZE)
    order.add_fill(1010.0, LOT_SIZE)
    assert order.average_price == pytest.approx(1005.0)
    assert order.filled_quantity == pytest.approx(2 * LOT_SIZE)


def test_order_rejects_negative_quantity(retail_costs: CostModel) -> None:
    order = retail_costs.order(Product.OPTIONS, "buy")
    with pytest.raises(ValueError, match="non-negative"):
        order.add_fill(OPT_PREMIUM, -1)


def test_order_side_is_validated(retail_costs: CostModel) -> None:
    with pytest.raises(ValueError, match="side"):
        retail_costs.order(Product.OPTIONS, "SELL")  # type: ignore[arg-type]


def test_capped_brokerage_grows_with_cumulative_executed_value(
    zerodha_costs: CostModel,
) -> None:
    """Why brokerage is recomputed per query rather than accumulated.

    Under a pct cap the charge depends on the order's *total* executed value, so
    it is not knowable from any single fill. Here each fill adds Rs 30,000 of
    turnover: the percentage binds after one fill and the flat fee after three.
    """
    order = zerodha_costs.order(Product.FUTURES, "buy")
    order.add_fill(1000.0, LOT_SIZE)
    assert order.brokerage == pytest.approx(9.0)
    order.add_fill(1000.0, LOT_SIZE)
    assert order.brokerage == pytest.approx(18.0)
    order.add_fill(1000.0, LOT_SIZE)
    assert order.brokerage == pytest.approx(DHAN_PER_ORDER), "cap now binds"
    order.add_fill(1000.0, LOT_SIZE)
    assert order.brokerage == pytest.approx(DHAN_PER_ORDER), "and stays bound"


# --- quote size ---------------------------------------------------------------


def test_per_unit_cost_falls_with_quote_size(retail_costs: CostModel) -> None:
    """The trade-off Avellaneda-Stoikov does not model.

    A/S derives an optimal spread assuming no fixed per-trade cost. With Rs 20
    per order, per-unit cost is 38% lower at twenty lots than at one, so size
    becomes a decision variable rather than a free scaling factor.
    """
    ladder = size_ladder(
        retail_costs, Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK
    )
    per_unit = [row.per_unit for row in ladder]
    assert per_unit == sorted(per_unit, reverse=True), "must be monotonically cheaper"

    first, last = ladder[0], ladder[-1]
    assert first.n_lots == 1 and last.n_lots == 20
    assert first.per_unit == pytest.approx(3.94, abs=0.01)
    assert last.per_unit == pytest.approx(2.45, abs=0.01)
    # Brokerage's share of total cost collapses as size grows.
    assert first.brokerage_share > 0.39
    assert last.brokerage_share < 0.04


def test_member_per_unit_cost_is_flat_in_size(costs: CostModel) -> None:
    """With no fixed fee there is no size effect, which isolates the cause."""
    ladder = size_ladder(costs, Product.OPTIONS, OPT_PREMIUM, LOT_SIZE, OPT_TICK)
    per_unit = [row.per_unit for row in ladder]
    assert per_unit == pytest.approx([per_unit[0]] * len(per_unit))
    assert all(row.brokerage_share == 0.0 for row in ladder)


def test_freeze_quantity_caps_quote_size_at_twenty_lots() -> None:
    """The exchange, not the cost curve, sets the ceiling.

    Per-unit cost keeps falling with size, so the cost model alone would quote
    unboundedly large. BANKNIFTY's 601-unit freeze quantity over a 30-unit lot
    caps a single order at 20 lots.
    """
    assert freeze_lot_cap(FREEZE_QTY, LOT_SIZE) == 20
    assert freeze_lot_cap(600, 30) == 20
    assert freeze_lot_cap(629, 30) == 20, "partial lots cannot be quoted"


@pytest.mark.parametrize(("freeze", "lot"), [(0, 30), (601, 0), (-1, 30)])
def test_freeze_lot_cap_validates_inputs(freeze: float, lot: float) -> None:
    with pytest.raises(ValueError):
        freeze_lot_cap(freeze, lot)
