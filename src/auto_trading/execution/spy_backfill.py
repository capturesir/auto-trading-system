"""One-shot backfill for the 2026-10-16 SPY paper fill.

The order was submitted and filled at the paper Gateway, and the journal
never recorded it. Facts below are the ones observed for that fill. The
script copies ``data/trading.db`` before it writes, and ``--undo`` restores
that copy when no later order or execution has been added.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.execution.fills import ingest_broker_fills
from auto_trading.execution.oms import order_ref
from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.paper_only import port_accepted
from auto_trading.portfolio.journal import TradeJournal

INTENT_ID = "2026-10-16-SPY-entry-test10"
STRATEGY_ID = "sig_trend"
EXEC_ID = "00025b49.6ad2ade3.01.01"
PERM_ID = 1145186315
SYMBOL = "SPY"
SIDE = "BUY"
QTY = 10.0
PRICE = 772.28
EXCHANGE = "ARCA"
ACCOUNT = "DUR243736"
CASH_BEFORE = 1_000_000.0
CASH_AFTER = 992_276.20
# Cash drop minus the shares. The Gateway average (~772.38) includes this.
FEE = round(CASH_BEFORE - CASH_AFTER - QTY * PRICE, 2)
FILLED_AT = datetime(2026, 10, 16, 21, 35, 8, tzinfo=ZoneInfo("Asia/Macau"))
SCHEMA = "backfill_spy_20261016/1.0"
BACKUP_NAME = "trading-before-spy-20261016.db"
MANIFEST_NAME = "backfill_spy_20261016.json"


class BackfillError(Exception):
    """The backfill refused to change the journal."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        self.code = code
        super().__init__(message)


def order_id() -> str:
    """``sha256(intent_id + seq)``. The known id starts 04ad62c5 and ends 4501f4."""
    digest = order_ref(INTENT_ID, 1)
    if not (digest.startswith("04ad62c5") and digest.endswith("4501f4")):
        raise BackfillError("computed order id does not match the known SPY order")
    return digest


def fill_payload() -> dict[str, Any]:
    return {
        "exec_id": EXEC_ID,
        "order_ref": order_id(),
        "perm_id": PERM_ID,
        "symbol": SYMBOL,
        "side": SIDE,
        "qty": QTY,
        "price": PRICE,
        "fee": FEE,
        "time": FILLED_AT.astimezone(UTC).isoformat(timespec="seconds"),
        "account": ACCOUNT,
        "exchange": EXCHANGE,
        "strategy_id": STRATEGY_ID,
        "intent_id": INTENT_ID,
    }


def _check_environment() -> None:
    if DU_ACCOUNT.fullmatch(ACCOUNT) is None:
        raise BackfillError("backfill account is not a paper id")
    pin = os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()
    if pin and pin != ACCOUNT:
        raise BackfillError("IBKR_PAPER_ACCOUNT does not match the SPY paper account")
    raw_port = os.environ.get("IB_PORT", os.environ.get("IBKR_PORT", "")).strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise BackfillError("IB port is not a number") from exc
        if not port_accepted(port):
            raise BackfillError("IB port is not a paper port")


def _manifest_path(home: Path) -> Path:
    return home / "state" / MANIFEST_NAME


def _backup_path(home: Path) -> Path:
    return home / "data" / "backups" / BACKUP_NAME


