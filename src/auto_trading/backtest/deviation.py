"""Pair expected trades with actual fills and attribute the return gap.

The report keys follow strategy spec §6.3. Amounts are in the account
currency. ``gap_attribution`` splits the currency gap into missed trades,
extra trades, fill prices, costs, FX, and a residual.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TradeRef:
    ticker: str
    side: str
    session: date
    price: float
    qty: float = 0.0
    pnl: float = 0.0


def _matched(expected: TradeRef, actual: list[TradeRef]) -> TradeRef | None:
    window = {expected.session + timedelta(days=offset) for offset in (-1, 0, 1)}
    for trade in actual:
        same_name = trade.ticker == expected.ticker and trade.side == expected.side
        if same_name and trade.session in window:
            return trade
    return None


def trade_match_rate(expected: list[TradeRef], actual: list[TradeRef]) -> float:
    """Fraction of expected trades with a same-name fill within one day."""
    if not expected:
        return 1.0
    hits = sum(_matched(trade, actual) is not None for trade in expected)
    return hits / len(expected)


def fill_gap_bps(expected: TradeRef, actual: TradeRef) -> float:
    """BUY gap is actual/expected − 1, in basis points. SELL flips the sign."""
    gap = (actual.price / expected.price - 1) * 10_000
    if expected.side == "SELL":
        return -gap
    return gap


def _percentile(values: list[float], score: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    below = sum(item <= score for item in ordered)
    return below / len(ordered)


def _fill_cash_gap(expected: TradeRef, actual: TradeRef) -> float:
    """Cash difference caused by the fill price. BUY pays more when actual is higher."""
    qty = actual.qty or expected.qty
    if expected.side == "BUY":
        return (expected.price - actual.price) * qty
    return (actual.price - expected.price) * qty


def deviation_report(
    *,
    nlv: float,
    expected_pnl: float,
    actual_pnl: float,
    expected_equity: list[float],
    actual_equity: list[float],
    expected_trades: list[TradeRef],
    actual_trades: list[TradeRef],
    expected_cost: float = 0.0,
    actual_cost: float = 0.0,
    fx_gap: float = 0.0,
    slippage_model_bps: float = 0.0,
    actual_slippage_bps: float = 0.0,
    ret_distribution: list[float] | None = None,
) -> dict[str, object]:
    """Build the §6.3 deviation object, including gap attribution."""
    ret_expected = expected_pnl / nlv if nlv else 0.0
    ret_actual = actual_pnl / nlv if nlv else 0.0
    gaps: list[float] = []
    length = min(len(expected_equity), len(actual_equity))
    for index in range(1, length):
        exp_ret = expected_equity[index] / expected_equity[index - 1] - 1
        act_ret = actual_equity[index] / actual_equity[index - 1] - 1
        gaps.append(act_ret - exp_ret)
    tracking = None
    if len(gaps) > 1:
        mean = sum(gaps) / len(gaps)
        variance = sum((item - mean) ** 2 for item in gaps) / (len(gaps) - 1)
        tracking = math.sqrt(variance) * math.sqrt(252)
    matched: list[tuple[TradeRef, TradeRef]] = []
    missed: list[TradeRef] = []
    used: set[int] = set()
    for expected in expected_trades:
        hit = None
        hit_index = None
        for index, actual in enumerate(actual_trades):
            if index in used:
                continue
            if _matched(expected, [actual]) is not None:
                hit = actual
                hit_index = index
                break
        if hit is None or hit_index is None:
            missed.append(expected)
        else:
            used.add(hit_index)
            matched.append((expected, hit))
    extra = [trade for index, trade in enumerate(actual_trades) if index not in used]
    bps = [fill_gap_bps(expected, actual) for expected, actual in matched]
    fill_cash = sum(_fill_cash_gap(expected, actual) for expected, actual in matched)
    missed_cash = -sum(trade.pnl for trade in missed)
    extra_cash = sum(trade.pnl for trade in extra)
    cost_gap = actual_cost - expected_cost
    total_gap = actual_pnl - expected_pnl
    residual = total_gap - (missed_cash + extra_cash + fill_cash + cost_gap + fx_gap)
    percentile = _percentile(ret_distribution or [], ret_actual)
    in_range = None
    if ret_distribution:
        ordered = sorted(ret_distribution)
        low = ordered[max(0, int(len(ordered) * 0.05) - 1)]
        high = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        in_range = low <= ret_actual <= high
    return {
        "ret_actual": ret_actual,
        "ret_expected": ret_expected,
        "ret_gap_pp": (ret_actual - ret_expected) * 100,
        "tracking_error_ann": tracking,
        "trade_match_rate": trade_match_rate(expected_trades, actual_trades),
        "missed_trades": [trade.ticker for trade in missed],
        "extra_trades": [trade.ticker for trade in extra],
        "fill_gap_bps": {
            "mean": statistics.fmean(bps) if bps else None,
            "median": statistics.median(bps) if bps else None,
            "p90": sorted(bps)[max(0, math.ceil(len(bps) * 0.9) - 1)] if bps else None,
        },
        "slippage_model_gap_bps": actual_slippage_bps - slippage_model_bps,
        "cost_gap": cost_gap,
        "fx_gap": fx_gap,
        "range_percentile": percentile,
        "in_expected_range": in_range,
        "gap_attribution": {
            "missed": missed_cash,
            "extra": extra_cash,
            "fill": fill_cash,
            "cost": cost_gap,
            "fx": fx_gap,
            "residual": residual,
        },
    }
