"""Position rows for ``state/account.json``.

``positions()`` has quantity and average cost. Market value and unrealized
P&L come from portfolio marks. Average cost stays in the contract currency
and is written as both ``avg`` and ``avg_cost``. Market value and unrealized
P&L are converted into the system base currency with the same rules as NLV,
so a holding's weight is that market value divided by net liquidation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import Any


def finite_number(value: Any) -> float | None:
    """A finite float, or None. Booleans and blank text are not numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    elif isinstance(value, int | float):
        number = float(value)
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def amount_in_system_base(
    amount: float, rows: list[Any], *, system: str | None = None
) -> float | None:
    """Convert an account-base amount the same way NLV is converted.

    An unidentified account base is returned unchanged. A known base that
    cannot be converted is omitted (``None``) rather than stored in the
    wrong currency beside NLV.
    """
    from auto_trading.backtest.fx import to_base
    from auto_trading.execution.fx import (
        DEFAULT_RATES_HKD,
        account_base_currency,
        rates_from_account_values,
    )

    if system is None:
        from auto_trading.config import get_settings

        system = get_settings().base_currency
    base = account_base_currency(rows)
    if base is None or base in {system, "BASE"}:
        return amount
    rates = rates_from_account_values(rows)
    if base not in rates:
        if base not in DEFAULT_RATES_HKD:
            return None
        rates = dict(DEFAULT_RATES_HKD)
    try:
        return to_base(amount, base, rates, system)
    except (KeyError, ValueError):
        return None


def _con_id(contract: Any) -> int:
    try:
        return int(getattr(contract, "conId", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _symbol(contract: Any) -> str:
    return str(getattr(contract, "symbol", "") or "")


def _exchange(contract: Any) -> str:
    primary = str(getattr(contract, "primaryExchange", "") or "")
    if primary:
        return primary
    return str(getattr(contract, "exchange", "") or "")


def _mark_for(contract: Any, marks: list[Any]) -> Any | None:
    con_id = _con_id(contract)
    symbol = _symbol(contract)
    if con_id:
        for item in marks:
            if _con_id(getattr(item, "contract", None)) == con_id:
                return item
    if symbol:
        for item in marks:
            other = getattr(item, "contract", None)
            if _symbol(other) == symbol:
                return item
    return None


def _marked(value: float | None, convert: Callable[[float], float | None] | None) -> float | None:
    if value is None:
        return None
    if convert is None:
        return value
    return finite_number(convert(value))


def rows_from_broker(
    positions: Iterable[Any],
    portfolio: Iterable[Any],
    *,
    convert: Callable[[float], float | None] | None = None,
) -> list[dict[str, Any]]:
    """One account.json row per broker position, with portfolio marks merged in.

    ``avg`` is kept for existing readers. ``avg_cost`` is the same number.
    Market value and unrealized P&L are written only when the mark is a
    finite number after ``convert``.
    """
    marks = list(portfolio)
    rows: list[dict[str, Any]] = []
    for pos in positions:
        contract = getattr(pos, "contract", None)
        symbol = _symbol(contract)
        qty = finite_number(getattr(pos, "position", None))
        if not symbol or qty is None:
            raise ValueError("broker position needs a symbol and a finite qty")
        mark = _mark_for(contract, marks)
        avg = finite_number(getattr(pos, "avgCost", None))
        market_price = None
        market_value = None
        unrealized = None
        if mark is not None:
            if avg is None:
                avg = finite_number(getattr(mark, "averageCost", None))
            market_price = finite_number(getattr(mark, "marketPrice", None))
            market_value = _marked(finite_number(getattr(mark, "marketValue", None)), convert)
            unrealized = _marked(finite_number(getattr(mark, "unrealizedPNL", None)), convert)
        row: dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "con_id": _con_id(contract),
            "currency": str(getattr(contract, "currency", "") or ""),
            "exchange": _exchange(contract),
        }
        if avg is not None:
            row["avg"] = avg
            row["avg_cost"] = avg
        if market_price is not None:
            row["market_price"] = market_price
        if market_value is not None:
            row["mkt_value"] = market_value
            row["market_value"] = market_value
        if unrealized is not None:
            row["unrealized"] = unrealized
            row["unrealized_pnl"] = unrealized
        rows.append(row)
    return rows
