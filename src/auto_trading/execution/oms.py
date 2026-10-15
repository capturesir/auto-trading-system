"""Order identity and the order state machine.

``order_id`` is ``sha256(intent_id + seq)`` and is stored as the broker
order reference. The database unique index rejects a second insert.
``UNKNOWN`` stays unresolved until a later transition names a real state,
and while it does that symbol cannot take another order.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

from auto_trading.portfolio.journal import TradeJournal

TRANSITIONS: dict[str, frozenset[str]] = {
    "INTENT_RECEIVED": frozenset({"INTENT_REJECTED", "DRAFT"}),
    "DRAFT": frozenset({"RISK_REJECTED", "PENDING_APPROVAL"}),
    "PENDING_APPROVAL": frozenset({"APPROVED", "DECLINED", "APPROVAL_EXPIRED"}),
    "APPROVED": frozenset({"SUBMITTING", "RISK_REJECTED", "REAPPROVAL_REQUIRED"}),
    "REAPPROVAL_REQUIRED": frozenset({"PENDING_APPROVAL", "RISK_REJECTED"}),
    "SUBMITTING": frozenset({"SUBMITTED", "BROKER_REJECTED", "UNKNOWN"}),
    "SUBMITTED": frozenset(
        {
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCEL_REQUESTED",
            "EXPIRED",
            "BROKER_REJECTED",
            "UNKNOWN",
        }
    ),
    "PARTIALLY_FILLED": frozenset(
        {"PARTIALLY_FILLED", "FILLED", "CANCEL_REQUESTED", "EXPIRED", "UNKNOWN"}
    ),
    "CANCEL_REQUESTED": frozenset({"CANCELLED", "FILLED", "PARTIALLY_FILLED", "UNKNOWN"}),
    "UNKNOWN": frozenset(
        {
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "EXPIRED",
            "BROKER_REJECTED",
        }
    ),
}


class IllegalTransition(Exception):
    """The state machine does not allow this edge."""


class OrderRefError(Exception):
    """The idempotency key is already in the journal."""

    def __init__(self) -> None:
        self.code = "R12"
        super().__init__(self.code)


def order_ref(intent_id: str, seq: int) -> str:
    """IB ``orderRef``. The same intent and sequence always hash the same."""
    return hashlib.sha256(f"{intent_id}{seq}".encode()).hexdigest()


class OrderMachine:
    """Persists one order row and an append-only event per transition."""

    def __init__(self, journal: TradeJournal) -> None:
        self.journal = journal

    def create(self, *, intent_id: str, symbol: str, seq: int = 1) -> str:
        """Insert ``INTENT_RECEIVED``. A repeated key raises :class:`OrderRefError`."""
        order_id = order_ref(intent_id, seq)
        try:
            self.journal.create_order(
                order_id=order_id,
                intent_id=intent_id,
                seq=seq,
                symbol=symbol,
            )
        except sqlite3.IntegrityError as exc:
            raise OrderRefError() from exc
        return order_id

    def transition(self, order_id: str, new_state: str, *, when: datetime | None = None) -> str:
        current = self.journal.order_state(order_id)
        if current is None:
            raise IllegalTransition(f"missing order {order_id}")
        allowed = TRANSITIONS.get(current, frozenset())
        if new_state not in allowed:
            raise IllegalTransition(f"{current} -> {new_state}")
        self.journal.set_order_state(order_id, new_state, when=when or datetime.now(UTC))
        return new_state

    def unknown_symbols(self) -> frozenset[str]:
        return frozenset(symbol for symbol, _order_id in self.journal.unknown_orders())
