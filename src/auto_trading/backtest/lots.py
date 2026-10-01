"""Share rounding. Never round up.

The odd-lot toggle defaults to false: Taiwan orders are whole lots of
1,000. Odd-lot sizing (step 1 under 1,000, drop the remainder at or above
1,000) is implemented and used only when the toggle is explicitly true.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TW_MIN_NOTIONAL = 100_000.0
TW_LOT = 1000


@dataclass(frozen=True)
class LotDecision:
    qty: int
    lot_size: int
    status: str
    binding_limit: str | None
    odd_remainder_dropped: int
    adr_checked: bool = False
    reason: str | None = None


def _floor_shares(raw: float) -> int:
    """Floor a share count.

    A budget built as ``qty * price * fx`` can land a few ulps under the
    integer. Treat that hairline as the integer; anything further below
    still floors down.
    """
    nearest = round(raw)
    if math.isclose(raw, nearest, abs_tol=1e-8):
        return int(nearest)
    return math.floor(raw)


def _binding(strategy: float, order: float, ticker: float) -> str:
    """Name the tightest budget. A tie between strategy and order is the order cap."""
    if ticker < strategy and ticker < order:
        return "ticker_cap"
    if order <= strategy:
        return "order_cap"
    return "strategy_target"


def size_quantity(
    *,
    price_local: float,
    fx_to_base: float,
    strategy_budget: float,
    order_budget: float,
    ticker_budget: float,
    market: str,
    odd_lot_enabled: bool = False,
    lot_size: int = 1,
) -> LotDecision:
    """Floor the affordable quantity to the market step.

    ``fx_to_base`` is base-currency units per one unit of local currency.
    Budgets are in base currency. A zero quantity is ``LOT_TOO_LARGE``.
    Taiwan notionals under TWD 100,000 are ``BELOW_MIN_ORDER_COST``.
    """
    if price_local <= 0 or fx_to_base <= 0:
        raise ValueError("price and FX must be positive")
    unit = price_local * fx_to_base
    budget = min(strategy_budget, order_budget, ticker_budget)
    raw_shares = budget / unit if budget > 0 else 0.0
    dropped = 0
    if market == "TW":
        if odd_lot_enabled:
            whole = _floor_shares(raw_shares)
            if whole >= TW_LOT:
                qty = (whole // TW_LOT) * TW_LOT
                dropped = whole - qty
            else:
                qty = whole
        else:
            qty = (_floor_shares(raw_shares) // TW_LOT) * TW_LOT
        reported_lot = TW_LOT
    else:
        step = lot_size
        qty = _floor_shares(raw_shares / step) * step if step else 0
        reported_lot = lot_size
    if qty <= 0:
        return LotDecision(
            qty=0,
            lot_size=reported_lot,
            status="LOT_TOO_LARGE",
            binding_limit=_binding(strategy_budget, order_budget, ticker_budget),
            odd_remainder_dropped=0,
        )
    if market == "TW" and qty * price_local < TW_MIN_NOTIONAL:
        return LotDecision(
            qty=0,
            lot_size=reported_lot,
            status="BELOW_MIN_ORDER_COST",
            binding_limit=None,
            odd_remainder_dropped=dropped,
        )
    return LotDecision(
        qty=qty,
        lot_size=reported_lot,
        status="ok",
        binding_limit=None,
        odd_remainder_dropped=dropped,
    )
