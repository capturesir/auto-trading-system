"""Moving averages, Wilder RSI, population Bollinger bands, and Wilder ATR.

Indicators use adjusted closes. Quantity, fees, limits, and fair value use
raw prices, which the caller keeps separate. ``compute`` remains a live quote
placeholder and does not fetch data.
"""

from __future__ import annotations

import math


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average. The first ``period - 1`` points are missing."""
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        out[index] = window / period
    return out


def _wilder(values: list[float], period: int) -> list[float | None]:
    """Wilder smoothing. Seed is the SMA of the first ``period`` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    for index in range(period, len(values)):
        previous = out[index - 1]
        assert previous is not None
        out[index] = (previous * (period - 1) + values[index]) / period
    return out


def rsi_wilder(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI. A zero average loss is 100."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains[index] = max(change, 0.0)
        losses[index] = max(-change, 0.0)
    avg_gain = _wilder(gains[1:], period)
    avg_loss = _wilder(losses[1:], period)
    out: list[float | None] = [None] * len(closes)
    for index, (gain, loss) in enumerate(zip(avg_gain, avg_loss, strict=True)):
        if gain is None or loss is None:
            continue
        if loss == 0:
            out[index + 1] = 100.0
        else:
            rs = gain / loss
            out[index + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def population_std(values: list[float]) -> float:
    """Population standard deviation (``ddof=0``)."""
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / len(values)
    return math.sqrt(variance)


def bollinger(
    closes: list[float], period: int, stds: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return middle, lower, and upper bands. Middle is the SMA."""
    middle = sma(closes, period)
    lower: list[float | None] = [None] * len(closes)
    upper: list[float | None] = [None] * len(closes)
    for index, mid in enumerate(middle):
        if mid is None:
            continue
        sigma = population_std(closes[index - period + 1 : index + 1])
        lower[index] = mid - stds * sigma
        upper[index] = mid + stds * sigma
    return middle, lower, upper


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """True range. The first bar uses high minus low."""
    out = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        span = highs[index] - lows[index]
        up = abs(highs[index] - closes[index - 1])
        down = abs(lows[index] - closes[index - 1])
        out.append(max(span, up, down))
    return out


def atr_wilder(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float | None]:
    """Wilder ATR."""
    return _wilder(true_ranges(highs, lows, closes), period)


def ma_slope_up(closes: list[float], period: int, lookback: int) -> bool:
    """True when MA(period) is higher than it was ``lookback`` bars ago."""
    averages = sma(closes, period)
    if len(averages) <= lookback:
        return False
    latest = averages[-1]
    earlier = averages[-1 - lookback]
    if latest is None or earlier is None:
        return False
    return latest > earlier


def compute(symbol: str, timeframe: str) -> None:
    """Placeholder for a current indicator snapshot. It does not call a broker."""
    raise NotImplementedError("technical indicators are not implemented yet")
