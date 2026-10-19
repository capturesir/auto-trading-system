"""Record IB executions through the journal.

The submit loop pulls Gateway executions. The OMS loop reads the same
snapshot from ``state/executions.json``. Both call :func:`ingest_broker_fills`,
which writes :meth:`TradeJournal.record_fill`, moves the order to
``FILLED`` or ``PARTIALLY_FILLED``, and :func:`book_fill` into the strategy
book. A repeated poll of the same ``execId`` does not book again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_trading.execution.oms import IllegalTransition, OrderMachine
from auto_trading.execution.strategy_positions import book_fill
from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.portfolio.journal import TradeJournal

log = logging.getLogger(__name__)

EXECUTIONS_SCHEMA = "executions/1.0"
_FEE_ABS_MAX = 1_000_000.0
_QTY_EPS = 1e-9


def executions_path(home: Path) -> Path:
    return home / "state" / "executions.json"


def write_execution_snapshot(home: Path, now: datetime, rows: list[dict[str, Any]]) -> None:
    """Atomically publish the latest broker executions for the OMS loop."""
    from auto_trading.alerts.holdings_state import atomic_write_json

    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    atomic_write_json(
        executions_path(home),
        {
            "schema_version": EXECUTIONS_SCHEMA,
            "at": moment.astimezone(UTC).isoformat(timespec="seconds"),
            "executions": rows,
        },
    )


def read_execution_snapshot(home: Path) -> list[dict[str, Any]]:
    path = executions_path(home)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("executions")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _paper_account(value: str) -> bool:
    return DU_ACCOUNT.fullmatch(value.strip()) is not None


def _fee(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) > _FEE_ABS_MAX:
        return None
    return number


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return None
    return number


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _side(value: Any) -> str:
    text = _text(value).upper()
    if text in {"BUY", "BOT"}:
        return "BUY"
    if text in {"SELL", "SLD"}:
        return "SELL"
    return ""


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _from_mapping(raw: dict[str, Any]) -> dict[str, Any] | None:
    nested = raw.get("execution") if isinstance(raw.get("execution"), dict) else None
    contract = raw.get("contract") if isinstance(raw.get("contract"), dict) else None
    report = raw.get("commissionReport") if isinstance(raw.get("commissionReport"), dict) else None
    source = dict(raw)
    if nested:
        source.update(nested)
    if contract:
        for key in ("symbol", "conId", "con_id"):
            if key in contract and key not in source:
                source[key] = contract[key]
    exec_id = _text(_first(source, "exec_id", "execId", "fill_id"))
    if not exec_id:
        return None
    qty_raw = _first(source, "qty", "quantity", "shares")
    price_raw = _first(source, "price")
    try:
        qty = float(qty_raw)
        price = float(price_raw)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or price <= 0:
        return None
    fee_raw = _first(source, "fee", "commission")
    if fee_raw is None and report is not None:
        fee_raw = report.get("commission")
    when = _as_utc(_first(source, "time", "when", "ts", "ts_utc"))
    perm = _optional_int(_first(source, "perm_id", "permId"))
    con = _optional_int(_first(source, "con_id", "conId")) or 0
    cum_raw = _first(source, "cum_qty", "cumQty")
    try:
        cum_qty = None if cum_raw is None else float(cum_raw)
    except (TypeError, ValueError):
        cum_qty = None
    return {
        "exec_id": exec_id,
        "order_ref": _text(_first(source, "order_ref", "orderRef", "client_id")),
        "perm_id": perm,
        "symbol": _text(_first(source, "symbol")),
        "side": _side(_first(source, "side")),
        "qty": qty,
        "price": price,
        "fee": _fee(fee_raw),
        "time": None if when is None else when.isoformat(timespec="seconds"),
        "account": _text(_first(source, "account", "acctNumber")),
        "con_id": con,
        "exchange": _text(_first(source, "exchange")),
        "cum_qty": cum_qty,
        "intent_id": _text(_first(source, "intent_id")),
        "strategy_id": _text(_first(source, "strategy_id")),
    }


def normalize_execution(raw: Any) -> dict[str, Any] | None:
    """One broker fill as a plain dict. ``None`` when the id or price is missing."""
    if isinstance(raw, dict):
        return _from_mapping(raw)
    execution = getattr(raw, "execution", None)
    contract = getattr(raw, "contract", None)
    report = getattr(raw, "commissionReport", None)
    if execution is None and contract is None:
        return None
    payload: dict[str, Any] = {}
    if execution is not None:
        payload.update(
            {
                "execId": getattr(execution, "execId", None),
                "orderRef": getattr(execution, "orderRef", None),
                "permId": getattr(execution, "permId", None),
                "side": getattr(execution, "side", None),
                "shares": getattr(execution, "shares", None),
                "price": getattr(execution, "price", None),
                "acctNumber": getattr(execution, "acctNumber", None),
                "exchange": getattr(execution, "exchange", None),
                "cumQty": getattr(execution, "cumQty", None),
                "time": getattr(execution, "time", None) or getattr(raw, "time", None),
            }
        )
    if contract is not None:
        payload["symbol"] = getattr(contract, "symbol", None)
        payload["conId"] = getattr(contract, "conId", None)
    if report is not None:
        payload["commission"] = getattr(report, "commission", None)
    return _from_mapping(payload)


def _execution_filter(account: str) -> Any:
    from ib_async import ExecutionFilter

    filt = ExecutionFilter()
    filt.clientId = 0
    if account:
        filt.acctCode = account
    return filt


async def read_ib_executions(ib: Any, account: str) -> list[dict[str, Any]]:
    """Executions for every API client on this paper account.

    IB's default filter is the current ``clientId``. Orders are placed on
    client 11, so the snapshot asks for client 0 (all clients).
    """
    try:
        filt = _execution_filter(account)
    except Exception as exc:
        log.warning("execution filter unavailable: %s", type(exc).__name__)
        return []
    req = getattr(ib, "reqExecutionsAsync", None)
    try:
        if callable(req):
            rows = await req(filt)
        else:
            sync = getattr(ib, "reqExecutions", None)
            if not callable(sync):
                return []
            sync(filt)
            fills = getattr(ib, "fills", None)
            rows = list(fills()) if callable(fills) else []
    except Exception as exc:
        log.warning("execution request failed: %s: %s", type(exc).__name__, exc)
        return []
    found: list[dict[str, Any]] = []
    for row in rows or []:
        item = normalize_execution(row)
        if item is not None:
            found.append(item)
    return found


def pull_executions() -> list[dict[str, Any]]:
    """One paper Gateway connection. Caller checks ``TRADING_GATEWAY_CONNECT``."""
    import asyncio

    from auto_trading.config import get_settings
    from auto_trading.execution.ibkr import PaperGateway

    client_id = int(os.environ.get("IB_CLIENT_ID", "11"))
    gateway = PaperGateway(get_settings(), client_id=client_id)
    return asyncio.run(gateway.snapshot_executions())


def sync_fills(
    home: Path,
    now: datetime,
    *,
    fills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pull when the unit may connect, otherwise read the snapshot, then record.

    An explicit ``fills`` list is used as-is and does not open a socket.
    """
    source = fills
    if source is None and os.environ.get("TRADING_GATEWAY_CONNECT", "").strip() == "1":
        try:
            source = pull_executions()
        except Exception as exc:
            log.warning("fill pull failed: %s: %s", type(exc).__name__, exc)
            source = None
        else:
            write_execution_snapshot(home, now, source)
    if source is None:
        source = read_execution_snapshot(home)
    return ingest_broker_fills(home, source, now=now)


