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
                    payload_json TEXT NOT NULL,
                    trigger_ref_json TEXT,
                    protective_stop_pct REAL,
                    trigger_type TEXT
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
                    intent_type TEXT,
                    protective INTEGER NOT NULL DEFAULT 0,
                    quote_source TEXT,
                    market_data_type INTEGER,
                    quote_data_type TEXT,
                    quote_as_of_utc TEXT,
                    quote_received_at_utc TEXT,
                    delay_sec REAL,
                    UNIQUE (intent_id, seq)
                );
                CREATE TABLE IF NOT EXISTS intent_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT,
                    detail_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS intent_files_no_update
                BEFORE UPDATE ON intent_files
                BEGIN
                    SELECT RAISE(ABORT, 'intent_files is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS intent_files_no_delete
                BEFORE DELETE ON intent_files
                BEGIN
                    SELECT RAISE(ABORT, 'intent_files is append-only');
                END;
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
                CREATE TABLE IF NOT EXISTS risk_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    intent_id TEXT,
                    symbol TEXT,
                    code TEXT,
                    detail_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS risk_checks_no_update
                BEFORE UPDATE ON risk_checks
                BEGIN
                    SELECT RAISE(ABORT, 'risk_checks is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS risk_checks_no_delete
                BEFORE DELETE ON risk_checks
                BEGIN
                    SELECT RAISE(ABORT, 'risk_checks is append-only');
                END;
                CREATE TABLE IF NOT EXISTS hwm_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    hwm REAL NOT NULL,
                    nlv REAL,
                    reason TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS hwm_history_no_update
                BEFORE UPDATE ON hwm_history
                BEGIN
                    SELECT RAISE(ABORT, 'hwm_history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS hwm_history_no_delete
                BEFORE DELETE ON hwm_history
                BEGIN
                    SELECT RAISE(ABORT, 'hwm_history is append-only');
                END;
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    strategy_id TEXT
                );
                CREATE TABLE IF NOT EXISTS strategy_positions (
                    account TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    con_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    qty REAL NOT NULL,
                    avg_cost REAL NOT NULL,
                    root_intent_id TEXT,
                    protective_stop_order_id TEXT,
                    oca_group TEXT,
                    opened_at_utc TEXT,
                    updated_at_utc TEXT,
                    PRIMARY KEY (account, strategy_id, con_id)
                );
                CREATE TRIGGER IF NOT EXISTS anomaly_events_no_update
                BEFORE UPDATE ON anomaly_events
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS anomaly_events_no_delete
                BEFORE DELETE ON anomaly_events
                BEGIN
                    SELECT RAISE(ABORT, 'anomaly_events is append-only');
                END;
                CREATE TABLE IF NOT EXISTS adr_controlled_set (
                    symbol TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS r9_cycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    account TEXT,
                    cycle_no INTEGER,
                    from_state TEXT,
                    to_state TEXT,
                    kind TEXT NOT NULL,
                    risk_date TEXT,
                    nlv_base REAL,
                    old_hwm_base REAL,
                    new_hwm_base REAL,
                    by TEXT,
                    seq INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS r9_cycle_events_no_update
                BEFORE UPDATE ON r9_cycle_events
                BEGIN
                    SELECT RAISE(ABORT, 'r9_cycle_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS r9_cycle_events_no_delete
                BEFORE DELETE ON r9_cycle_events
                BEGIN
                    SELECT RAISE(ABORT, 'r9_cycle_events is append-only');
                END;
                """
            )
            self._create_approvals(conn)
            self._create_ops_tables(conn)
            self._migrate_orders(conn)
            self._migrate_intents(conn)
            self._migrate_anomaly(conn)

    def _create_approvals(self, conn: sqlite3.Connection) -> None:
        """Approvals table. The check value is filled from the mode constant."""
        from auto_trading.execution.approval_mode import approvals_check_sql

        check = approvals_check_sql()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts_utc TEXT NOT NULL,"
            "order_id TEXT NOT NULL,"
            "order_hash TEXT NOT NULL,"
            "status TEXT NOT NULL,"
            "detail_json TEXT NOT NULL,"
            "approver TEXT,"
            "channel TEXT,"
            "attempt INTEGER,"
            "approval_price REAL,"
            "r0_check_json TEXT,"
            f"{check})"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS approvals_no_update
            BEFORE UPDATE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approvals is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS approvals_no_delete
            BEFORE DELETE ON approvals
            BEGIN
                SELECT RAISE(ABORT, 'approvals is append-only');
            END;
            """
        )

    def _create_ops_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS anomaly_order_counts (
                risk_date TEXT NOT NULL,
                order_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY (risk_date, order_id)
            );
            CREATE TABLE IF NOT EXISTS b_alerts (
                alert_id TEXT PRIMARY KEY,
                dedup_key TEXT,
                rule_id TEXT,
                ticker TEXT,
                severity TEXT,
                received_at_utc TEXT,
                forwarded_at_utc TEXT,
                status TEXT,
                outbox_file TEXT,
                source TEXT
            );
            CREATE TABLE IF NOT EXISTS critical_events (
                alert_id TEXT PRIMARY KEY,
                event_id TEXT,
                kind TEXT,
                rule_id TEXT,
                reason_code TEXT,
                created_at_utc TEXT,
                pending_file TEXT,
                outbox_file TEXT,
                forwarded_at_utc TEXT,
                status TEXT
            );
            """
        )

    def _migrate_orders(self, conn: sqlite3.Connection) -> None:
        """Add quote and protective columns on a journal created before them."""
        present = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        additions = {
            "intent_type": "ALTER TABLE orders ADD COLUMN intent_type TEXT",
            "protective": "ALTER TABLE orders ADD COLUMN protective INTEGER NOT NULL DEFAULT 0",
            "quote_source": "ALTER TABLE orders ADD COLUMN quote_source TEXT",
            "market_data_type": "ALTER TABLE orders ADD COLUMN market_data_type INTEGER",
            "quote_data_type": "ALTER TABLE orders ADD COLUMN quote_data_type TEXT",
            "quote_as_of_utc": "ALTER TABLE orders ADD COLUMN quote_as_of_utc TEXT",
            "quote_received_at_utc": "ALTER TABLE orders ADD COLUMN quote_received_at_utc TEXT",
            "delay_sec": "ALTER TABLE orders ADD COLUMN delay_sec REAL",
        }
        for name, statement in additions.items():
            if name not in present:
                conn.execute(statement)

    def _migrate_intents(self, conn: sqlite3.Connection) -> None:
        present = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
        additions = {
            "trigger_ref_json": "ALTER TABLE intents ADD COLUMN trigger_ref_json TEXT",
            "protective_stop_pct": "ALTER TABLE intents ADD COLUMN protective_stop_pct REAL",
            "trigger_type": "ALTER TABLE intents ADD COLUMN trigger_type TEXT",
        }
        for name, statement in additions.items():
            if name not in present:
                conn.execute(statement)

    def _migrate_anomaly(self, conn: sqlite3.Connection) -> None:
        present = {row[1] for row in conn.execute("PRAGMA table_info(anomaly_events)")}
        if "strategy_id" not in present:
            conn.execute("ALTER TABLE anomaly_events ADD COLUMN strategy_id TEXT")

    def record_intent(
        self,
        intent: dict[str, Any],
        *,
        trigger_ref_raw: str | None = None,
        protective_stop_pct: float | None = None,
        trigger_type: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intents (
                    intent_id, strategy_id, strategy_version,
                    spec_hash, signal_id, payload_json,
                    trigger_ref_json, protective_stop_pct, trigger_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO NOTHING
                """,
                (
                    intent["intent_id"],
                    intent.get("strategy_id"),
                    intent.get("strategy_version"),
                    intent.get("spec_hash"),
                    intent.get("signal_id"),
                    json.dumps(intent, ensure_ascii=False),
                    trigger_ref_raw,
                    protective_stop_pct,
                    trigger_type,
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

    def create_order(
        self,
        *,
        order_id: str,
        intent_id: str,
        seq: int,
        symbol: str,
        intent_type: str | None = None,
        protective: bool = False,
        quote_source: str | None = None,
        market_data_type: int | None = None,
        quote_data_type: str | None = None,
        quote_as_of_utc: str | None = None,
        quote_received_at_utc: str | None = None,
        delay_sec: float | None = None,
    ) -> None:
        when = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, intent_id, seq, symbol, state, intent_type, protective,
                    quote_source, market_data_type, quote_data_type,
                    quote_as_of_utc, quote_received_at_utc, delay_sec
                )
                VALUES (?, ?, ?, ?, 'INTENT_RECEIVED', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    intent_id,
                    seq,
                    symbol,
                    intent_type,
                    int(protective),
                    quote_source,
                    market_data_type,
                    quote_data_type,
                    quote_as_of_utc,
                    quote_received_at_utc,
                    delay_sec,
                ),
            )
        self._order_event(order_id, "INTENT_RECEIVED", when)

    def cancel_unfilled_orders(self, *, keep_exits: bool = False) -> list[str]:
        """Request cancellation of working orders that are not protective stops.

        ``keep_exits`` leaves resting EXIT and REDUCE orders in place. Protective
        stops are always left in place.
        """
        working = (
            "INTENT_RECEIVED",
            "DRAFT",
            "PENDING_APPROVAL",
            "APPROVED",
            "SUBMITTING",
            "SUBMITTED",
            "PARTIALLY_FILLED",
        )
        placeholders = ", ".join("?" for _ in working)
        extra = ""
        if keep_exits:
            extra = " AND UPPER(COALESCE(intent_type, '')) NOT IN ('EXIT', 'REDUCE')"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT order_id FROM orders
                WHERE COALESCE(protective, 0) = 0 AND state IN ({placeholders}){extra}
                """,
                working,
            ).fetchall()
        when = datetime.now(UTC)
        cancelled: list[str] = []
        for row in rows:
            order_id = str(row["order_id"])
            self.set_order_state(order_id, "CANCEL_REQUESTED", when=when)
            cancelled.append(order_id)
        return cancelled

    def record_intent_file(
        self,
        *,
        path: str,
        content_hash: str | None = None,
        detail: dict[str, Any] | None = None,
        when: datetime | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intent_files (ts_utc, path, content_hash, detail_json)
                VALUES (?, ?, ?, ?)
                """,
                (ts, path, content_hash, json.dumps(detail or {}, ensure_ascii=False)),
            )

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

    def record_risk_check(
        self,
        *,
        symbol: str,
        code: str | None,
        intent_id: str | None = None,
        detail: dict[str, Any] | None = None,
        when: datetime | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_checks (ts_utc, intent_id, symbol, code, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, intent_id, symbol, code, json.dumps(detail or {}, ensure_ascii=False)),
            )

    def record_approval(
        self,
        *,
        order_id: str,
        order_hash: str,
        status: str,
        detail: dict[str, Any] | None = None,
        when: datetime | None = None,
        approver: str | None = None,
        channel: str | None = None,
        attempt: int | None = None,
        approval_price: float | None = None,
        r0_check: dict[str, Any] | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    ts_utc, order_id, order_hash, status, detail_json,
                    approver, channel, attempt, approval_price, r0_check_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    order_id,
                    order_hash,
                    status,
                    json.dumps(detail or {}, ensure_ascii=False),
                    approver,
                    channel,
                    attempt,
                    approval_price,
                    None if r0_check is None else json.dumps(r0_check, ensure_ascii=False),
                ),
            )
        self.audit("approval", {"order_id": order_id, "status": status}, when=when)

    def note_counted_anomaly(self, risk_date: str, order_id: str, kind: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO anomaly_order_counts (risk_date, order_id, kind)
                VALUES (?, ?, ?)
                """,
                (risk_date, order_id, kind),
            )
            changed = conn.execute("SELECT changes()").fetchone()
        return int(changed[0]) == 1

    def counted_anomalies(self, risk_date: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM anomaly_order_counts WHERE risk_date = ?",
                (risk_date,),
            ).fetchone()
        return int(row["n"])

    def clear_counted_anomalies(self, risk_date: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM anomaly_order_counts WHERE risk_date = ?", (risk_date,))

    def alert_seen(self, alert_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM b_alerts WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        return row is not None

    def dedup_forwarded(self, dedup_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM b_alerts
                WHERE dedup_key = ? AND status = 'forwarded'
                """,
                (dedup_key,),
            ).fetchone()
        return row is not None

    def record_alert(
        self,
        *,
        alert_id: str,
        dedup_key: str,
        source: str,
        status: str,
        when: datetime,
        rule_id: str = "",
        ticker: str = "",
        severity: str = "",
        outbox_file: str = "",
    ) -> None:
        ts = when.astimezone(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO b_alerts (
                    alert_id, dedup_key, rule_id, ticker, severity,
                    received_at_utc, forwarded_at_utc, status, outbox_file, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    dedup_key,
                    rule_id,
                    ticker,
                    severity,
                    ts,
                    ts if status == "forwarded" else None,
                    status,
                    outbox_file,
                    source,
                ),
            )

    def record_critical_event(
        self,
        *,
        alert_id: str,
        event_id: str,
        kind: str,
        rule_id: str,
        reason_code: str,
        created_at_utc: str,
        pending_file: str,
        outbox_file: str,
        status: str,
        when: datetime,
    ) -> None:
        ts = when.astimezone(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO critical_events (
                    alert_id, event_id, kind, rule_id, reason_code,
                    created_at_utc, pending_file, outbox_file, forwarded_at_utc, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    event_id,
                    kind,
                    rule_id,
                    reason_code,
                    created_at_utc,
                    pending_file,
                    outbox_file,
                    ts,
                    status,
                ),
            )

    def summary_fills(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.order_id, o.symbol, o.quote_data_type, o.market_data_type,
                       o.delay_sec, e.price, e.quantity, e.intent_id
                FROM executions e
                JOIN orders o ON o.intent_id = e.intent_id
                WHERE e.ts_utc >= ? AND e.ts_utc < ?
                """,
                (
                    start.astimezone(UTC).isoformat(timespec="seconds"),
                    end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary_rejections(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT intent_id, symbol, code, detail_json
                FROM risk_checks
                WHERE code IS NOT NULL AND ts_utc >= ? AND ts_utc < ?
                """,
                (
                    start.astimezone(UTC).isoformat(timespec="seconds"),
                    end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchall()
        found = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            found.append(item)
        return found

    def summary_submitted(self, start: datetime, end: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT order_id) AS n FROM order_events
                WHERE state = 'SUBMITTED' AND ts_utc >= ? AND ts_utc < ?
                """,
                (
                    start.astimezone(UTC).isoformat(timespec="seconds"),
                    end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchone()
        return int(row["n"])

    def summary_approvals(self, start: datetime, end: datetime) -> dict[str, int]:
        from auto_trading.execution.approval_mode import is_auto_approver

        start_s = start.astimezone(UTC).isoformat(timespec="seconds")
        end_s = end.astimezone(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT approver, attempt FROM approvals
                WHERE ts_utc >= ? AND ts_utc < ?
                """,
                (start_s, end_s),
            ).fetchall()
            chase = conn.execute(
                """
                SELECT COUNT(*) AS n FROM risk_checks
                WHERE code = 'CHASE_REAPPROVAL_EXCEEDED' AND ts_utc >= ? AND ts_utc < ?
                """,
                (start_s, end_s),
            ).fetchone()
            expired = conn.execute(
                """
                SELECT COUNT(*) AS n FROM risk_checks
                WHERE code = 'APPROVAL_EXPIRED' AND ts_utc >= ? AND ts_utc < ?
                """,
                (start_s, end_s),
            ).fetchone()
        auto = sum(1 for row in rows if is_auto_approver(row["approver"]) and row["attempt"] == 1)
        reapproved = sum(1 for row in rows if row["attempt"] == 2)
        user = sum(1 for row in rows if row["approver"] == "user")
        return {
            "auto": auto,
            "reapproved": reapproved,
            "user": user,
            "chase": int(chase["n"]),
            "expired": int(expired["n"]),
        }

    def record_hwm(
        self,
        *,
        hwm: float,
        nlv: float | None,
        reason: str,
        detail: dict[str, Any] | None = None,
        when: datetime | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hwm_history (ts_utc, hwm, nlv, reason, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, hwm, nlv, reason, json.dumps(detail or {}, ensure_ascii=False)),
            )

    def record_anomaly(
        self,
        *,
        kind: str,
        detail: dict[str, Any] | None = None,
        when: datetime | None = None,
        strategy_id: str | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO anomaly_events (ts_utc, kind, detail_json, strategy_id)
                VALUES (?, ?, ?, ?)
                """,
                (ts, kind, json.dumps(detail or {}, ensure_ascii=False), strategy_id),
            )

    def append_r9_event(
        self,
        *,
        account: str,
        cycle_no: int,
        from_state: str,
        to_state: str,
        kind: str,
        risk_date: str | None,
        nlv_base: float | None,
        old_hwm_base: float | None,
        new_hwm_base: float | None,
        by: str | None,
        seq: int,
        when: datetime | None = None,
    ) -> None:
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO r9_cycle_events (
                    ts_utc, account, cycle_no, from_state, to_state, kind,
                    risk_date, nlv_base, old_hwm_base, new_hwm_base, by, seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    account,
                    cycle_no,
                    from_state,
                    to_state,
                    kind,
                    risk_date,
                    nlv_base,
                    old_hwm_base,
                    new_hwm_base,
                    by,
                    seq,
                ),
            )

    def last_r9_seq(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(seq) AS seq FROM r9_cycle_events").fetchone()
        if row is None or row["seq"] is None:
            return None
        return int(row["seq"])

    def r9_event_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM r9_cycle_events").fetchone()
        return int(row["n"])

    def order_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
        return int(row["n"])

    def apply_strategy_fill(
        self,
        *,
        account: str,
        strategy_id: str,
        con_id: int,
        ticker: str,
        side: str,
        qty: float,
        price: float,
        root_intent_id: str | None,
        when: datetime,
    ) -> None:
        """Update one strategy row from a fill, or set ``_manual`` outright."""
        ts = when.isoformat(timespec="seconds")
        oca = None if root_intent_id is None else f"{strategy_id}:{root_intent_id}"
        with self._connect() as conn:
            current = conn.execute(
                """
                SELECT qty, avg_cost, opened_at_utc, root_intent_id
                FROM strategy_positions
                WHERE account = ? AND strategy_id = ? AND con_id = ?
                """,
                (account, strategy_id, con_id),
            ).fetchone()
            if side == "SET":
                new_qty = qty
                avg = float(current["avg_cost"]) if current else 0.0
                opened = ts if current is None else current["opened_at_utc"]
                root = root_intent_id
            elif current is None:
                signed = qty if side == "BUY" else -qty
                new_qty = signed
                avg = price
                opened = ts
                root = root_intent_id
            else:
                old_qty = float(current["qty"])
                old_avg = float(current["avg_cost"])
                if side == "BUY":
                    new_qty = old_qty + qty
                    avg = ((old_qty * old_avg) + (qty * price)) / new_qty if new_qty else old_avg
                else:
                    new_qty = old_qty - qty
                    avg = old_avg
                opened = current["opened_at_utc"]
                root = current["root_intent_id"] or root_intent_id
            conn.execute(
                """
                INSERT INTO strategy_positions (
                    account, strategy_id, con_id, ticker, qty, avg_cost,
                    root_intent_id, protective_stop_order_id, oca_group,
                    opened_at_utc, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(account, strategy_id, con_id) DO UPDATE SET
                    ticker = excluded.ticker,
                    qty = excluded.qty,
                    avg_cost = excluded.avg_cost,
                    root_intent_id = excluded.root_intent_id,
                    oca_group = excluded.oca_group,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (account, strategy_id, con_id, ticker, new_qty, avg, root, oca, opened, ts),
            )

    def set_protective_stop(
        self,
        *,
        account: str,
        strategy_id: str,
        con_id: int,
        order_id: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE strategy_positions
                SET protective_stop_order_id = ?
                WHERE account = ? AND strategy_id = ? AND con_id = ?
                """,
                (order_id, account, strategy_id, con_id),
            )

    def strategy_position_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_positions ORDER BY ticker, strategy_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def quantity_by_ticker(self) -> dict[str, float]:
        """Net journal quantity per ticker. This is not the broker snapshot."""
        totals: dict[str, float] = {}
        for row in self.strategy_position_rows():
            ticker = str(row["ticker"])
            totals[ticker] = totals.get(ticker, 0.0) + float(row["qty"])
        return {ticker: qty for ticker, qty in totals.items() if abs(qty) > 1e-9}

    def intent_in_flight(self, intent_id: str) -> bool:
        """True once this intent has been journaled for send."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM orders
                WHERE intent_id = ?
                  AND state IN (
                    'SUBMITTING', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED'
                  )
                """,
                (intent_id,),
            ).fetchone()
        return row is not None

    def working_orders(self) -> list[tuple[str, str]]:
        """Orders the broker should still be working: ``(order_id, symbol)``."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT order_id, symbol FROM orders
                WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'CANCEL_REQUESTED')
                """
            ).fetchall()
        return [(str(row["order_id"]), str(row["symbol"])) for row in rows]

    def strategy_pnl(self) -> list[dict[str, Any]]:
        """Cash by strategy. A SELL adds proceeds and a BUY subtracts cost."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.strategy_id AS strategy_id,
                       i.payload_json AS payload_json,
                       e.symbol AS ticker,
                       e.price AS price,
                       e.quantity AS quantity
                FROM executions e
                JOIN intents i ON i.intent_id = e.intent_id
                """
            ).fetchall()
        totals: dict[tuple[str, str], float] = {}
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            side = str(payload.get("side") or "BUY")
            signed = float(row["quantity"]) * float(row["price"])
            if side == "BUY":
                signed = -signed
            key = (str(row["strategy_id"]), str(row["ticker"]))
            totals[key] = totals.get(key, 0.0) + signed
        return [
            {"strategy_id": strategy_id, "ticker": ticker, "pnl": cash}
            for (strategy_id, ticker), cash in sorted(totals.items())
        ]

    def backup(self, dest: Path) -> None:
        """Write a read-only snapshot. Callers query the copy, not this file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with self._connect() as source, sqlite3.connect(dest) as target:
            source.backup(target)

    def anomalies_for(self, strategy_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM anomaly_events WHERE strategy_id = ? ORDER BY id",
                (strategy_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remember_controlled(self, symbols: set[str], *, when: datetime | None = None) -> None:
        """Replace the last controlled ADR set. Used when the map is valid."""
        ts = (when or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("DELETE FROM adr_controlled_set")
            conn.executemany(
                "INSERT INTO adr_controlled_set (symbol, recorded_at) VALUES (?, ?)",
                [(symbol, ts) for symbol in sorted(symbols)],
            )

    def last_controlled(self) -> frozenset[str]:
        """Symbols stored the last time the ADR map loaded."""
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol FROM adr_controlled_set").fetchall()
        return frozenset(str(row["symbol"]) for row in rows)

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
        traced = {
            "found": True,
            "fill": dict(fill),
            "intent_id": fill["intent_id"],
            "signal_id": None if intent is None else intent["signal_id"],
            "spec_hash": None if intent is None else intent["spec_hash"],
            "strategy_id": None if intent is None else intent["strategy_id"],
            "intent": intent_payload,
        }
        if intent is None:
            return traced
        raw = intent["trigger_ref_json"]
        traced["trigger_ref_json"] = raw
        traced["trigger_type"] = intent["trigger_type"]
        traced["rationale"] = None if intent_payload is None else intent_payload.get("rationale")
        if intent["trigger_type"] == "market" and raw:
            from auto_trading.execution.trigger_ref import fixed_rationale, pool_status

            parsed = json.loads(raw)
            detail, ok = pool_status(self.home, parsed, str(intent_payload.get("ticker") or ""))
            archive = self.home / "data" / "universe" / "archive" / f"{parsed['pool_id']}.csv"
            traced["signal_id"] = None
            traced["template_rationale"] = fixed_rationale(parsed)
            traced["pool_archive"] = str(archive) if archive.is_file() else None
            traced["pool_hash_check"] = detail
            traced["pool_hash_ok"] = ok
        return traced
