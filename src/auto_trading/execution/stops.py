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


def disaster_stop_price(
    average_fill: float,
    *,
    protective_stop_pct: float | None = None,
    stop_loss_pct: float | None = None,
    multiplier: float = 1.5,
    default_pct: float = 0.15,
    market: str = "US",
) -> float:
    """Stop from percent points in a strategy file, or from the decimal default.

    ``protective_stop_pct`` and ``stop_loss_pct`` are percent points (15.0
    means 15%). The default is already a fraction and is not divided again.
    A protective value is not widened by ``multiplier``.
    """
    from auto_trading.execution.pricing import align_tick
    from auto_trading.market.ticks import tick_size

    if protective_stop_pct is not None:
        distance = protective_stop_pct / 100.0
    elif stop_loss_pct is not None:
        distance = (stop_loss_pct / 100.0) * multiplier
    else:
        distance = default_pct
    raw = average_fill * (1.0 - distance)
    tick = tick_size(market, raw)
    return align_tick(raw, tick, buy=False)


def cancel_keeps_protective(*, protective: bool, full_stop: bool) -> bool:
    """True when this cancel must be refused so the broker stop stays working."""
    return protective and full_stop
