"""Long-only daily event-driven backtest.

Decisions at 07:05 use bars whose ``available_at`` is already known. Fills use
the next session's official open and the limit model. The odd-lot toggle
defaults to false. R9 uses ``manual_no_reset`` unless another scenario is
requested.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from auto_trading.backtest.adr import AdrMapping, consider_adr
from auto_trading.backtest.costs import market_cost
from auto_trading.backtest.fills import try_fill
from auto_trading.backtest.fx import to_base
from auto_trading.backtest.lots import size_quantity
from auto_trading.backtest.metrics import METRIC_KEYS, compute_metrics
from auto_trading.backtest.risk_sim import RiskSimulator, fill_rejections
from auto_trading.market.instruments import describe
from auto_trading.news.timing import entry_due, event_clock, is_first_session_of_week
from auto_trading.strategy.conditions import UnknownCondition, choose_exit, confirms, exit_hits
from auto_trading.strategy.sizing import target_fraction
from auto_trading.strategy.spec import INTERPRETER_VERSION, spec_hash
from auto_trading.strategy.vocabulary import Series, atr_latest

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
    adj_close: float | None = None


@dataclass
class BtSignal:
    ticker: str
    side: str
    confidence: int
    fair_value: float
    signal_id: str
    price_date: str
    upside_pct: float | None = None
    event_source: str | None = None
    news_id: str | None = None
    pre_event_close: float | None = None
    issuer_group: str | None = None


@dataclass
class _Pos:
    qty: int
    avg_cost: float
    entry_intent: str
    stop: float
    highest: float
    hold_sessions: int = 0
    atr_at_entry: float | None = None
    pre_event_close: float | None = None
    partial_taken: bool = False
    news_id: str | None = None
    event_source: str | None = None


@dataclass
class _Order:
    ticker: str
    side: str
    qty: int
    kind: str
    intent_id: str
    parent: str | None
    decision_date: str
    limit_cap: float | None = None
    atr_at_entry: float | None = None
    pre_event_close: float | None = None
    signal_id: str | None = None
    confidence: int | None = None
    fair_value: float | None = None
    ref_price: float | None = None
    event_source: str | None = None
    news_id: str | None = None


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


def _series(rows: list[Bar]) -> Series:
    closes = [row.adj_close if row.adj_close is not None else row.close for row in rows]
    return Series(
        closes=closes,
        highs=[row.high for row in rows],
        lows=[row.low for row in rows],
        volumes=[row.volume for row in rows],
        opens=[row.open for row in rows],
        raw_close=rows[-1].close if rows else None,
    )


def _confirms(
    strategy: dict[str, Any], rows: list[Bar], refs: dict[str, Any] | None = None
) -> bool:
    return confirms(strategy, _series(rows), refs)


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
    adr_map: list[AdrMapping] | None = None,
    events: list[dict[str, Any]] | None = None,
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
        "commission": 0.0,
        "tax": 0.0,
        "slippage": 0.0,
        "traded": 0.0,
        "days_in_market": 0,
        "adr": [],
        "skipped_same": 0,
    }
    protective_distance, _stop_pct = _stop_parts(strategy)
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
                limit_cap=order.limit_cap,
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
            fee = _fee(
                spec.market,
                result.qty,
                result.fill_price * result.qty,
                day,
                order.side != "BUY",
                rates,
                base,
            )
            counters["commission"] += fee[0]
            counters["tax"] += fee[1]
            counters["slippage"] += to_base(
                abs(result.fill_price - row.open) * result.qty, spec.currency, rates, base
            )
            counters["traded"] += notional
            if order.side == "BUY":
                positions[order.ticker] = _Pos(
                    qty=result.qty,
                    avg_cost=result.fill_price,
                    entry_intent=order.intent_id,
                    stop=result.fill_price * (1 - protective_distance),
                    highest=row.close,
                    atr_at_entry=order.atr_at_entry,
                    pre_event_close=order.pre_event_close,
                    news_id=order.news_id,
                    event_source=order.event_source,
                )
                cash -= notional + fee[0] + fee[1]
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
                cash -= fee[0] + fee[1]
                if order.kind == "indicator_take_profit" and order.ticker in positions:
                    positions[order.ticker].partial_taken = True
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
        if positions:
            counters["days_in_market"] += 1
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
                strategy, ticker, pos, row, day, signals, bars, sim, apply_risk, events or []
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
                adr_map or [],
                events or [],
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
            "skipped_same_ticker": counters["skipped_same"],
            "cost_commission": counters["commission"],
            "cost_tax": counters["tax"],
            "cost_slippage": counters["slippage"],
            "equity_dates": [day for day, _value in equity],
            "exposure": counters["days_in_market"] / len(dates) if dates else None,
            "turnover_annual": _turnover(counters["traded"], equity, len(dates)),
            "excess_return_vs_benchmark": None,
            "monthly_returns": [],
            "adr_substitutions": counters["adr"],
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
    strategy, ticker, pos, row, day, signals, bars, sim, apply_risk, events
) -> _Order | None:
    history = [item for item in bars.get(ticker, []) if item.date <= day]
    series = _series(history)
    latest = _latest_signal(signals, ticker, day)
    upside = _upside(latest, row.close)
    scheduled = strategy.get("trigger", {}).get("type") == "schedule"
    rank = _rank(signals, bars, ticker, day) if scheduled else None
    refs = {
        "close": series.closes[-1] if series.closes else row.close,
        "raw_close": row.close,
        "avg_cost": pos.avg_cost,
        "entry": pos.avg_cost,
        "highest": pos.highest,
        "atr_at_entry": pos.atr_at_entry,
        "atr_now": atr_latest(series, 14),
        "fair_value": None if latest is None else latest.fair_value,
        "side": None if latest is None else latest.side,
        "upside": upside,
        "hold_sessions": pos.hold_sessions,
        "sessions_without": _sessions_without(bars, ticker, latest, day),
        "rank": rank,
        "pnl_pct": (row.close / pos.avg_cost - 1) * 100 if pos.avg_cost else None,
        "pre_event_close": pos.pre_event_close,
        "partial_taken": pos.partial_taken,
        "event_reversed": _event_reversed(events, ticker, pos.news_id, day),
        "market": describe(ticker).market,
        "limit_up": _limit_up(describe(ticker).market, history),
    }
    chosen = choose_exit(exit_hits(strategy, series, refs))
    if chosen is None:
        return None
    reason, fraction = chosen
    qty = pos.qty if fraction >= 1 else int(pos.qty * fraction)
    if qty <= 0:
        return None
    if apply_risk and sim.allow("SELL", "exit"):
        return None
    return _Order(
        ticker,
        "SELL",
        qty,
        reason,
        f"{day}-{ticker}-exit",
        pos.entry_intent,
        day,
        signal_id=None if latest is None else latest.signal_id,
        confidence=None if latest is None else latest.confidence,
        fair_value=None if latest is None else latest.fair_value,
        ref_price=row.close,
    )


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
    adr_map,
    events,
):
    orders: list[_Order] = []
    trigger = strategy.get("trigger") or {}
    kind = trigger.get("type")
    if kind == "schedule":
        if not _rebalance_day(bars, day):
            return orders
        ranked = _rotation_candidates(strategy, bars, signals, day)
        universe = int((strategy.get("universe") or {}).get("min_universe_size") or 0)
        if universe and len(ranked) < universe:
            return orders
        top_n = int((strategy.get("selection") or {}).get("top_n") or len(ranked))
        chosen = ranked[:top_n]
    elif kind == "event":
        chosen = _event_signals(strategy, bars, signals, events, day, decision_time)
    elif kind in {None, "signal"}:
        chosen = list(signals)
    else:
        raise UnknownCondition(str(kind))
    minimum = int(trigger.get("min_confidence", 60))
    opened: set[str] = set()
    for signal in chosen:
        if signal.price_date > day or signal.ticker in positions or signal.side != "BUY":
            continue
        if signal.ticker in opened:
            counters["skipped_same"] += 1
            continue
        if signal.confidence < minimum:
            continue
        history = [row for row in bars.get(signal.ticker, []) if row.date < day]
        assert_visible(history, decision_time)
        price = history[-1].close if history else signal.fair_value
        upside = _upside(signal, price)
        floor = trigger.get("min_upside_live_pct")
        if kind == "schedule":
            entry_filter = (strategy.get("selection") or {}).get("entry_filter", {})
            floor = entry_filter.get("min_upside_live_pct", floor)
        if floor is not None and (upside is None or upside < float(floor)):
            continue
        refs = _entry_refs(signal, history)
        if strategy.get("confirm") and not _confirms(strategy, history, refs):
            continue
        if apply_risk and sim.allow("BUY", "entry"):
            continue
        order = _sized_order(
            strategy,
            signal,
            history,
            nav,
            odd_lot,
            rates,
            base,
            day,
            counters,
            adr_map,
            signals,
            bars,
        )
        if order is None:
            continue
        opened.add(order.ticker)
        orders.append(order)
    return orders


def _latest_signal(signals: list[BtSignal], ticker: str, day: str) -> BtSignal | None:
    found = [item for item in signals if item.ticker == ticker and item.price_date <= day]
    if not found:
        return None
    return max(found, key=lambda item: item.price_date)


def _upside(signal: BtSignal | None, price: float | None) -> float | None:
    if signal is None:
        return None
    if price and signal.fair_value:
        return (signal.fair_value / price - 1) * 100
    return signal.upside_pct


def _sessions_without(bars, ticker: str, latest: BtSignal | None, day: str) -> int:
    rows = [row for row in bars.get(ticker, []) if row.date <= day]
    if latest is None:
        return len(rows)
    return sum(1 for row in rows if row.date > latest.price_date)


def _rank(signals, bars, ticker: str, day: str) -> int | None:
    scored: list[tuple[float, str]] = []
    for signal in signals:
        history = [row for row in bars.get(signal.ticker, []) if row.date <= day]
        if not history:
            continue
        scored.append((_upside(signal, history[-1].close) or -10_000, signal.ticker))
    scored.sort(reverse=True)
    for index, (_score, name) in enumerate(scored, start=1):
        if name == ticker:
            return index
    return None


def _limit_up(market: str, rows: list[Bar]) -> bool:
    if market in {"US", "HK"} or len(rows) < 2:
        return False
    band = {"TW": 0.10, "KR": 0.30}.get(market, 0.15)
    last = rows[-1]
    return last.close >= rows[-2].close * (1 + band) - 1e-6 and last.close >= last.high - 1e-9


def _event_reversed(
    events: list[dict[str, Any]], ticker: str, news_id: str | None, day: str
) -> bool:
    from auto_trading.news.reader import root_news_id

    root = root_news_id(news_id) if news_id else ""
    for event in events:
        if event.get("ticker") != ticker and not event.get("ticker_removed"):
            continue
        if event.get("ticker_removed") and event.get("ticker") not in {None, ticker}:
            continue
        judged = str(event.get("judged_at") or "")
        if judged and judged[:10] > day:
            continue
        event_id = str(event.get("news_id") or "")
        same = bool(root) and root_news_id(event_id) == root
        status = event.get("status")
        if status == "retracted" and (same or event_id == news_id):
            return True
        if event.get("direction") == "negative" and int(event.get("impact") or 0) >= 2:
            return True
        if not (same and status == "update"):
            continue
        impact = int(event.get("impact") or 0)
        disqualified = (
            event.get("direction") != "positive"
            or impact < 2
            or event.get("event_type") in {"rating", "other"}
            or event.get("ticker_removed") is True
        )
        if disqualified:
            return True
    return False


def _entry_refs(signal: BtSignal, history: list[Bar]) -> dict[str, Any]:
    market = describe(signal.ticker).market
    return {
        "pre_event_close": signal.pre_event_close,
        "event_source": signal.event_source,
        "market": market,
        "limit_up": _limit_up(market, history),
    }


def _stop_distance(strategy: dict[str, Any], series: Series, price: float) -> float:
    cap = 12.0
    multiple = 2.0
    for rule in strategy.get("exits") or []:
        if rule.get("type") == "stop_loss_pct":
            cap = float(rule["value"])
        if rule.get("type") == "stop_atr":
            multiple = float(rule["multiple"])
    size = (strategy.get("action") or {}).get("size") or {}
    if size.get("type") == "risk_pct" and size.get("stop_ref") == "stop_atr" and price:
        atr = atr_latest(series, 14)
        if atr:
            return min(atr * multiple / price * 100, cap)
    return cap


def _fee(market, qty, notional_local, day, sell, rates, base) -> tuple[float, float]:
    cost = market_cost(market, qty, notional_local, date.fromisoformat(day), sell=sell)
    return (
        to_base(cost.commission, cost.currency, rates, base),
        to_base(cost.tax, cost.currency, rates, base),
    )


def _turnover(traded: float, equity: list[tuple[str, float]], n_days: int) -> float | None:
    if not equity or n_days <= 0:
        return None
    average = sum(value for _day, value in equity) / len(equity)
    years = n_days / 252
    if average <= 0 or years <= 0:
        return None
    return traded / average / years


def _rebalance_day(bars: dict[str, list[Bar]], day: str) -> bool:
    session = date.fromisoformat(day)
    return any(is_first_session_of_week(describe(ticker).market, session) for ticker in bars)


def _rotation_candidates(strategy, bars, signals, day: str) -> list[BtSignal]:
    selection = strategy.get("selection") or {}
    entry = selection.get("entry_filter") or {}
    sides = set(entry.get("side") or ["BUY"])
    minimum = int(entry.get("min_confidence", 60))
    min_upside = entry.get("min_upside_live_pct")
    min_return = entry.get("min_return_20d_pct")
    session = date.fromisoformat(day)
    ranked: list[tuple[float, BtSignal]] = []
    for signal in signals:
        if signal.side not in sides or signal.confidence < minimum or signal.price_date > day:
            continue
        history = [row for row in bars.get(signal.ticker, []) if row.date < day]
        if not history:
            continue
        market = describe(signal.ticker).market
        if not is_first_session_of_week(market, session):
            continue
        if min_return is not None and len(history) > 20:
            change = (history[-1].close / history[-21].close - 1) * 100
            if change < float(min_return):
                continue
        upside = _upside(signal, history[-1].close)
        if min_upside is not None and (upside is None or upside < float(min_upside)):
            continue
        ranked.append((upside or -10_000, signal))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [signal for _score, signal in ranked]


def _event_signals(strategy, bars, signals, events, day: str, decision_time: str) -> list[BtSignal]:
    timing = (strategy.get("trigger") or {}).get("timing") or {}
    max_age = int(timing.get("max_age_sessions") or 1)
    session = date.fromisoformat(day)
    chosen: list[BtSignal] = []
    for event in events:
        ticker = str(event.get("ticker") or "")
        if not ticker:
            continue
        clock = event_clock(
            event,
            describe(ticker).market,
            update=event.get("update"),
            max_age_sessions=max_age,
        )
        if not entry_due(clock, session):
            continue
        if clock.available_at is not None and clock.available_at.isoformat() > decision_time:
            continue
        base_signal = _latest_signal(signals, ticker, day)
        if base_signal is None or base_signal.side != "BUY":
            continue
        pre_close = None
        if clock.pre_event_session is not None:
            row = _bar(bars, ticker, clock.pre_event_session.isoformat())
            if row is not None:
                pre_close = row.close
        chosen.append(
            BtSignal(
                ticker,
                "BUY",
                base_signal.confidence,
                base_signal.fair_value,
                base_signal.signal_id,
                base_signal.price_date,
                event_source=str(event.get("source") or "a_news"),
                news_id=str(event.get("news_id") or ""),
                pre_event_close=pre_close,
            )
        )
    return chosen


def _order_from(
    signal: BtSignal, qty: int, day: str, price: float, history: list[Bar], strategy: dict[str, Any]
) -> _Order:
    series = _series(history) if history else Series(closes=[price])
    cap = None
    limit_rule = ((strategy.get("action") or {}).get("order") or {}).get("limit_cap")
    if isinstance(limit_rule, dict) and signal.pre_event_close:
        cap = signal.pre_event_close * (1 + float(limit_rule["max_pct"]) / 100)
    return _Order(
        signal.ticker,
        "BUY",
        qty,
        "ENTRY",
        f"{day}-{signal.ticker}-entry",
        None,
        day,
        limit_cap=cap,
        atr_at_entry=atr_latest(series, 14),
        pre_event_close=signal.pre_event_close,
        signal_id=signal.signal_id,
        confidence=signal.confidence,
        fair_value=signal.fair_value,
        ref_price=price,
        event_source=signal.event_source,
        news_id=signal.news_id,
    )


def _sized_order(
    strategy,
    signal,
    history,
    nav,
    odd_lot,
    rates,
    base,
    day,
    counters,
    adr_map,
    signals,
    bars,
    *,
    allow_adr: bool = True,
) -> _Order | None:
    spec = describe(signal.ticker, odd_lot_enabled=odd_lot)
    price = history[-1].close if history else signal.fair_value
    series = _series(history) if history else Series(closes=[])
    weight = target_fraction(
        strategy["action"]["size"],
        confidence=signal.confidence,
        stop_distance_pct=_stop_distance(strategy, series, price),
    )
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
    if decision.status == "ok":
        return _order_from(signal, decision.qty, day, price, history, strategy)
    if allow_adr and decision.status == "LOT_TOO_LARGE" and decision.binding_limit == "order_cap":
        replacement = _adr_order(
            strategy, signal, nav, odd_lot, rates, base, day, adr_map, signals, bars, counters
        )
        if replacement is not None:
            return replacement
    if decision.status == "LOT_TOO_LARGE":
        counters["lot_too_large"] += 1
    else:
        counters["below_min"] += 1
    return None


def _adr_order(
    strategy, signal, nav, odd_lot, rates, base, day, adr_map, signals, bars, counters
) -> _Order | None:
    mapped = next((row for row in adr_map if row.local_ticker == signal.ticker), None)
    adr_signal = None
    if mapped is not None:
        adr_signal = next(
            (
                item
                for item in signals
                if (
                    item.ticker == mapped.adr_ticker
                    and item.price_date <= day
                    and item.side == "BUY"
                )
            ),
            None,
        )
    twd_per_usd = rates["USD"] / rates["TWD"] if rates.get("TWD") else None
    verdict = consider_adr(
        strategy_id=str(strategy.get("id")),
        local_ticker=signal.ticker,
        odd_lot_enabled=odd_lot,
        whole_lot_exceeds_cap=True,
        mappings=adr_map,
        as_of=date.fromisoformat(day),
        adr_signal_side=None if adr_signal is None else adr_signal.side,
        adr_signal_id=None if adr_signal is None else adr_signal.signal_id,
        fv_local=signal.fair_value,
        fv_adr=None if adr_signal is None else adr_signal.fair_value,
        twd_per_usd=twd_per_usd,
    )
    if not verdict.substitute or adr_signal is None or mapped is None:
        return None
    history = [row for row in bars.get(mapped.adr_ticker, []) if row.date < day]
    if not history or not _confirms(strategy, history, _entry_refs(adr_signal, history)):
        return None
    scratch = {"remainder": 0, "lot_too_large": 0, "below_min": 0}
    order = _sized_order(
        strategy,
        adr_signal,
        history,
        nav,
        False,
        rates,
        base,
        day,
        scratch,
        [],
        signals,
        bars,
        allow_adr=False,
    )
    if order is None:
        return None
    counters["remainder"] += scratch["remainder"]
    counters["adr"].append(
        {
            "local": signal.ticker,
            "adr": mapped.adr_ticker,
            "day": day,
            "signal_id": adr_signal.signal_id,
        }
    )
    return order


def _snapshot(bars: dict[str, list[Bar]]) -> str:
    parts = [
        f"{ticker}|{bar.date}|{bar.open}|{bar.high}|{bar.low}|{bar.close}"
        for ticker in sorted(bars)
        for bar in bars[ticker]
    ]
    return "sha256:" + hashlib.sha256("\n".join(parts).encode()).hexdigest()


def plan_session(
    strategy: dict[str, Any],
    bars: dict[str, list[Bar]],
    signals: list[BtSignal],
    session_date: str,
    *,
    nlv: float = 1_000_000.0,
    base_currency: str = "HKD",
    rates_hkd_per_unit: dict[str, float] | None = None,
    odd_lot_enabled: bool = False,
    adr_map: list[AdrMapping] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> list[_Order]:
    """Opening and exit orders for one 07:05 decision, starting from a flat book."""
    rates = rates_hkd_per_unit or {
        "HKD": 1.0,
        "USD": 7.8,
        "TWD": 0.244,
        "JPY": 0.052,
        "KRW": 0.0056,
    }
    counters: dict[str, Any] = {
        "remainder": 0,
        "lot_too_large": 0,
        "below_min": 0,
        "adr": [],
        "skipped_same": 0,
    }
    return _entries(
        strategy,
        bars,
        signals,
        session_date,
        f"{session_date}T07:05:00+08:00",
        {},
        nlv,
        odd_lot_enabled,
        rates,
        base_currency,
        RiskSimulator(hwm=nlv),
        True,
        counters,
        adr_map or [],
        events or [],
    )


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
    adr_map: list[AdrMapping] | None = None,
    events: list[dict[str, Any]] | None = None,
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
        "adr_map": adr_map,
        "events": events,
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
