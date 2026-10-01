"""Next-open limit fill model (strategy spec §2.4 and §2.8).

BUY limit is official open × 1.001, tick-aligned down. SELL is × 0.999,
aligned up. A BUY fills only when low ≤ limit. The strict variant treats an
open that is also the day's extreme as unfilled. Fill prices are never worse
than the limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from auto_trading.market.ticks import tick_size

SLIP_BPS = {"US": 5.0, "JP": 8.0, "HK": 10.0, "KR": 10.0, "TW": 10.0}
ODD_LOT_SLIP_BPS = 25.0
OFFSET_BPS = 10.0


@dataclass(frozen=True)
class FillResult:
    filled: bool
    limit_price: float | None
    fill_price: float | None
    qty: int
    reason: str | None


def align(price: float, tick: float, side: str) -> float:
    """BUY rounds toward zero ticks; SELL rounds away from zero ticks."""
    steps = price / tick
    aligned = math.floor(steps + 1e-09) * tick if side == "BUY" else math.ceil(steps - 1e-09) * tick
    return round(aligned, 10)


def limit_from_open(open_price: float, side: str, market: str) -> float:
    """Official-open limit, tick aligned."""
    raw = open_price * (1.001 if side == "BUY" else 0.999)
    return align(raw, tick_size(market, raw), side)


def _slip_bps(market: str, odd_lot: bool) -> float:
    if market == "TW" and odd_lot:
        return ODD_LOT_SLIP_BPS
    return SLIP_BPS[market]


def try_fill(
    *,
    open_price: float,
    high: float,
    low: float,
    side: str,
    market: str,
    qty: int,
    strict: bool = False,
    limit_cap: float | None = None,
    odd_lot: bool = False,
    avg_volume_20: float | None = None,
    odd_lot_low: float | None = None,
) -> FillResult:
    """Simulate one DAY limit order on a daily bar."""
    if side == "BUY" and limit_cap is not None and open_price > limit_cap:
        return FillResult(False, None, None, 0, "CHASE_CAP_EXCEEDED")
    limit = limit_from_open(open_price, side, market)
    if side == "BUY" and limit_cap is not None:
        limit = align(min(limit, limit_cap), tick_size(market, min(limit, limit_cap)), "BUY")
    tick = tick_size(market, open_price)
    one_bp = 0.0001
    if strict and side == "BUY" and low >= open_price * (1 - one_bp):
        return FillResult(False, limit, None, 0, "strict_no_fill")
    if strict and side == "SELL" and high <= open_price * (1 + one_bp):
        return FillResult(False, limit, None, 0, "strict_no_fill")
    if odd_lot and strict and odd_lot_low is None and low > limit * (1 - 0.003):
        return FillResult(False, limit, None, 0, "strict_odd_lot")
    if odd_lot and strict and odd_lot_low is not None and odd_lot_low > limit:
        return FillResult(False, limit, None, 0, "strict_odd_lot")
    if side == "BUY" and low > limit:
        return FillResult(False, limit, None, 0, "limit_not_reached")
    if side == "SELL" and high < limit:
        return FillResult(False, limit, None, 0, "limit_not_reached")
    filled_qty = qty
    if odd_lot and avg_volume_20 is not None:
        cap = math.floor(avg_volume_20 * 0.001)
        filled_qty = min(filled_qty, cap)
        if filled_qty < 1:
            return FillResult(False, limit, None, 0, "odd_lot_volume_cap")
    slip = max(open_price * _slip_bps(market, odd_lot) / 10_000, tick)
    price = min(limit, open_price + slip) if side == "BUY" else max(limit, open_price - slip)
    return FillResult(True, limit, price, filled_qty, None)
