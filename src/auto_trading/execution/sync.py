"""Read-only account sync and a local reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading.broker import BrokerPosition
from auto_trading.risk.gate import GateContext


@dataclass(frozen=True)
class ReconBreak:
    symbol: str
    broker_qty: float
    journal_qty: float
    kind: str = "QTY"
    order_id: str = ""


def reconcile(
    broker_positions: list[BrokerPosition],
    journal_qty: dict[str, float],
    *,
    unknown_orders: list[tuple[str, str]] | None = None,
) -> list[ReconBreak]:
    """Compare broker quantities with the journal. Empty means they match.

    An unresolved ``UNKNOWN`` order is its own break. It blocks that symbol
    only. A quantity mismatch freezes every new order.
    """
    symbols = set(journal_qty) | {row.symbol for row in broker_positions}
    breaks: list[ReconBreak] = []
    broker = {row.symbol: row.quantity for row in broker_positions}
    for symbol in sorted(symbols):
        left = broker.get(symbol, 0.0)
        right = journal_qty.get(symbol, 0.0)
        if abs(left - right) > 1e-9:
            breaks.append(ReconBreak(symbol, left, right))
    for symbol, order_id in unknown_orders or []:
        breaks.append(ReconBreak(symbol, 0.0, 0.0, "UNKNOWN_ORDER", order_id))
    return breaks


def apply_recon(ctx: GateContext, breaks: list[ReconBreak]) -> None:
    """Copy a reconciliation result onto a gate context."""
    if any(row.kind == "QTY" for row in breaks):
        ctx.recon_ok = False
    ctx.unknown_symbols = frozenset(row.symbol for row in breaks if row.kind == "UNKNOWN_ORDER")
