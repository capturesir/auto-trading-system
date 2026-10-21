"""Admin holdings fields and guest weights from the paper account snapshot."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_trading.dashboard.holdings import admin_holdings, guest_holdings
from auto_trading.execution.account_positions import amount_in_system_base, rows_from_broker
from auto_trading.execution.ibkr import load_portfolio_marks, net_liquidation_base
from auto_trading.execution.services import account_once

PIN = "DUR243736"


def _contract(**kwargs: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "symbol": "SPY",
        "conId": 756733,
        "currency": "USD",
        "primaryExchange": "ARCA",
        "exchange": "SMART",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _position(contract: SimpleNamespace, qty: float = 10.0, avg: float = 772.38) -> SimpleNamespace:
    return SimpleNamespace(contract=contract, position=qty, avgCost=avg)


def _mark(
    contract: SimpleNamespace,
    *,
    market_price: float = 780.0,
    market_value: float = 7800.0,
    unrealized: float = 76.2,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract,
        marketPrice=market_price,
        marketValue=market_value,
        averageCost=772.38,
        unrealizedPNL=unrealized,
    )


def _usd_rows(hkd_rate: str | None = None) -> list[dict[str, str]]:
    rows = [
        {"tag": "NetLiquidation", "currency": "USD", "value": "1000"},
        {"tag": "ExchangeRate", "currency": "USD", "value": "1"},
        {"tag": "ExchangeRate", "currency": "BASE", "value": "1"},
    ]
    if hkd_rate is not None:
        rows.append({"tag": "ExchangeRate", "currency": "HKD", "value": hkd_rate})
    return rows


def test_rows_keep_avg_and_add_canonical_cost() -> None:
    contract = _contract()
    rows = rows_from_broker([_position(contract)], [])
    assert rows == [
        {
            "symbol": "SPY",
            "qty": 10.0,
            "con_id": 756733,
            "currency": "USD",
            "exchange": "ARCA",
            "avg": 772.38,
            "avg_cost": 772.38,
        }
    ]


def test_portfolio_marks_fill_market_value_and_unrealized_pnl() -> None:
    contract = _contract()
    rows = rows_from_broker(
        [_position(contract)],
        [_mark(contract)],
        convert=lambda amount: amount_in_system_base(amount, _usd_rows(), system="HKD"),
    )
    row = rows[0]
    assert row["avg"] == pytest.approx(772.38)
    assert row["avg_cost"] == pytest.approx(772.38)
    assert row["market_price"] == pytest.approx(780.0)
    assert row["mkt_value"] == pytest.approx(7800 * 7.8)
    assert row["market_value"] == pytest.approx(row["mkt_value"])
    assert row["unrealized"] == pytest.approx(76.2 * 7.8)
    assert row["unrealized_pnl"] == pytest.approx(row["unrealized"])


def test_market_value_converts_like_nlv() -> None:
    plain = _usd_rows()
    assert amount_in_system_base(1000, plain, system="HKD") == pytest.approx(7800)
    assert net_liquidation_base(plain) == pytest.approx(7800)
    quoted = _usd_rows("0.125")
    assert amount_in_system_base(1000, quoted, system="HKD") == pytest.approx(8000)
    assert net_liquidation_base(quoted) == pytest.approx(8000)
    assert amount_in_system_base(1000, plain, system="USD") == pytest.approx(1000)


def test_mark_matches_on_symbol_when_con_id_is_missing() -> None:
    contract = _contract(conId=0, primaryExchange="")
    mark_contract = _contract(conId=0)
    rows = rows_from_broker([_position(contract)], [_mark(mark_contract)], convert=None)
    assert rows[0]["exchange"] == "SMART"
    assert rows[0]["mkt_value"] == pytest.approx(7800)


def test_non_finite_mark_is_omitted_and_avg_stays() -> None:
    contract = _contract()
    mark = _mark(contract, market_value=float("nan"), unrealized=float("inf"))
    rows = rows_from_broker([_position(contract)], [mark])
    row = rows[0]
    assert row["avg"] == pytest.approx(772.38)
    assert row["avg_cost"] == pytest.approx(772.38)
    assert "mkt_value" not in row
    assert "unrealized_pnl" not in row


def test_admin_maps_avg_and_portfolio_aliases() -> None:
    bare = admin_holdings(
        {
            "nlv": 100_000,
            "positions": [{"symbol": "SPY", "qty": 10, "avg": 772.38, "currency": "USD"}],
        }
    )
    cost_only = bare["positions"][0]
    assert cost_only["avg"] == pytest.approx(772.38)
    assert cost_only["avg_cost"] == pytest.approx(772.38)
    assert cost_only["mkt_value"] is None
    assert cost_only["unrealized_pnl"] is None
    assert cost_only["weight_pct"] is None

    marked = admin_holdings(
        {
            "nlv": 100_000,
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 10,
                    "avgCost": 772.38,
                    "marketValue": 25_000,
                    "unrealizedPNL": 120.5,
                }
            ],
        }
    )
    row = marked["positions"][0]
    assert row["avg"] == pytest.approx(772.38)
    assert row["avg_cost"] == pytest.approx(772.38)
    assert row["mkt_value"] == pytest.approx(25_000)
    assert row["market_value"] == pytest.approx(25_000)
    assert row["unrealized"] == pytest.approx(120.5)
    assert row["unrealized_pnl"] == pytest.approx(120.5)
    assert row["weight_pct"] == pytest.approx(25)


def test_guest_weight_is_market_value_over_total_assets() -> None:
    account = {
        "nlv": 100_000,
        "cash": 75_000,
        "positions": [
            {
                "symbol": "SPY",
                "qty": 10,
                "avg": 772.38,
                "mkt_value": 25_000,
                "unrealized_pnl": 100,
            },
            {"symbol": "AAPL", "qty": 1, "avg": 10, "market_value": 50_000},
            {"ticker": "QQQ", "qty": 2, "avg_cost": 400},
        ],
    }
    view = guest_holdings(account)
    assert view == {
        "positions": [
            {"symbol": "SPY", "weight_pct": 25.0},
            {"symbol": "AAPL", "weight_pct": 50.0},
            {"symbol": "QQQ"},
        ]
    }
    for item in view["positions"]:
        assert set(item) <= {"symbol", "weight_pct"}
    assert "cash" not in view
    assert "nlv" not in view

    with_cash_weight = dict(account)
    with_cash_weight["cash_weight_pct"] = 25
    assert guest_holdings(with_cash_weight)["cash_weight_pct"] == 25


def test_guest_weight_needs_a_positive_total() -> None:
    account = {"nlv": 0, "positions": [{"symbol": "SPY", "qty": 10, "mkt_value": 25_000}]}
    assert guest_holdings(account) == {"positions": [{"symbol": "SPY"}]}


def test_account_once_writes_cost_value_and_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    monkeypatch.setenv("IB_PORT", "4002")
    result = account_once(
        tmp_path,
        datetime(2026, 10, 8, 2, 0, tzinfo=UTC),
        snapshot={
            "accounts": [PIN],
            "nlv": 100_000.0,
            "cash": 75_000.0,
            "positions": [
                {
                    "symbol": "SPY",
                    "qty": 10.0,
                    "avg": 772.38,
                    "con_id": 756733,
                    "currency": "USD",
                    "exchange": "ARCA",
                    "marketValue": 25_000.0,
                    "unrealizedPNL": 120.5,
                }
            ],
            "open_orders": [],
        },
    )
    assert result["wrote"] is True
    body = json.loads((tmp_path / "state" / "account.json").read_text(encoding="utf-8"))
    row = body["positions"][0]
    assert row["qty"] == 10
    assert row["avg"] == pytest.approx(772.38)
    assert row["avg_cost"] == pytest.approx(772.38)
    assert row["mkt_value"] == pytest.approx(25_000)
    assert row["market_value"] == pytest.approx(25_000)
    assert row["unrealized"] == pytest.approx(120.5)
    assert row["unrealized_pnl"] == pytest.approx(120.5)
    assert row["weight_pct"] == pytest.approx(25)
    guest = guest_holdings(body)
    assert guest["positions"] == [{"symbol": "SPY", "weight_pct": 25.0}]
    assert "qty" not in guest["positions"][0]
    assert "avg" not in guest["positions"][0]


def test_portfolio_marks_are_requested_when_positions_have_none() -> None:
    contract = _contract()
    mark = _mark(contract)

    class Book:
        def __init__(self) -> None:
            self.marks: list[SimpleNamespace] = []
            self.calls = 0

        def positions(self) -> list[SimpleNamespace]:
            return [_position(contract)]

        def portfolio(self, account: str = "") -> list[SimpleNamespace]:
            return list(self.marks)

        async def reqAccountUpdatesAsync(self, account: str) -> None:
            self.calls += 1
            assert account == PIN
            self.marks = [mark]

    book = Book()
    found = asyncio.run(load_portfolio_marks(book, PIN))
    assert book.calls == 1
    assert found == [mark]

    book.calls = 0
    again = asyncio.run(load_portfolio_marks(book, PIN))
    assert book.calls == 0
    assert again == [mark]


def test_portfolio_update_is_not_requested_for_a_flat_book() -> None:
    class Book:
        def __init__(self) -> None:
            self.calls = 0

        def positions(self) -> list[object]:
            return []

        def portfolio(self, account: str = "") -> list[object]:
            return []

        async def reqAccountUpdatesAsync(self, account: str) -> None:
            self.calls += 1

    book = Book()
    assert asyncio.run(load_portfolio_marks(book, PIN)) == []
    assert book.calls == 0


def test_portfolio_update_timeout_does_not_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("auto_trading.execution.ibkr._PORTFOLIO_WAIT_SEC", 0.05)
    contract = _contract()

    class Book:
        def positions(self) -> list[SimpleNamespace]:
            return [_position(contract)]

        def portfolio(self, account: str = "") -> list[object]:
            return []

        async def reqAccountUpdatesAsync(self, account: str) -> None:
            await asyncio.sleep(2)

    found = asyncio.run(load_portfolio_marks(Book(), PIN))
    assert found == []
