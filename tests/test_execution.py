"""Orders cannot reach the broker until the risk gate allows them."""

from __future__ import annotations

import asyncio

import pytest

from auto_trading.broker import OrderIntent, OrderSide
from auto_trading.execution import Executor
from auto_trading.risk import RiskGate, RiskRejected


def test_submit_stops_at_the_risk_gate() -> None:
    class RejectingBroker:
        venue = "ibkr"

        async def place(self, intent: OrderIntent) -> None:
            raise AssertionError("broker.place was called")

    intent = OrderIntent(
        client_id="cid-1",
        symbol="2330.TW",
        side=OrderSide.BUY,
        quantity=1000,
    )

    async def scenario() -> None:
        executor = Executor(broker=RejectingBroker(), gate=RiskGate())
        with pytest.raises(RiskRejected):
            await executor.submit(intent)

    asyncio.run(scenario())
