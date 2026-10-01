"""Pair expected trades with actual fills for the deviation report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TradeRef:
    ticker: str
    side: str
    session: date
    price: float


def _matched(expected: TradeRef, actual: list[TradeRef]) -> TradeRef | None:
    window = {expected.session + timedelta(days=offset) for offset in (-1, 0, 1)}
    for trade in actual:
        same_name = trade.ticker == expected.ticker and trade.side == expected.side
        if same_name and trade.session in window:
            return trade
    return None


def trade_match_rate(expected: list[TradeRef], actual: list[TradeRef]) -> float:
    """Fraction of expected trades with a same-name fill within one day."""
    if not expected:
        return 1.0
    hits = sum(_matched(trade, actual) is not None for trade in expected)
    return hits / len(expected)


def fill_gap_bps(expected: TradeRef, actual: TradeRef) -> float:
    """BUY gap is actual/expected − 1, in basis points. SELL flips the sign."""
    gap = (actual.price / expected.price - 1) * 10_000
    if expected.side == "SELL":
        return -gap
    return gap
