"""IBKR adapter for the async Broker protocol.

A real session uses ``ib_async`` against IB Gateway. That import happens
only inside :meth:`PaperGateway.connect`, so tests never load it and CI
does not open a socket. Ports are the paper pair only.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from auto_trading.broker import (
    Balance,
    BrokerPosition,
    Fill,
    Instrument,
    OrderAck,
    OrderIntent,
    OrderState,
)
from auto_trading.config import DEFAULT_TRADING_HOME, Settings
from auto_trading.execution.contracts import map_contract
from auto_trading.execution.r0 import AccountGuard, latch_kill_switch
from auto_trading.execution.stops import cancel_keeps_protective
from auto_trading.risk.gate import MAX_QUOTE_DELAY_SEC, RiskRejected


def _field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "")
    return str(getattr(item, name, "") or "")


def net_liquidation_base(rows: list[Any]) -> float | None:
    """Net liquidation in BASE only.

    A missing BASE line is a failure. USD or HKD must not stand in for it.
    """
    for item in rows:
        if _field(item, "tag") != "NetLiquidation":
            continue
        if _field(item, "currency") != "BASE":
            continue
        try:
            return float(_field(item, "value"))
        except ValueError:
            return None
    return None


class IbkrBroker:
    """Paper Gateway adapter. ``place`` re-checks R0 immediately before submit."""

    venue = "ibkr"

    def __init__(
        self,
        client: Any | None = None,
        *,
        port: int = 4002,
        expected_account: str = "",
        trading_home: Path | None = None,
    ) -> None:
        self.client = client
        self.trading_home = _latch_home(trading_home)

        def _trip(code: str) -> None:
            latch_kill_switch(self.trading_home, code)
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                disconnect()

        self.guard = AccountGuard(port, expected_account, on_violation=_trip)

    async def _snapshot(self, account: str) -> None:
        if self.client is not None:
            self.guard.observe(await self.client.managed_accounts())
        self.guard.assert_order_account(account)

    async def balances(self) -> list[Balance]:
        self._require_client()
        return await self.client.balances()

    async def positions(self) -> list[BrokerPosition]:
        self._require_client()
        return await self.client.positions()

    async def instrument(self, symbol: str) -> Instrument:
        spec = map_contract(symbol)
        return Instrument(
            symbol=spec.symbol,
            market=spec.market,
            currency=spec.currency,
            lot_size=spec.lot_size,
        )

    async def place(self, intent: OrderIntent) -> OrderAck:
        """Third R0 check, immediately before the client submits."""
        await self._snapshot(intent.account)
        self._require_client()
        blocked = _quote_block(intent)
        if blocked:
            raise RiskRejected(blocked)
        spec = map_contract(intent.symbol)
        order = {
            "client_id": intent.client_id,
            "account": intent.account,
            "symbol": spec.ib_symbol,
            "exchange": spec.exchange,
            "currency": spec.currency,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "tif": "DAY",
            "order_type": "LMT",
            "quote_source": intent.quote_source,
            "quote_data_type": intent.quote_data_type,
            "quote_age_sec": intent.quote_age_sec,
            "quote_delay_sec": intent.quote_delay_sec,
            "market_data_type": intent.quote_market_data_type,
        }
        return await self.client.place_order(order)

    async def modify(self, client_id: str, limit_price: float, *, account: str = "") -> None:
        """Modifications use the same R0 check as a new order."""
        await self._snapshot(account)
        self._require_client()
        await self.client.modify_order(client_id, limit_price)

    async def cancel(
        self,
        client_id: str,
        *,
        protective: bool = False,
        full_stop: bool = False,
    ) -> None:
        """Cancels still pass after an R0 violation. A full stop keeps disaster stops."""
        if cancel_keeps_protective(protective=protective, full_stop=full_stop):
            return
        self._require_client()
        await self.client.cancel_order(client_id)

    async def order(self, client_id: str) -> OrderState:
        self._require_client()
        return await self.client.order(client_id)

    def fills(self) -> AsyncIterator[Fill]:
        self._require_client()

        async def _stream() -> AsyncIterator[Fill]:
            for fill in await self.client.executions():
                yield fill

        return _stream()

    def _require_client(self) -> None:
        if self.client is None:
            raise ConnectionError("paper gateway is not connected")


def _latch_home(trading_home: Path | None) -> Path:
    """Where an R0 violation writes the kill switch, even when no home was passed."""
    if trading_home is not None:
        return trading_home
    chosen = os.environ.get("TRADING_HOME", "").strip()
    if chosen:
        return Path(chosen)
    return DEFAULT_TRADING_HOME


def _quote_block(intent: OrderIntent) -> str | None:
    """Public prices and market-data type 2 never reach the broker."""
    if intent.protective:
        return None
    if intent.quote_source == "yahoo" or intent.quote_data_type == "public":
        return "R22"
    if intent.quote_market_data_type == 2:
        return "R22"
    if intent.quote_data_type == "delayed" and intent.quote_market_data_type not in (3, 4):
        return "R22"
    if intent.quote_delay_sec is not None and intent.quote_delay_sec > MAX_QUOTE_DELAY_SEC:
        return "QUOTE_DELAY_EXCEEDED"
    return None


class PaperGateway:
    """Real ``ib_async`` session. Not constructed by tests or CI.

    Connects to a Gateway that is already logged in. Login names are not
    read here; the IBC launcher is the only reader of those variables.
    """

    def __init__(self, settings: Settings, *, client_id: int) -> None:
        self.settings = settings
        self.client_id = client_id
        self._ib: Any = None

    async def connect(self) -> list[str]:
        from ib_async import IB

        ib = IB()
        await ib.connectAsync(
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            clientId=self.client_id,
        )
        self._ib = ib
        accounts = list(ib.managedAccounts())

        def _trip(code: str) -> None:
            latch_kill_switch(self.settings.trading_home, code)
            ib.disconnect()

        AccountGuard(
            self.settings.ibkr_port,
            self.settings.ibkr_paper_account,
            on_violation=_trip,
        ).observe(accounts)
        return accounts

    async def snapshot_account(self) -> dict[str, Any]:
        """Connect, check R0, and return NLV, positions, and open orders."""
        accounts = await self.connect()
        ib = self._ib
        try:
            nlv = net_liquidation_base(list(ib.accountValues()))
            positions: list[dict[str, Any]] = []
            for pos in ib.positions():
                contract = pos.contract
                positions.append(
                    {
                        "symbol": str(contract.symbol),
                        "qty": float(pos.position),
                        "avg": float(pos.avgCost),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "exchange": str(
                            getattr(contract, "primaryExchange", "")
                            or getattr(contract, "exchange", "")
                            or ""
                        ),
                    }
                )
            open_orders: list[dict[str, str]] = []
            for trade in ib.openTrades():
                order = trade.order
                contract = trade.contract
                ref = str(getattr(order, "orderRef", "") or getattr(order, "orderId", ""))
                open_orders.append(
                    {
                        "order_id": ref,
                        "symbol": str(contract.symbol),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "exchange": str(
                            getattr(contract, "primaryExchange", "")
                            or getattr(contract, "exchange", "")
                            or ""
                        ),
                    }
                )
            return {
                "accounts": accounts,
                "nlv": nlv,
                "positions": positions,
                "open_orders": open_orders,
            }
        finally:
            with contextlib.suppress(Exception):
                ib.disconnect()

    async def submit_order(self, intent: Any) -> Any:
        """Place one order through the connected Gateway. E2 only."""
        await self.connect()

        class _Client:
            def __init__(self, ib: Any, accounts: list[str]) -> None:
                self._ib = ib
                self._accounts = accounts

            async def managed_accounts(self) -> list[str]:
                return list(self._accounts)

            async def place_order(self, order: dict[str, Any]) -> Any:
                from ib_async import LimitOrder, Stock

                contract = Stock(order["symbol"], order["exchange"], order["currency"])
                side = "BUY" if order["side"] == "BUY" else "SELL"
                ticket = LimitOrder(side, order["quantity"], order["limit_price"])
                ticket.account = order["account"]
                ticket.orderRef = order["client_id"]
                ticket.tif = "DAY"
                self._ib.placeOrder(contract, ticket)
                from auto_trading.broker import OrderAck

                return OrderAck(client_id=order["client_id"], broker_order_id=order["client_id"])

        accounts = list(self._ib.managedAccounts())
        broker = IbkrBroker(
            _Client(self._ib, accounts),
            port=self.settings.ibkr_port,
            expected_account=self.settings.ibkr_paper_account,
            trading_home=self.settings.trading_home,
        )
        return await broker.place(intent)
