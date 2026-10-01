"""Lot rounding, including the odd-lot comparison that is off by default."""

from __future__ import annotations

from auto_trading.backtest.lots import size_quantity

FX = 0.244
PRICE = 2445.0


def _tw(budget: float, *, odd_lot: bool, order_budget: float | None = None) -> object:
    return size_quantity(
        price_local=PRICE,
        fx_to_base=FX,
        strategy_budget=budget,
        order_budget=order_budget if order_budget is not None else budget,
        ticker_budget=budget,
        market="TW",
        odd_lot_enabled=odd_lot,
        lot_size=1000,
    )


def test_odd_lot_on_buys_83_shares() -> None:
    decision = _tw(50_000, odd_lot=True)
    assert decision.qty == 83
    assert decision.status == "ok"
    assert decision.lot_size == 1000


def test_odd_lot_drops_remainder_above_one_lot() -> None:
    budget = 1234 * PRICE * FX
    decision = _tw(budget, odd_lot=True, order_budget=budget)
    assert decision.qty == 1000
    assert decision.odd_remainder_dropped == 234


def test_odd_lot_off_is_lot_too_large() -> None:
    decision = _tw(50_000, odd_lot=False)
    assert decision.qty == 0
    assert decision.status == "LOT_TOO_LARGE"
    assert decision.binding_limit == "order_cap"


def test_live_eight_shares_are_below_min_cost() -> None:
    decision = _tw(5_000, odd_lot=True)
    assert decision.status == "BELOW_MIN_ORDER_COST"
    assert decision.qty == 0


def test_us_live_zero_is_strategy_target() -> None:
    decision = size_quantity(
        price_local=1000,
        fx_to_base=7.8,
        strategy_budget=100,
        order_budget=50_000,
        ticker_budget=95_000,
        market="US",
        lot_size=1,
    )
    assert decision.status == "LOT_TOO_LARGE"
    assert decision.binding_limit == "strategy_target"


def test_ticker_room_is_the_binding_limit() -> None:
    decision = size_quantity(
        price_local=10,
        fx_to_base=1,
        strategy_budget=50_000,
        order_budget=50_000,
        ticker_budget=5,
        market="US",
        lot_size=1,
    )
    assert decision.binding_limit == "ticker_cap"
