"""Broker-side disaster stop.

The stop is 1.5 times the strategy stop under the fill. A strategy with no
stop uses 15% and is not multiplied again. Full-stop cancellation keeps
these orders working.
"""

from __future__ import annotations


def protective_stop_price(
    average_fill: float,
    strategy_stop_pct: float | None,
    *,
    multiplier: float = 1.5,
    default_pct: float = 0.15,
) -> float:
    """GTC STP trigger. ``strategy_stop_pct`` is a fraction (0.06 means 6%)."""
    distance = default_pct if strategy_stop_pct is None else strategy_stop_pct * multiplier
    return average_fill * (1 - distance)


def cancel_keeps_protective(*, protective: bool, full_stop: bool) -> bool:
    """True when this cancel must be refused so the broker stop stays working."""
    return protective and full_stop
