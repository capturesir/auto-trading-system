"""Async broker protocol.

Interactive Brokers is the only adapter in this repository. Another venue
can be added later by implementing :class:`Broker` without changing the
risk gate or the strategy engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Balance(BaseModel):
    currency: str = Field(min_length=1)
    amount: float


class BrokerPosition(BaseModel):
    symbol: str = Field(min_length=1)
    quantity: float
    average_price: float | None = None


class Instrument(BaseModel):
    symbol: str = Field(min_length=1)
    market: str = Field(min_length=1)
    currency: str | None = None
    lot_size: float | None = None
    tick_size: float | None = None
    min_notional: float | None = None


class OrderIntent(BaseModel):
    """A proposed order. ``client_id`` is the idempotency key."""

    client_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: OrderSide
    quantity: float = Field(gt=0)
    limit_price: float | None = None


class OrderAck(BaseModel):
    client_id: str
    broker_order_id: str | None = None


class OrderState(BaseModel):
    client_id: str
    status: str


class Fill(BaseModel):
    client_id: str
    price: float
    quantity: float
    fee: float | None = None
    fee_currency: str | None = None


class Broker(Protocol):
    """Venue-neutral trading port. Adapters must not be called except by the executor."""

    venue: str

    async def balances(self) -> list[Balance]: ...

    async def positions(self) -> list[BrokerPosition]: ...

    async def instrument(self, symbol: str) -> Instrument: ...

    async def place(self, intent: OrderIntent) -> OrderAck: ...

    async def cancel(self, client_id: str) -> None: ...

    async def order(self, client_id: str) -> OrderState: ...

    def fills(self) -> AsyncIterator[Fill]: ...
