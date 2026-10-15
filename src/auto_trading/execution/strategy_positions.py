"""Several strategies may hold one contract. Exits touch only their own stop.

Account exposure stays the sum. A strategy may sell only its own quantity,
and working sells plus this order cannot exceed the account position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from auto_trading.portfolio.journal import TradeJournal

MANUAL = "_manual"


@dataclass
class WorkingSell:
    strategy_id: str
    order_id: str
    qty: float
    protective: bool = False


def book_fill(
    journal: TradeJournal,
    *,
    account: str,
    strategy_id: str,
    con_id: int,
    ticker: str,
    side: str,
    qty: float,
    price: float,
    intent_id: str,
    parent_intent_id: str | None = None,
    when: datetime | None = None,
) -> None:
    """Attribute one fill. Unknown or manual fills use ``_manual``."""
    moment = when or datetime.now(UTC)
    root = intent_id if parent_intent_id is None else parent_intent_id
    journal.apply_strategy_fill(
        account=account,
        strategy_id=strategy_id or MANUAL,
        con_id=con_id,
        ticker=ticker,
        side=side,
        qty=qty,
        price=price,
        root_intent_id=root,
        when=moment,
    )


def sums_by_ticker(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        ticker = str(row["ticker"])
        totals[ticker] = totals.get(ticker, 0.0) + float(row["qty"])
    return totals


def quantity_breaks(
    rows: list[dict[str, Any]], broker_qty: dict[str, float]
) -> list[tuple[str, float, float]]:
    """Tickers whose strategy sum does not equal the broker position."""
    totals = sums_by_ticker(rows)
    symbols = set(totals) | set(broker_qty)
    breaks: list[tuple[str, float, float]] = []
    for symbol in sorted(symbols):
        left = float(broker_qty.get(symbol, 0.0))
        right = float(totals.get(symbol, 0.0))
        if abs(left - right) > 1e-9:
            breaks.append((symbol, left, right))
    return breaks


def ack_manual(
    journal: TradeJournal,
    *,
    account: str,
    broker_qty: dict[str, float],
    con_ids: dict[str, int],
    when: datetime | None = None,
) -> None:
    """Give the broker-minus-strategies gap to ``_manual`` and clear the freeze."""
    moment = when or datetime.now(UTC)
    rows = journal.strategy_position_rows()
    totals = sums_by_ticker(rows)
    symbols = set(totals) | set(broker_qty)
    for symbol in sorted(symbols):
        others = sum(
            float(row["qty"])
            for row in rows
            if row["ticker"] == symbol and row["strategy_id"] != MANUAL
        )
        gap = float(broker_qty.get(symbol, 0.0)) - others
        journal.apply_strategy_fill(
            account=account,
            strategy_id=MANUAL,
            con_id=con_ids.get(symbol, 0),
            ticker=symbol,
            side="SET",
            qty=gap,
            price=0.0,
            root_intent_id=None,
            when=moment,
        )


def prepare_exit(
    *,
    strategy_id: str,
    position_qty: float,
    exit_qty: float,
    stops: list[WorkingSell],
) -> dict[str, Any]:
    """Reduce or cancel this strategy's stop before the exit is sent."""
    if exit_qty > position_qty + 1e-9:
        return {"code": "R20"}
    own = [row for row in stops if row.strategy_id == strategy_id and row.protective]
    others = [row for row in stops if row.strategy_id != strategy_id]
    remaining = position_qty - exit_qty
    action = "cancel" if remaining <= 1e-9 else "reduce"
    return {
        "code": None,
        "stop_action": action,
        "stop_qty": remaining,
        "own_order_id": None if not own else own[0].order_id,
        "others": [(row.strategy_id, row.qty, row.order_id) for row in others],
    }


def sell_exposure(stops: list[WorkingSell], this_qty: float) -> float:
    """Working sells, including protective stops, plus the order under check."""
    return sum(row.qty for row in stops) + this_qty


def rehang_stop(position_qty: float, filled_exit_qty: float) -> float:
    """Quantity of the protective stop after the exit finishes."""
    return max(position_qty - filled_exit_qty, 0.0)
