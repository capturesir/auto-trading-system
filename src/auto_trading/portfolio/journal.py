"""SQLite trade journal plus append-only JSONL and CSV.

``why`` walks fill → intent → signal id → strategy hash. Audit tables reject
later updates.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TradeJournal:
    """Local journal. The database is created on first use."""

    def __init__(self, trading_home: Path) -> None:
        self.home = trading_home
        self.path = trading_home / "data" / "trading.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    strategy_id TEXT,
                    strategy_version INTEGER,
                    spec_hash TEXT,
                    signal_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT,
                    client_id TEXT,
                    symbol TEXT,
                    price REAL,
                    quantity REAL,
                    fee REAL,
                    ts_utc TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit_log is append-only');
                END;
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    UNIQUE (intent_id, seq)
                );
                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    ts_utc TEXT NOT NULL,
                    state TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS order_events_no_update
                BEFORE UPDATE ON order_events
                BEGIN
                    SELECT RAISE(ABORT, 'order_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS order_events_no_delete
                BEFORE DELETE ON order_events
                BEGIN
                    SELECT RAISE(ABORT, 'order_events is append-only');
                END;
                """
            )

    def record_intent(self, intent: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intents (
                    intent_id, strategy_id, strategy_version,
                    spec_hash, signal_id, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO NOTHING
                """,
                (
                    intent["intent_id"],
                    intent.get("strategy_id"),
                    intent.get("strategy_version"),
                    intent.get("spec_hash"),
                    intent.get("signal_id"),
                    json.dumps(intent, ensure_ascii=False),
                ),
            )

    def record_fill(
        self,
        *,
        fill_id: str,
        intent_id: str,
        client_id: str,
        symbol: str,
        price: float,
        quantity: float,
        fee: float | None = None,
        when: datetime | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        row = {
            "fill_id": fill_id,
            "intent_id": intent_id,
            "client_id": client_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "ts_utc": ts,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO executions (
                    fill_id, intent_id, client_id, symbol,
                    price, quantity, fee, ts_utc
                )
                VALUES (
                    :fill_id, :intent_id, :client_id, :symbol,
                    :price, :quantity, :fee, :ts_utc
                )
                """,
                row,
            )
        day = ts[:10].replace("-", "")
        log = self.home / "logs" / f"events_{day}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        export = self.home / "exports" / f"journal_{ts[:7].replace('-', '')}.csv"
        export.parent.mkdir(parents=True, exist_ok=True)
        new_file = not export.exists()
        encoding = "utf-8-sig" if new_file else "utf-8"
        with export.open("a", encoding=encoding, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def audit(self, action: str, detail: dict[str, Any], *, when: datetime | None = None) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts_utc, action, detail_json) VALUES (?, ?, ?)",
                (ts, action, json.dumps(detail, ensure_ascii=False)),
            )

    def create_order(self, *, order_id: str, intent_id: str, seq: int, symbol: str) -> None:
        when = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (order_id, intent_id, seq, symbol, state)
                VALUES (?, ?, ?, ?, 'INTENT_RECEIVED')
                """,
                (order_id, intent_id, seq, symbol),
            )
        self._order_event(order_id, "INTENT_RECEIVED", when)

    def order_state(self, order_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["state"])

    def set_order_state(self, order_id: str, state: str, *, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET state = ? WHERE order_id = ?",
                (state, order_id),
            )
        self._order_event(order_id, state, when)

    def order_events(self, order_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state FROM order_events
                WHERE order_id = ? ORDER BY id
                """,
                (order_id,),
            ).fetchall()
        return [str(row["state"]) for row in rows]

    def unknown_orders(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, order_id FROM orders WHERE state = 'UNKNOWN'"
            ).fetchall()
        return [(str(row["symbol"]), str(row["order_id"])) for row in rows]

    def _order_event(self, order_id: str, state: str, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO order_events (order_id, ts_utc, state, detail_json)
                VALUES (?, ?, ?, '{}')
                """,
                (order_id, when.isoformat(timespec="seconds"), state),
            )

    def why(self, key: str) -> dict[str, Any]:
        """Trace a fill id, client id, or intent id back to the strategy hash."""
        with self._connect() as conn:
            fill = conn.execute(
                """
                SELECT * FROM executions
                WHERE fill_id = ? OR client_id = ? OR intent_id = ?
                ORDER BY ts_utc DESC LIMIT 1
                """,
                (key, key, key),
            ).fetchone()
            if fill is None:
                return {"found": False, "key": key}
            intent = conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (fill["intent_id"],),
            ).fetchone()
        intent_payload = json.loads(intent["payload_json"]) if intent else None
        return {
            "found": True,
            "fill": dict(fill),
            "intent_id": fill["intent_id"],
            "signal_id": None if intent is None else intent["signal_id"],
            "spec_hash": None if intent is None else intent["spec_hash"],
            "strategy_id": None if intent is None else intent["strategy_id"],
            "intent": intent_payload,
        }
