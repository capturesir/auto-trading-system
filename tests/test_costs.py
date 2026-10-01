"""Commission and tax examples. At least three cases per market."""

from __future__ import annotations

from datetime import date

from auto_trading.backtest.costs import hk_cost, jp_cost, kr_cost, tw_cost, us_cost


def test_us_min_cap_and_sell_tax() -> None:
    small = us_cost(10, 5_000, sell=False)
    assert small.commission == 1
    assert small.tax == 0
    capped = us_cost(10_000, 100, sell=False)
    assert capped.commission == 1
    sold = us_cost(100, 10_000, sell=True)
    assert sold.tax == 10_000 * 0.0000206 + 100 * 0.000195


def test_japan_minimum_and_rate() -> None:
    assert jp_cost(10_000).commission == 80
    assert jp_cost(1_000_000).commission == 800
    assert jp_cost(200_000).tax == 0


def test_hong_kong_stamp_rounds_up() -> None:
    cheap = hk_cost(1_000)
    assert cheap.commission == 18
    stamped = hk_cost(10_050)
    assert stamped.tax > 10
    large = hk_cost(100_000)
    assert large.commission == 80


def test_korea_tax_depends_on_the_year() -> None:
    assert kr_cost(100_000, date(2022, 6, 1), sell=True).tax == 100_000 * 0.0023
    assert kr_cost(100_000, date(2024, 6, 1), sell=True).tax == 100_000 * 0.0018
    assert kr_cost(100_000, date(2026, 6, 1), sell=True).tax == 100_000 * 0.0020
    assert kr_cost(10_000, date(2026, 6, 1), sell=False).commission == 4000


def test_taiwan_minimum_and_sell_tax() -> None:
    assert tw_cost(10_000, sell=False).commission == 80
    assert tw_cost(1_000_000, sell=False).commission == 800
    sold = tw_cost(1_000_000, sell=True)
    assert sold.tax == 3_000
