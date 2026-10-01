"""Persistence port.

Callers depend on this protocol. The first implementation is a local
SQLite file; nothing here opens a network database.
"""

from __future__ import annotations

from typing import Protocol


class Repository(Protocol):
    def record_journal(self, note: str) -> None: ...

    def record_fill(self, client_id: str, price: float, quantity: float) -> None: ...