def _lock(home: Path):
    folder = home / "state"
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / "fills.lock").open("a", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _con_id(
    journal: TradeJournal,
    account: str,
    strategy_id: str,
    ticker: str,
    reported: int,
) -> int:
    """Reuse this strategy's row for the ticker. Unknown conIds stay at 0.

    A second ticker must not take the placeholder 0 when that row is already
    another symbol. The stand-in is negative so it cannot be an IB conId.
    """
    rows = journal.strategy_position_rows()
    for row in rows:
        if (
            str(row.get("account") or "") == account
            and str(row.get("strategy_id") or "") == strategy_id
            and str(row.get("ticker") or "") == ticker
        ):
            return int(row["con_id"])
    if reported:
        return reported
    taken = any(
        str(row.get("account") or "") == account
        and str(row.get("strategy_id") or "") == strategy_id
        and int(row.get("con_id") or 0) == 0
        and str(row.get("ticker") or "") != ticker
        for row in rows
    )
    if not taken:
        return 0
    return -int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16) or -1


def _target_state(filled: float, order_qty: float | None) -> str:
    if order_qty is not None and filled + _QTY_EPS >= order_qty:
        return "FILLED"
    return "PARTIALLY_FILLED"


def ingest_broker_fills(
    home: Path,
    fills: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Write new executions and book them once.

    Fills that do not match a journal order are left unmatched. A second call
    with the same ``exec_id`` does not change strategy quantity.
    """
    normalized: list[dict[str, Any]] = []
    for raw in fills:
        item = raw if "exec_id" in raw else normalize_execution(raw)
        if item is None:
            continue
        account = str(item.get("account") or "")
        if account and not _paper_account(account):
            continue
        normalized.append(item)
    normalized.sort(key=lambda row: (str(row.get("time") or ""), str(row["exec_id"])))

    result: dict[str, Any] = {
        "recorded": [],
        "booked": [],
        "skipped": [],
        "unmatched": [],
        "states": {},
        "state_errors": {},
    }
    if not normalized:
        return result

    lock = _lock(home)
    try:
        journal = TradeJournal(home)
        machine = OrderMachine(journal)
        touched: set[str] = set()
        for item in normalized:
            _apply_one(journal, item, now=now, touched=touched, result=result)
        for order_id in sorted(touched):
            _advance(machine, journal, order_id, now=now, result=result)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return result


def _apply_one(
    journal: TradeJournal,
    item: dict[str, Any],
    *,
    now: datetime,
    touched: set[str],
    result: dict[str, Any],
) -> None:
    exec_id = str(item["exec_id"])
    order_ref = str(item.get("order_ref") or "")
    ctx = journal.order_context(order_ref) if order_ref else None
    if ctx is None:
        result["unmatched"].append(exec_id)
        return
    touched.add(str(ctx["order_id"]))
    inserted = False
    if not journal.has_execution(exec_id):
        when = _as_utc(item.get("time")) or (
            now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        )
        perm = item.get("perm_id")
        try:
            journal.record_fill(
                fill_id=exec_id,
                intent_id=str(ctx["intent_id"]),
                client_id=str(ctx["order_id"]),
                symbol=str(ctx["symbol"] or item.get("symbol") or ""),
                price=float(item["price"]),
                quantity=float(item["qty"]),
                fee=None if item.get("fee") is None else float(item["fee"]),
                when=when.astimezone(UTC),
                perm_id=None if perm is None else int(perm),
                exchange=str(item.get("exchange") or "") or None,
            )
        except sqlite3.IntegrityError:
            inserted = False
        else:
            inserted = True
            result["recorded"].append(exec_id)
    if journal.execution_booked(exec_id):
        if not inserted:
            result["skipped"].append(exec_id)
        return
    account = str(item.get("account") or "")
    if not account:
        account = os.environ.get("IBKR_PAPER_ACCOUNT", "").strip()
    if account and not _paper_account(account):
        return
    strategy_id = str(ctx.get("strategy_id") or item.get("strategy_id") or "")
    ticker = str(ctx["symbol"] or item.get("symbol") or "")
    side = _side(item.get("side")) or _side(ctx.get("side"))
    if not side:
        return
    when = _as_utc(item.get("time")) or (now if now.tzinfo is not None else now.replace(tzinfo=UTC))
    book_fill(
        journal,
        account=account,
        strategy_id=strategy_id,
        con_id=_con_id(
            journal,
            account,
            strategy_id or "_manual",
            ticker,
            int(item.get("con_id") or 0),
        ),
        ticker=ticker,
        side=side,
        qty=float(item["qty"]),
        price=float(item["price"]),
        intent_id=str(ctx["intent_id"]),
        parent_intent_id=ctx.get("parent_intent_id"),
        when=when.astimezone(UTC),
    )
    journal.mark_execution_booked(exec_id)
    result["booked"].append(exec_id)


def _advance(
    machine: OrderMachine,
    journal: TradeJournal,
    order_id: str,
    *,
    now: datetime,
    result: dict[str, Any],
) -> None:
    ctx = journal.order_context(order_id)
    if ctx is None:
        return
    current = str(ctx["state"])
    if current in {"FILLED", "CANCELLED", "EXPIRED", "BROKER_REJECTED"}:
        result["states"][order_id] = current
        return
    filled = journal.execution_quantity(order_id)
    if filled <= _QTY_EPS:
        return
    target = _target_state(filled, ctx.get("quantity"))
    if current == target:
        result["states"][order_id] = current
        return
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    try:
        machine.transition(order_id, target, when=moment.astimezone(UTC))
    except IllegalTransition as exc:
        result["state_errors"][order_id] = str(exc)
        return
    result["states"][order_id] = target
