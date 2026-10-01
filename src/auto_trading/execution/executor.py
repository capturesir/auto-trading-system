"""Order executor.

``submit`` asks the risk gate before it touches the broker. There is no
other method that places an order.
"""

from __future__ import annotations

from auto_trading.broker import Broker, OrderAck, OrderIntent
from auto_trading.risk import RiskGate


class Executor:
    """Forwards an intent only after :meth:`RiskGate.allow` returns."""

    def __init__(self, broker: Broker, gate: RiskGate) -> None:
        self._broker = broker
        self._gate = gate

    async def submit(self, intent: OrderIntent) -> OrderAck:
        self._gate.allow(intent)
        return await self._broker.place(intent)
