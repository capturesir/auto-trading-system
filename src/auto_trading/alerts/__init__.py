"""Alerts for price, technical, and signal-change conditions.

Delivery is not implemented yet. Alerts do not place orders.
"""

from enum import StrEnum


class AlertKind(StrEnum):
    PRICE = "price"
    TECHNICAL = "technical"
    SIGNAL_CHANGE = "signal_change"


def publish(kind: AlertKind, symbol: str, message: str) -> None:
    """Placeholder for an operator alert."""
    raise NotImplementedError("alerts are not implemented yet")
