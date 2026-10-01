"""Performance statistics required by strategy spec §6.1.

Missing inputs stay as ``None`` or zeros so every key is present. Sharpe uses
a risk-free rate of zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

METRIC_KEYS = (
    "cagr",
    "ann_vol",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "payoff_ratio",
    "profit_factor",
    "avg_holding_days",
    "n_trades",
    "turnover_annual",
    "exposure",
    "cost_commission",
    "cost_tax",
    "cost_slippage",
    "excess_return_vs_benchmark",
    "monthly_returns",
    "lot_too_large_count",
    "survivorship_bias_flag",
    "n_trials",
    "data_snapshot_hash",
    "interpreter_version",
    "strategy_id",
    "strategy_version",
    "spec_hash",
    "base_currency",
    "fill_model",
    "fill_strict_delta",
    "unfilled_count",
    "below_min_order_cost_count",
    "odd_lot_fills",
    "odd_remainder_dropped",
    "adr_substitutions",
    "r9_resume_scenario",
    "risk_config_hash",
    "backtest_mode",
    "allocator_priority",
    "risk_triggers",
    "skipped_same_ticker",
    "live_executable_ratio",
    "metrics_no_risk",
)


def _drawdown(equity: list[float]) -> dict[str, Any]:
    peak = equity[0]
    peak_at = 0
    worst = 0.0
    start = 0
    end = 0
    for index, value in enumerate(equity):
        if value > peak:
            peak = value
            peak_at = index
        drop = value / peak - 1 if peak else 0.0
        if drop < worst:
            worst = drop
            start = peak_at
            end = index
    return {"depth": worst, "start_index": start, "end_index": end, "recovered": False}


def _cagr(equity: list[float]) -> float | None:
    if len(equity) < 2 or equity[0] <= 0 or equity[-1] <= 0:
        return None
    years = (len(equity) - 1) / 252
    if years <= 0:
        return None
    return (equity[-1] / equity[0]) ** (1 / years) - 1


def compute_metrics(
    equity: list[float],
    trade_pnls: list[float],
    holding_days: list[int],
    *,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the report object. ``meta`` fills identity and counter fields."""
    report: dict[str, Any] = dict.fromkeys(METRIC_KEYS)
    report.update(meta)
    if len(equity) < 2:
        report["cagr"] = None
        report["max_drawdown"] = _drawdown(equity or [0.0])
        report["monthly_returns"] = []
        return report
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / len(returns)
    std = math.sqrt(variance)
    downside = math.sqrt(sum(min(item, 0.0) ** 2 for item in returns) / len(returns))
    cagr = _cagr(equity)
    drawdown = _drawdown(equity)
    report["cagr"] = cagr
    report["ann_vol"] = std * math.sqrt(252)
    report["max_drawdown"] = drawdown
    report["sharpe"] = mean / std * math.sqrt(252) if std else None
    report["sortino"] = mean / downside * math.sqrt(252) if downside else None
    depth = drawdown["depth"]
    report["calmar"] = cagr / abs(depth) if cagr is not None and depth else None
    wins = [item for item in trade_pnls if item > 0]
    losses = [item for item in trade_pnls if item < 0]
    report["n_trades"] = len(trade_pnls)
    report["win_rate"] = len(wins) / len(trade_pnls) if trade_pnls else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    report["payoff_ratio"] = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss else None
    gross_loss = abs(sum(losses))
    report["profit_factor"] = sum(wins) / gross_loss if gross_loss else None
    report["avg_holding_days"] = sum(holding_days) / len(holding_days) if holding_days else None
    report["monthly_returns"] = report.get("monthly_returns") or []
    return report
