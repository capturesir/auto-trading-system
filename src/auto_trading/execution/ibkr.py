"""IBKR adapter placeholder.

The live client will use IB Gateway and ``ib_async`` (optional extra
``ibkr``). That library is not imported here. A Hong Kong IBKR account is
expected to trade US, Japan, Hong Kong, Korea, and Taiwan equities. Paper
trading stays the default (``IBKR_PAPER``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from auto_trading.broker import (
    Balance,
    BrokerPosition,
    Fill,
    Instrument,
    OrderAck,
    OrderIntent,
    OrderState,
)


class IbkrBroker:
    """Unimplemented :class:`~auto_trading.broker.Broker` for venue ``ibkr``."""

    venue = "ibkr"

    async def balances(self) -> list[Balance]:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    async def positions(self) -> list[BrokerPosition]:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    async def instrument(self, symbol: str) -> Instrument:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    async def place(self, intent: OrderIntent) -> OrderAck:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    async def cancel(self, client_id: str) -> None:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    async def order(self, client_id: str) -> OrderState:
        raise NotImplementedError("IBKR adapter is not implemented yet")

    def fills(self) -> AsyncIterator[Fill]:
        raise NotImplementedError("IBKR adapter is not implemented yet")
