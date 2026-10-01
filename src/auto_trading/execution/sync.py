"""Read-only account sync and a local reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading.broker import BrokerPosition


@dataclass(frozen=True)
class ReconBreak:
    symbol: str
    broker_qty: float
    journal_qty: float


def reconcile(
    broker_positions: list[BrokerPosition],
    journal_qty: dict[str, float],
) -> list[ReconBreak]:
    """Compare broker quantities with the journal. Empty means they match."""
    symbols = set(journal_qty) | {row.symbol for row in broker_positions}
    breaks: list[ReconBreak] = []
    broker = {row.symbol: row.quantity for row in broker_positions}
    for symbol in sorted(symbols):
        left = broker.get(symbol, 0.0)
        right = journal_qty.get(symbol, 0.0)
        if abs(left - right) > 1e-9:
            breaks.append(ReconBreak(symbol, left, right))
    return breaks
