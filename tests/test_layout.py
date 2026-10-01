"""Placeholder packages stay importable and do not touch the broker."""

from __future__ import annotations

import importlib

import pytest

from auto_trading.market import MARKETS

MODULES = [
    "auto_trading",
    "auto_trading.config",
    "auto_trading.signals",
    "auto_trading.market",
    "auto_trading.market.indicators",
    "auto_trading.strategy",
    "auto_trading.strategy.spec",
    "auto_trading.strategy.engine",
    "auto_trading.backtest",
    "auto_trading.alerts",
    "auto_trading.risk",
    "auto_trading.broker",
    "auto_trading.execution",
    "auto_trading.execution.ibkr",
    "auto_trading.execution.executor",
    "auto_trading.portfolio",
    "auto_trading.portfolio.positions",
    "auto_trading.portfolio.journal",
    "auto_trading.storage",
    "auto_trading.storage.sqlite",
    "auto_trading.dashboard",
    "auto_trading.reports",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_markets_include_taiwan() -> None:
    assert MARKETS == ("US", "JP", "HK", "KR", "TW")


def test_placeholders_are_not_wired_to_the_broker() -> None:
    from auto_trading.dashboard import render_dashboard
    from auto_trading.execution.ibkr import IbkrBroker

    with pytest.raises(NotImplementedError):
        IbkrBroker().fills()
    with pytest.raises(NotImplementedError):
        render_dashboard()
