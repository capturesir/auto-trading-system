"""Long-only daily event-driven backtest.

Decisions at 07:05 use bars whose ``available_at`` is already known. Fills use
the next session's official open and the limit model. The odd-lot toggle
defaults to false. R9 uses ``manual_no_reset`` unless another scenario is
requested.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from auto_trading.backtest.fills import try_fill
from auto_trading.backtest.fx import to_base
from auto_trading.backtest.lots import size_quantity
from auto_trading.backtest.metrics import METRIC_KEYS, compute_metrics
from auto_trading.backtest.risk_sim import RiskSimulator, fill_rejections
from auto_trading.market.instruments import describe
from auto_trading.strategy.sizing import target_fraction
from auto_trading.strategy.spec import INTERPRETER_VERSION, spec_hash
from auto_trading.strategy.vocabulary import (
    Series,
    close_above_ma,
    exit_priority,
    parse_hold,
    signal_reversal,
    slope_up,
    stop_loss_pct,
    time_exit,
)

PRIORITY = ["sig_trend", "sig_pullback", "news_event", "fv_rotation"]


class LookaheadError(Exception):
    """A bar was read before its ``available_at`` time."""


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    available_at: str | None = None


@dataclass
class BtSignal:
    ticker: str
    side: str
    confidence: int
    fair_value: float
    signal_id: str
    price_date: str


@dataclass
class _Pos:
    qty: int
    avg_cost: float
    entry_intent: str
    stop: float
    highest: float
    hold_sessions: int = 0


@dataclass
class _Order:
    ticker: str
    side: str
    qty: int
    kind: str
    intent_id: str
    parent: str | None
    decision_date: str


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    trades: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    equity: list[tuple[str, float]] = field(default_factory=list)


def assert_visible(bars: list[Bar], decision_time: str) -> None:
    """Raise when any bar is not yet available at ``decision_time``."""
    for bar in bars:
        if bar.available_at is not None and bar.available_at > decision_time:
            raise LookaheadError(
                f"{bar.date} available_at {bar.available_at} is after {decision_time}"
            )


def _confirms(strategy: dict[str, Any], closes: list[float]) -> bool:
    confirm = strategy.get("confirm") or {}
    series = Series(closes=closes)
    checks: list[bool] = []
    for rule in confirm.get("all_of") or []:
        name = rule.get("indicator")
        if name == "close_above_ma":
            checks.append(close_above_ma(series, int(rule["period"])))
        elif name == "ma_slope_up":
            checks.append(slope_up(series, int(rule["period"]), int(rule["lookback"])))
    return all(checks) if checks else True


def _stop_parts(strategy: dict[str, Any]) -> tuple[float, float | None]:
    """Broker stop distance, and the strategy stop percent when one exists.

    A strategy stop is widened by 1.5. With no strategy stop the broker
    distance is the 15% default, not 15% × 1.5.
    """
    for rule in strategy.get("exits") or []:
        if rule.get("type") == "stop_loss_pct":
            percent = float(rule["value"])
            return percent / 100 * 1.5, percent
    return 0.15, None


def _fx(currency: str, rates: dict[str, float], base: str) -> float:
    if base == "HKD":
        return rates[currency]
    return rates[currency] / rates["USD"]


def _bar(bars: dict[str, list[Bar]], ticker: str, day: str) -> Bar | None:
    return next((row for row in bars.get(ticker, []) if row.date == day), None)


def _run_once(
    strategy: dict[str, Any],
    bars: dict[str, list[Bar]],
    signals: list[BtSignal],
    *,
    nlv: float,
    base: str,
    rates: dict[str, float],
    odd_lot: bool,
    strict: bool,
    resume: str,
    apply_risk: bool,
) -> BacktestResult:
    cash = nlv
    positions: dict[str, _Pos] = {}
    pending: list[_Order] = []
    trades: list[dict[str, Any]] = []
    order_log: list[dict[str, Any]] = []
    equity: list[tuple[str, float]] = []
    sim = RiskSimulator(hwm=nlv, scenario=resume)
    counters: dict[str, Any] = {
        "unfilled": 0,
        "lot_too_large": 0,
        "below_min": 0,
        "odd_lot_fills": 0,
        "remainder": 0,
        "rejected": {name: 0 for name in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R15")},
    }
    protective_distance, stop_pct = _stop_parts(strategy)
    dates = sorted({row.date for rows in bars.values() for row in rows})

    def marked(day: str, field_name: str) -> float:
        total = cash
        for ticker, pos in positions.items():
            row = _bar(bars, ticker, day)
            if row is None:
                continue
            spec = describe(ticker, odd_lot_enabled=odd_lot)
            total += to_base(getattr(row, field_name) * pos.qty, spec.currency, rates, base)
        return total

    for day in dates:
        decision_time = f"{day}T07:05:00+08:00"
        sim.mark(marked(day, "open"), new_risk_day=True, trading_day=True, session_id=day)
        for ticker, pos in list(positions.items()):
            row = _bar(bars, ticker, day)
            if row is not None and row.low <= pos.stop:
                cash = _sell(
                    cash,
                    positions,
                    ticker,
                    pos.qty,
                    min(row.open, pos.stop),
                    "protective",
                    day,
                    rates,
                    base,
                    odd_lot,
                    trades,
                )
                sim.note_protective_exit()
        nxt: list[_Order] = []
        for order in pending:
            row = _bar(bars, order.ticker, day)
            if row is None or (order.ticker not in positions and order.side == "SELL"):
                if row is None:
                    nxt.append(order)
                continue
            spec = describe(order.ticker, odd_lot_enabled=odd_lot)
            odd = odd_lot and spec.market == "TW" and order.qty < 1000
            result = try_fill(
                open_price=row.open,
                high=row.high,
                low=row.low,
                side=order.side,
                market=spec.market,
                qty=order.qty,
                strict=strict,
                odd_lot=odd,
            )
            if not result.filled or result.fill_price is None:
                counters["unfilled"] += 1
                continue
            notional = to_base(result.fill_price * result.qty, spec.currency, rates, base)
            if apply_risk and order.side == "BUY":
                reasons = fill_rejections(notional, marked(day, "open"))
                if reasons:
                    for reason in reasons:
                        counters["rejected"][reason] += 1
                    continue
                if sim.allow("BUY", "entry"):
                    continue
            if order.side == "BUY":
                positions[order.ticker] = _Pos(
                    qty=result.qty,
                    avg_cost=result.fill_price,
                    entry_intent=order.intent_id,
                    stop=result.fill_price * (1 - protective_distance),
                    highest=row.close,
                )
                cash -= notional
                if odd:
                    counters["odd_lot_fills"] += 1
            else:
                cash = _sell(
                    cash,
                    positions,
                    order.ticker,
                    result.qty,
                    result.fill_price,
                    order.kind,
                    day,
                    rates,
                    base,
                    odd_lot,
                    trades,
                    order.parent,
                )
            order_log.append(
                {"intent_id": order.intent_id, "decision_date": order.decision_date, "day": day}
            )
        pending = nxt
        for ticker, pos in list(positions.items()):
            row = _bar(bars, ticker, day)
            if row is not None and row.low <= pos.stop:
                cash = _sell(
                    cash,
                    positions,
                    ticker,
                    pos.qty,
                    min(row.open, pos.stop),
                    "protective",
                    day,
                    rates,
                    base,
                    odd_lot,
                    trades,
                )
                sim.note_protective_exit()
        nav = marked(day, "close")
        sim.mark(nav, new_risk_day=False, trading_day=True, session_id=day)
        equity.append((day, nav))
        for rows in bars.values():
            assert_visible([row for row in rows if row.date < day], decision_time)
        for ticker, pos in list(positions.items()):
            row = _bar(bars, ticker, day)
            if row is None:
                continue
            pos.highest = max(pos.highest, row.close)
            pos.hold_sessions += 1
            exit_order = _exit_order(
                strategy, ticker, pos, row, day, signals, stop_pct, sim, apply_risk
            )
            if exit_order is not None:
                pending.append(exit_order)
        pending.extend(
            _entries(
                strategy,
                bars,
                signals,
                day,
                decision_time,
                positions,
                nav,
                odd_lot,
                rates,
                base,
                sim,
                apply_risk,
                counters,
            )
        )
    meta = dict.fromkeys(METRIC_KEYS)
    meta.update(
        {
            "interpreter_version": INTERPRETER_VERSION,
            "strategy_id": strategy.get("id"),
            "strategy_version": strategy.get("version"),
            "spec_hash": spec_hash(strategy),
            "base_currency": base,
            "fill_model": "next_open_limit_strict" if strict else "next_open_limit",
            "unfilled_count": counters["unfilled"],
            "lot_too_large_count": counters["lot_too_large"],
            "below_min_order_cost_count": counters["below_min"],
            "odd_lot_fills": counters["odd_lot_fills"],
            "odd_remainder_dropped": counters["remainder"],
            "risk_triggers": {
                **{f"rejected_{name}": counters["rejected"][name] for name in counters["rejected"]},
                "r8_trigger_days": sim.r8_trigger_days,
                "r9_warn_count": sim.r9_warn_count,
                "r9_halt_count": sim.r9_halt_count,
                "halted_sessions": sim.halted_sessions,
                "protective_stop_exits": sim.protective_stop_exits,
                "r23_demote_count": 0,
            },
            "allocator_priority": PRIORITY,
            "backtest_mode": "standalone",
            "survivorship_bias_flag": True,
            "n_trials": 1,
            "turnover_annual": None,
            "exposure": None,
            "cost_commission": 0.0,
            "cost_tax": 0.0,
            "cost_slippage": 0.0,
            "excess_return_vs_benchmark": None,
            "monthly_returns": [],
            "adr_substitutions": [],
            "skipped_same_ticker": 0,
            "live_executable_ratio": None,
            "data_snapshot_hash": _snapshot(bars),
        }
    )
    pnls = [float(item["pnl"]) for item in trades]
    holds = [int(item["holding_days"]) for item in trades]
    return BacktestResult(
        metrics=compute_metrics([value for _day, value in equity], pnls, holds, meta=meta),
        trades=trades,
        orders=order_log,
        equity=equity,
    )


def _sell(
    cash: float,
    positions: dict[str, _Pos],
    ticker: str,
    qty: int,
    price: float,
    reason: str,
    day: str,
    rates: dict[str, float],
    base: str,
    odd_lot: bool,
    trades: list[dict[str, Any]],
    parent: str | None = None,
) -> float:
    pos = positions[ticker]
    sold = min(qty, pos.qty)
    spec = describe(ticker, odd_lot_enabled=odd_lot)
    notional = to_base(price * sold, spec.currency, rates, base)
    cost = to_base(pos.avg_cost * sold, spec.currency, rates, base)
    trades.append(
        {
            "ticker": ticker,
            "pnl": notional - cost,
            "holding_days": pos.hold_sessions,
            "exit_reason": reason,
            "parent_intent_id": parent or pos.entry_intent,
            "session": day,
        }
    )
    pos.qty -= sold
    if pos.qty <= 0:
        del positions[ticker]
    return cash + notional


def _exit_order(
    strategy, ticker, pos, row, day, signals, stop_pct, sim, apply_risk
) -> _Order | None:
    reasons: list[str] = []
    if stop_pct is not None and stop_loss_pct(row.close, pos.avg_cost, stop_pct):
        reasons.append("stop_loss_pct")
    latest = next(
        (item for item in signals if item.ticker == ticker and item.price_date <= day),
        None,
    )
    reversal = next(
        (rule for rule in strategy.get("exits") or [] if rule.get("type") == "signal_reversal"),
        None,
    )
    if (
        reversal
        and latest is not None
        and signal_reversal(
            latest.side,
            list(reversal.get("on_side") or []),
            None,
            reversal.get("upside_live_below_pct"),
        )
    ):
        reasons.append("signal_reversal")
    time_rule = next(
        (rule for rule in strategy.get("exits") or [] if rule.get("type") == "time"),
        None,
    )
    if time_rule is not None:
        pnl = (row.close / pos.avg_cost - 1) * 100
        below = time_rule.get("only_if_pnl_below_pct")
        if time_exit(pos.hold_sessions, parse_hold(time_rule["max_hold"]), pnl, below):
            reasons.append("time")
    reason = exit_priority(reasons)
    if reason is None:
        return None
    if apply_risk and sim.allow("SELL", "exit"):
        return None
    return _Order(ticker, "SELL", pos.qty, reason, f"{day}-{ticker}-exit", pos.entry_intent, day)


def _entries(
    strategy,
    bars,
    signals,
    day,
    decision_time,
    positions,
    nav,
    odd_lot,
    rates,
    base,
    sim,
    apply_risk,
    counters,
):
    orders: list[_Order] = []
    trigger = strategy.get("trigger") or {}
    if trigger.get("type") not in {None, "signal"}:
        return orders
    minimum = int(trigger.get("min_confidence", 60))
    for signal in signals:
        if signal.price_date > day or signal.ticker in positions or signal.side != "BUY":
            continue
        if signal.confidence < minimum:
            continue
        history = [row for row in bars.get(signal.ticker, []) if row.date < day]
        assert_visible(history, decision_time)
        closes = [row.close for row in history]
        if closes and not _confirms(strategy, closes):
            continue
        if apply_risk and sim.allow("BUY", "entry"):
            continue
        spec = describe(signal.ticker, odd_lot_enabled=odd_lot)
        weight = target_fraction(strategy["action"]["size"], confidence=signal.confidence)
        price = history[-1].close if history else signal.fair_value
        decision = size_quantity(
            price_local=price,
            fx_to_base=_fx(spec.currency, rates, base),
            strategy_budget=weight * nav,
            order_budget=min(nav * 0.05, 200_000) / 1.02,
            ticker_budget=0.095 * nav,
            market=spec.market,
            odd_lot_enabled=odd_lot,
            lot_size=spec.lot_size,
        )
        counters["remainder"] += decision.odd_remainder_dropped
        if decision.status != "ok":
            if decision.status == "LOT_TOO_LARGE":
                counters["lot_too_large"] += 1
            else:
                counters["below_min"] += 1
            continue
        orders.append(
            _Order(
                signal.ticker,
                "BUY",
                decision.qty,
                "ENTRY",
                f"{day}-{signal.ticker}-entry",
                None,
                day,
            )
        )
    return orders


def _snapshot(bars: dict[str, list[Bar]]) -> str:
    parts = [
        f"{ticker}|{bar.date}|{bar.open}|{bar.high}|{bar.low}|{bar.close}"
        for ticker in sorted(bars)
        for bar in bars[ticker]
    ]
    return "sha256:" + hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _scenario_summary(result: BacktestResult) -> dict[str, Any]:
    triggers = result.metrics["risk_triggers"]
    return {"cagr": result.metrics.get("cagr"), "r9_halt_count": triggers["r9_halt_count"]}


def run_backtest(
    strategy: dict[str, Any],
    bars: dict[str, list[Bar]],
    signals: list[BtSignal],
    *,
    nlv: float = 1_000_000.0,
    base_currency: str = "HKD",
    rates_hkd_per_unit: dict[str, float] | None = None,
    odd_lot_enabled: bool = False,
    resume: str = "manual_no_reset",
    apply_risk: bool = True,
    compare_r9: bool = False,
    compare_strict: bool = False,
) -> BacktestResult:
    """Run one backtest. Odd lots stay off unless ``odd_lot_enabled`` is set."""
    rates = rates_hkd_per_unit or {
        "HKD": 1.0,
        "USD": 7.8,
        "TWD": 0.244,
        "JPY": 0.052,
        "KRW": 0.0056,
    }
    kwargs = {
        "nlv": nlv,
        "base": base_currency,
        "rates": rates,
        "odd_lot": odd_lot_enabled,
        "resume": resume,
        "apply_risk": apply_risk,
    }
    primary = _run_once(strategy, bars, signals, strict=False, **kwargs)
    scenarios = {resume: _scenario_summary(primary)}
    if compare_r9:
        for name in ("manual_no_reset", "reset_after_5td", "never"):
            if name == resume:
                continue
            other_kwargs = dict(kwargs)
            other_kwargs["resume"] = name
            other = _run_once(strategy, bars, signals, strict=False, **other_kwargs)
            scenarios[name] = _scenario_summary(other)
    primary.metrics["r9_resume_scenario"] = scenarios
    if compare_strict:
        strict_run = _run_once(strategy, bars, signals, strict=True, **kwargs)
        primary.metrics["fill_strict_delta"] = {
            "cagr": (strict_run.metrics.get("cagr") or 0) - (primary.metrics.get("cagr") or 0),
            "n_trades": (strict_run.metrics.get("n_trades") or 0)
            - (primary.metrics.get("n_trades") or 0),
        }
    else:
        primary.metrics["fill_strict_delta"] = None
    if apply_risk:
        plain_kwargs = dict(kwargs)
        plain_kwargs["apply_risk"] = False
        plain = _run_once(strategy, bars, signals, strict=False, **plain_kwargs)
        primary.metrics["metrics_no_risk"] = {
            "cagr": plain.metrics.get("cagr"),
            "n_trades": plain.metrics.get("n_trades"),
        }
    else:
        primary.metrics["metrics_no_risk"] = None
    return primary
