"""SQLite repository placeholder.

The file path comes from ``DATABASE_PATH`` (default
``data/auto_trading.sqlite``). Schema creation is not implemented yet.
"""

from __future__ import annotations

from pathlib import Path

from auto_trading.config import get_settings


class SqliteRepository:
    """Local repository. Methods raise until the schema exists."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_settings().database_path

    def record_journal(self, note: str) -> None:
        raise NotImplementedError("SQLite persistence is not implemented yet")

    def record_fill(self, client_id: str, price: float, quantity: float) -> None:
        raise NotImplementedError("SQLite persistence is not implemented yet")
