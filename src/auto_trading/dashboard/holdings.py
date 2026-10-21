"""Admin and guest holdings projections for the paper account snapshot.

The file ``state/account.json`` keeps quantities and values for the admin
view and for the risk gate. Guest output is only each position's weight as
a percent of net liquidation. Absolute cash, share quantity, average cost,
and market value are left out of that view.

Cash weight is copied through only when the snapshot already has
``cash_weight_pct``. Cash itself is an IB BASE figure and is not assumed
to be in the same currency as NLV, so this module does not invent that ratio.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from auto_trading.execution.account_positions import finite_number

_AVG_KEYS = ("avg_cost", "avgCost", "averageCost", "average_cost", "avg")
_MKT_KEYS = ("mkt_value", "market_value", "marketValue", "mktValue")
_PNL_KEYS = (
    "unrealized_pnl",
    "unrealized",
    "unrealizedPNL",
    "unrealizedPnl",
    "unrealized_pnl_base",
)


def _first_number(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        number = finite_number(row.get(key))
        if number is not None:
            return number
    return None


def _fill(out: dict[str, Any], key: str, aliases: tuple[str, ...]) -> None:
    if finite_number(out.get(key)) is not None:
        return
    number = _first_number(out, aliases)
    if number is not None:
        out[key] = number


def position_weight_pct(market_value: float, nlv: float) -> float | None:
    """``market_value / nlv * 100`` when total assets are positive."""
    if nlv <= 0:
        return None
    return market_value / nlv * 100.0


def canonicalize_position(row: Mapping[str, Any], *, nlv: float | None = None) -> dict[str, Any]:
    """Copy ``row`` and fill the names the holdings table reads.

    ``avg`` is preserved when it is the only cost field, and it is filled
    from ``avg_cost`` when a newer writer omitted it. Market value accepts
    ``mkt_value``, ``market_value``, and ``marketValue``. Unrealized P&L
    accepts ``unrealized``, ``unrealized_pnl``, and ``unrealizedPNL``.
    """
    out = dict(row)
    _fill(out, "avg", _AVG_KEYS)
    _fill(out, "avg_cost", _AVG_KEYS)
    _fill(out, "mkt_value", _MKT_KEYS)
    _fill(out, "market_value", _MKT_KEYS)
    _fill(out, "unrealized", _PNL_KEYS)
    _fill(out, "unrealized_pnl", _PNL_KEYS)
    if finite_number(out.get("weight_pct")) is None:
        market = finite_number(out.get("mkt_value"))
        total = finite_number(nlv)
        if market is not None and total is not None:
            weight = position_weight_pct(market, total)
            if weight is not None:
                out["weight_pct"] = weight
    return out


def canonicalize_positions(rows: Any, *, nlv: float | None = None) -> list[Any]:
    """Canonicalize each position dict. Other entries are kept as they are."""
    if not isinstance(rows, list):
        return []
    out: list[Any] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(canonicalize_position(row, nlv=nlv))
        else:
            out.append(row)
    return out


def _symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "")


def admin_holdings(account: Mapping[str, Any]) -> dict[str, Any]:
    """Admin rows: quantity, average cost, market value, and unrealized P&L.

    Missing marks stay ``None`` so the table can show a dash instead of zero.
    """
    nlv = finite_number(account.get("nlv"))
    positions: list[dict[str, Any]] = []
    for row in account.get("positions") or []:
        if not isinstance(row, dict):
            continue
        canon = canonicalize_position(row, nlv=nlv)
        positions.append(
            {
                "symbol": _symbol(canon),
                "qty": finite_number(canon.get("qty") if "qty" in canon else canon.get("quantity")),
                "avg": finite_number(canon.get("avg")),
                "avg_cost": finite_number(canon.get("avg_cost")),
                "mkt_value": finite_number(canon.get("mkt_value")),
                "market_value": finite_number(canon.get("market_value")),
                "unrealized": finite_number(canon.get("unrealized")),
                "unrealized_pnl": finite_number(canon.get("unrealized_pnl")),
                "currency": canon.get("currency") or "",
                "weight_pct": finite_number(canon.get("weight_pct")),
            }
        )
    return {"positions": positions}


def guest_holdings(account: Mapping[str, Any]) -> dict[str, Any]:
    """Guest rows: symbol and weight percent only.

    Weight is market value divided by net liquidation, in percent. A position
    with no market value is listed without ``weight_pct``. Quantities, average
    cost, market value, unrealized P&L, cash, and NLV are not included.
    """
    nlv = finite_number(account.get("nlv"))
    positions: list[dict[str, Any]] = []
    for row in account.get("positions") or []:
        if not isinstance(row, dict):
            continue
        canon = canonicalize_position(row, nlv=nlv)
        symbol = _symbol(canon)
        if not symbol:
            continue
        item: dict[str, Any] = {"symbol": symbol}
        weight = finite_number(canon.get("weight_pct"))
        if weight is not None:
            item["weight_pct"] = weight
        positions.append(item)
    payload: dict[str, Any] = {"positions": positions}
    if "cash_weight_pct" in account:
        cash_weight = finite_number(account.get("cash_weight_pct"))
        if cash_weight is not None:
            payload["cash_weight_pct"] = cash_weight
    return payload
