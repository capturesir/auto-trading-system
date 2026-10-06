"""IBKR adapter for the async Broker protocol.

A real session uses ``ib_async`` against IB Gateway. That import happens
only inside :meth:`PaperGateway.connect`, so tests never load it and CI
does not open a socket. Ports are the paper pair only.
"""

from __future__ import annotations

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
from auto_trading.config import Settings
from auto_trading.execution.contracts import map_contract
from auto_trading.execution.r0 import AccountGuard, latch_kill_switch
from auto_trading.execution.stops import cancel_keeps_protective


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
        self.trading_home = trading_home

        def _trip(code: str) -> None:
            if trading_home is not None:
                latch_kill_switch(trading_home, code)

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

        AccountGuard(
            self.settings.ibkr_port,
            self.settings.ibkr_paper_account,
            on_violation=_trip,
        ).observe(accounts)
        return accounts
