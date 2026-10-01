"""Next-open limit fills, including the odd-lot comparison model."""

from __future__ import annotations

from auto_trading.backtest.fills import try_fill


def test_buy_does_not_fill_when_low_is_above_the_limit() -> None:
    result = try_fill(
        open_price=100,
        high=101,
        low=100.2,
        side="BUY",
        market="US",
        qty=10,
    )
    assert result.filled is False
    assert result.reason == "limit_not_reached"


def test_strict_open_equals_low_does_not_fill() -> None:
    result = try_fill(
        open_price=100,
        high=102,
        low=100,
        side="BUY",
        market="US",
        qty=1,
        strict=True,
    )
    assert result.reason == "strict_no_fill"


def test_open_above_cap_is_not_sent() -> None:
    result = try_fill(
        open_price=110,
        high=111,
        low=100,
        side="BUY",
        market="US",
        qty=1,
        limit_cap=105,
    )
    assert result.reason == "CHASE_CAP_EXCEEDED"


def test_fill_price_is_not_worse_than_the_limit() -> None:
    result = try_fill(
        open_price=100,
        high=101,
        low=99,
        side="BUY",
        market="US",
        qty=1,
    )
    assert result.filled
    assert result.fill_price is not None
    assert result.limit_price is not None
    assert result.fill_price <= result.limit_price
    sell = try_fill(open_price=100, high=101, low=99, side="SELL", market="US", qty=1)
    assert sell.filled
    assert sell.fill_price is not None and sell.limit_price is not None
    assert sell.fill_price >= sell.limit_price


def test_odd_lot_slip_volume_cap_and_strict() -> None:
    filled = try_fill(
        open_price=1000,
        high=1010,
        low=990,
        side="BUY",
        market="TW",
        qty=10,
        odd_lot=True,
    )
    assert filled.filled
    assert filled.fill_price == filled.limit_price
    capped = try_fill(
        open_price=1000,
        high=1010,
        low=990,
        side="BUY",
        market="TW",
        qty=100,
        odd_lot=True,
        avg_volume_20=1000,
    )
    assert capped.filled
    assert capped.qty == 1
    strict = try_fill(
        open_price=1000,
        high=1010,
        low=998,
        side="BUY",
        market="TW",
        qty=10,
        odd_lot=True,
        strict=True,
    )
    assert strict.reason == "strict_odd_lot"
