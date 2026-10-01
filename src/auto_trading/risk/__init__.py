"""Risk gate. This is the only path that may release an order to the executor.

Rules are not implemented yet. Strategy specs and any language model cannot
bypass or relax this gate, and neither can place orders on its behalf.
"""

from auto_trading.broker import OrderIntent


class RiskGate:
    """Rejects every intent until real checks exist."""

    def allow(self, intent: OrderIntent) -> None:
        """Raise when the intent must not continue to the broker."""
        raise NotImplementedError("risk gate is not implemented yet")
