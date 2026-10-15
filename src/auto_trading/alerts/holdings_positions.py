"""Load holdings from the snapshot, then from ``account.json``.

A missing snapshot uses the account file. A partial snapshot raises
:class:`IncompleteSnapshot` so the caller can fall back and retry next tick.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.alerts.holdings_state import MACAU, atomic_write_json, iso_macau
from auto_trading.jsonutil import loads_strict
from auto_trading.market.instruments import describe

SNAPSHOT_SCHEMA = "holdings_snapshot/1.0"
SNAPSHOT_NAME = "holdings_snapshot.json"


class IncompleteSnapshot(Exception):
    """``holdings_snapshot.json`` exists but is not a finished document."""


@dataclass(frozen=True)
class PositionView:
    ticker: str
    qty: float
    avg_cost: float | None = None
    con_id: int | None = None
    market: str | None = None
    currency: str | None = None
    strategy_id: str | None = None
    protective_stop_pct: float | None = None
    stop_price: float | None = None
    stop_source: str | None = None
    side: str = "long"
    protective_stop_exempt: bool = False

    @property
    def signed_qty(self) -> float:
        magnitude = abs(self.qty)
        return -magnitude if self.side == "short" else magnitude


@dataclass(frozen=True)
class PositionLoad:
    positions: list[PositionView]
    source: str
    retry_incomplete_snapshot: bool
    source_at: str | None
    snapshot_at: str | None


def snapshot_file(trading_home: Path, override: Path | None = None) -> Path:
    if override is not None:
        return override
    return trading_home / "state" / SNAPSHOT_NAME


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _side(raw: Any, qty: float) -> str:
    label = str(raw or "").strip().lower()
    if label in {"short", "sell", "s"}:
        return "short"
    if label in {"long", "buy", "b"}:
        return "long"
    return "short" if qty < 0 else "long"


def _exempt(row: dict[str, Any]) -> bool:
    if row.get("protective_stop_exempt") is True:
        return True
    stop = row.get("protective_stop")
    if stop == "exempt":
        return True
    return isinstance(stop, dict) and stop.get("exempt") is True


def _market(ticker: str, raw: Any) -> str | None:
    text = _text(raw)
    if text:
        return text.upper()
    try:
        return describe(ticker).market
    except (TypeError, ValueError):
        return None


def _currency(ticker: str, raw: Any) -> str | None:
    text = _text(raw)
    if text:
        return text.upper()
    try:
        return describe(ticker).currency
    except (TypeError, ValueError):
        return None


def _position_row(row: dict[str, Any]) -> PositionView:
    ticker = _text(row.get("ticker") or row.get("symbol"))
    qty = _number(row.get("qty") if "qty" in row else row.get("quantity"))
    if not ticker or qty is None:
        raise IncompleteSnapshot("position requires ticker and qty")
    avg = _number(row.get("avg_cost") if "avg_cost" in row else row.get("avg"))
    con_raw = row.get("con_id")
    con_id = None if con_raw in (None, "") else _number(con_raw)
    side = _side(row.get("side"), qty)
    source = _text(row.get("stop_source"))
    if source is not None:
        source = source.lower()
    strategy = _text(row.get("strategy_id"))
    return PositionView(
        ticker=ticker,
        qty=qty,
        avg_cost=avg,
        con_id=None if con_id is None else int(con_id),
        market=_market(ticker, row.get("market")),
        currency=_currency(ticker, row.get("currency")),
        strategy_id=strategy,
        protective_stop_pct=_number(row.get("protective_stop_pct")),
        stop_price=_number(row.get("stop_price")),
        stop_source=source,
        side=side,
        protective_stop_exempt=_exempt(row),
    )


def _as_of(payload: dict[str, Any]) -> str | None:
    for key in ("at", "generated_at", "source_at"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_holdings_snapshot(path: Path) -> tuple[list[PositionView], str | None]:
    """Parse a finished snapshot. Partial JSON raises :class:`IncompleteSnapshot`."""
    if not path.is_file():
        raise IncompleteSnapshot("missing")
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise IncompleteSnapshot("partial") from exc
    if not isinstance(payload, dict):
        raise IncompleteSnapshot("shape")
    if payload.get("schema_version") != SNAPSHOT_SCHEMA:
        raise IncompleteSnapshot("schema")
    rows = payload.get("positions")
    if not isinstance(rows, list):
        raise IncompleteSnapshot("positions")
    parsed = [_position_row(row) for row in rows if _require_row(row)]
    return parsed, _as_of(payload)


def _require_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise IncompleteSnapshot("position")
    return row


def parse_account_positions(path: Path) -> list[PositionView] | None:
    """Positions from ``account.json``. ``None`` means the file cannot be trusted."""
    if not path.is_file():
        return None
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("positions")
    if not isinstance(rows, list):
        return None
    parsed: list[PositionView] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        try:
            parsed.append(_position_row(row))
        except IncompleteSnapshot:
            return None
    return parsed


def account_as_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("at")
    return value.strip() if isinstance(value, str) and value.strip() else None


def load_positions(trading_home: Path, snapshot_path: Path | None = None) -> PositionLoad:
    """Prefer the snapshot. Fall back to ``account.json`` when it is missing or partial."""
    snap = snapshot_file(trading_home, snapshot_path)
    retry = False
    if snap.is_file():
        try:
            positions, as_of = parse_holdings_snapshot(snap)
        except IncompleteSnapshot:
            retry = True
        else:
            return PositionLoad(
                positions=positions,
                source="snapshot",
                retry_incomplete_snapshot=False,
                source_at=as_of,
                snapshot_at=as_of,
            )
    account_path = trading_home / "state" / "account.json"
    positions = parse_account_positions(account_path)
    if positions is None:
        return PositionLoad(
            positions=[],
            source="none",
            retry_incomplete_snapshot=retry,
            source_at=None,
            snapshot_at=None,
        )
    as_of = account_as_of(account_path)
    return PositionLoad(
        positions=positions,
        source="account",
        retry_incomplete_snapshot=retry,
        source_at=as_of,
        snapshot_at=None,
    )


def load_broker_stops(trading_home: Path) -> dict[str, list[tuple[str | None, float]]]:
    """Working broker STP prices from ``account.json`` open orders, keyed by symbol."""
    path = trading_home / "state" / "account.json"
    if not path.is_file():
        return {}
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("open_orders")
    if not isinstance(rows, list):
        return {}
    found: dict[str, list[tuple[str | None, float]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _stop_price(row)
        symbol = _text(row.get("symbol") or row.get("ticker"))
        if price is None or price <= 0 or not symbol:
            continue
        strategy = _text(row.get("strategy_id"))
        found.setdefault(symbol, []).append((strategy, price))
    return found


def _stop_price(row: dict[str, Any]) -> float | None:
    kind = str(row.get("order_type") or row.get("orderType") or "").upper().replace("_", " ")
    protective = row.get("protective") is True
    if kind not in {"STP", "STP LMT", "STOP", "STOP LIMIT"} and not protective:
        return None
    return _number(row.get("stop_price") if "stop_price" in row else row.get("auxPrice"))


def position_names(ticker: str) -> set[str]:
    names = {ticker, ticker.upper()}
    with contextlib.suppress(TypeError, ValueError):
        names.add(describe(ticker).ib_symbol)
    return {item for item in names if item}


def broker_stop_for(
    position: PositionView,
    orders: dict[str, list[tuple[str | None, float]]],
) -> float | None:
    """Broker STP on the position wins, then a matching open order."""
    if position.stop_source == "broker" and position.stop_price and position.stop_price > 0:
        return float(position.stop_price)
    candidates: list[tuple[str | None, float]] = []
    for name in position_names(position.ticker):
        candidates.extend(orders.get(name, []))
    if position.strategy_id:
        for strategy, price in candidates:
            if strategy == position.strategy_id:
                return price
    for strategy, price in candidates:
        if strategy is None:
            return price
    if candidates:
        return candidates[0][1]
    return None


def position_payload(position: PositionView) -> dict[str, Any]:
    return {
        "ticker": position.ticker,
        "qty": position.qty,
        "avg_cost": position.avg_cost,
        "market": position.market,
        "currency": position.currency,
        "con_id": position.con_id,
        "side": position.side,
        "strategy_id": position.strategy_id,
        "protective_stop_pct": position.protective_stop_pct,
        "stop_price": position.stop_price,
        "stop_source": position.stop_source,
        "protective_stop_exempt": position.protective_stop_exempt,
    }


def snapshot_document(
    positions: list[PositionView],
    *,
    account: str,
    now: datetime,
    source_at: str | None,
) -> dict[str, Any]:
    moment = iso_macau(now)
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "trading_scope": "paper_only",
        "account": account,
        "at": moment,
        "generated_at": moment,
        "source_at": source_at or moment,
        "position_count": len(positions),
        "positions": [position_payload(item) for item in positions],
    }


def write_holdings_snapshot(
    trading_home: Path,
    positions: list[PositionView],
    *,
    account: str,
    now: datetime,
    source_at: str | None,
    snapshot_path: Path | None = None,
) -> Path:
    """Atomically publish the shared holdings snapshot."""
    path = snapshot_file(trading_home, snapshot_path)
    atomic_write_json(
        path,
        snapshot_document(positions, account=account, now=now, source_at=source_at),
    )
    return path


def snapshot_age_sec(snapshot_at: str | None, now: datetime) -> float | None:
    if not snapshot_at:
        return None
    try:
        parsed = datetime.fromisoformat(snapshot_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MACAU)
    return (now - parsed).total_seconds()