def _read_manifest(home: Path) -> dict[str, Any] | None:
    path = _manifest_path(home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackfillError(f"manifest is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise BackfillError("manifest is not an object")
    return payload


def _write_manifest(home: Path, payload: dict[str, Any]) -> None:
    path = _manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ids(path: Path, table: str, column: str) -> set[str]:
    if not path.is_file():
        return set()
    with sqlite3.connect(path) as conn:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if present is None:
            return set()
        rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}


def _copy_db(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)


def _strategy_qty(home: Path) -> float:
    total = 0.0
    for row in TradeJournal(home).strategy_position_rows():
        ticker = str(row.get("ticker") or "")
        strategy = str(row.get("strategy_id") or "")
        if ticker == SYMBOL and strategy == STRATEGY_ID:
            total += float(row.get("qty") or 0)
    return total


def inspect_plan(home: Path) -> dict[str, Any]:
    """What an apply would change. Does not write."""
    _check_environment()
    db = home / "data" / "trading.db"
    if not db.is_file():
        raise BackfillError(f"journal is missing: {db}", code=2)
    ctx = TradeJournal(home).order_context(order_id())
    if ctx is None:
        raise BackfillError(
            f"order {order_id()} is not in the journal; nothing was written",
            code=2,
        )
    return {
        "order_id": order_id(),
        "intent_id": INTENT_ID,
        "strategy_id": STRATEGY_ID,
        "order_state": ctx["state"],
        "order_qty": ctx["quantity"],
        "execution_exists": TradeJournal(home).has_execution(EXEC_ID),
        "strategy_qty": _strategy_qty(home),
        "exec_id": EXEC_ID,
        "perm_id": PERM_ID,
        "price": PRICE,
        "qty": QTY,
        "fee": FEE,
        "exchange": EXCHANGE,
        "filled_at_utc": FILLED_AT.astimezone(UTC).isoformat(timespec="seconds"),
        "account": ACCOUNT,
    }


def _spy_broker_qty(home: Path) -> float | None:
    path = home / "state" / "account.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("positions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    total = 0.0
    seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "")
        if symbol != SYMBOL:
            continue
        seen = True
        total += float(row.get("qty") or row.get("quantity") or 0)
    return total if seen else None


def _refresh_recon(home: Path, now: datetime) -> dict[str, Any] | None:
    broker = _spy_broker_qty(home)
    if broker is None or broker + 1e-9 < QTY:
        return None
    from auto_trading.execution.services import recon_once

    return recon_once(home, now)


def apply_backfill(home: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Back up the journal, record the fill, and refresh recon when SPY is on the snapshot."""
    moment = now or datetime.now(UTC)
    plan = inspect_plan(home)
    if plan["order_qty"] is not None and abs(float(plan["order_qty"]) - QTY) > 1e-9:
        raise BackfillError(f"journal order quantity is {plan['order_qty']}, expected {QTY:g}")
    db = home / "data" / "trading.db"
    backup = _backup_path(home)
    # Keep the first copy. A later apply must not replace it with an already-filled journal.
    if not backup.is_file():
        _copy_db(db, backup)
    recorded = ingest_broker_fills(home, [fill_payload()], now=moment)
    _write_manifest(
        home,
        {
            "schema_version": SCHEMA,
            "status": "applied",
            "exec_id": EXEC_ID,
            "order_id": order_id(),
            "backup": backup.relative_to(home).as_posix(),
            "applied_at": moment.astimezone(UTC).isoformat(timespec="seconds"),
            "account": ACCOUNT,
        },
    )
    recon = _refresh_recon(home, moment)
    return {"plan": plan, "ingest": recorded, "backup": str(backup), "recon": recon}


def undo_backfill(home: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Restore the pre-apply journal when no later order or execution was added."""
    moment = now or datetime.now(UTC)
    _check_environment()
    manifest = _read_manifest(home)
    if manifest is None:
        raise BackfillError("no backfill manifest; nothing to undo")
    if manifest.get("status") == "undone":
        return {"status": "undone", "restored": False}
    if manifest.get("exec_id") != EXEC_ID:
        raise BackfillError("manifest is for a different fill")
    backup = home / str(manifest.get("backup") or "")
    if not backup.is_file():
        backup = _backup_path(home)
    if not backup.is_file():
        raise BackfillError("backup file is missing; journal was not restored")
    db = home / "data" / "trading.db"
    extra_orders = _ids(db, "orders", "order_id") - _ids(backup, "orders", "order_id")
    extra_fills = _ids(db, "executions", "fill_id") - _ids(backup, "executions", "fill_id")
    extra_fills.discard(EXEC_ID)
    if extra_orders or extra_fills:
        raise BackfillError("journal has orders or executions added after the backup; undo refused")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if sidecar.is_file():
            sidecar.unlink()
    _copy_db(backup, db)
    _write_manifest(
        home,
        {
            **manifest,
            "status": "undone",
            "undone_at": moment.astimezone(UTC).isoformat(timespec="seconds"),
        },
    )
    recon = _refresh_recon(home, moment)
    return {"status": "undone", "restored": True, "backup": str(backup), "recon": recon}
