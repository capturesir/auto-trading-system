"""Pure checks for one open position.

Entry-style checks (moving averages, prior-day range, volume, session range)
run only while that market is open. Stop distance still uses the last price
when the market is closed. A broker STP price wins over ``protective_stop_pct``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from auto_trading.alerts.holdings_positions import PositionView
from auto_trading.execution.sessions import session_window

PCT_MOVE = 0.03
DAY_RANGE_EDGE = 0.15
VOL_SPIKE = 2.0
STOP_WARN_PCT = 0.02
STOP_CRIT_PCT = 0.005
OPEN_WINDOWS = frozenset({"continuous", "open_buffer", "close_buffer"})
SESSION_RULES = frozenset(
    {
        "hm_ma20",
        "hm_ma50",
        "hm_prev_high",
        "hm_prev_low",
        "hm_vol20",
        "hm_day_range_pct",
    }
)


@dataclass(frozen=True)
class BarContext:
    ma20: float | None = None
    ma50: float | None = None
    atr: float | None = None
    vol20: float | None = None
    prior_high: float | None = None
    prior_low: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    session_open: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class StopDecision:
    price: float | None
    source: str | None


@dataclass(frozen=True)
class MarketSnap:
    last: float
    source: str = "ibkr"
    delay_sec: float | None = None
    open_price: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@dataclass(frozen=True)
class AlertDraft:
    rule_id: str
    alert_type: str
    severity: str
    ticker: str
    direction: str
    message_zh: str
    context: dict[str, Any] = field(default_factory=dict)


def market_open(market: str | None, now: datetime) -> bool:
    """True during the continuous session, including the open and close buffers."""
    if not market or now.tzinfo is None:
        return False
    try:
        window = session_window(market.upper(), now)
    except (KeyError, ValueError, TypeError):
        return False
    return window in OPEN_WINDOWS


def resolve_stop(position: PositionView, broker_stop: float | None = None) -> StopDecision:
    """Broker working STP first, otherwise the strategy percent, otherwise metadata."""
    if position.protective_stop_exempt:
        return StopDecision(None, None)
    broker = broker_stop
    if (broker is None or broker <= 0) and position.stop_source == "broker":
        broker = position.stop_price
    if broker is not None and broker > 0:
        return StopDecision(float(broker), "broker")
    avg = position.avg_cost
    pct = position.protective_stop_pct
    if avg is not None and avg > 0 and pct is not None:
        distance = float(pct) / 100.0
        if position.side == "short":
            return StopDecision(avg * (1.0 + distance), "strategy")
        return StopDecision(avg * (1.0 - distance), "strategy")
    if position.stop_price is not None and position.stop_price > 0:
        if position.stop_source in {"broker", "strategy"}:
            source = position.stop_source
        else:
            source = "strategy"
        return StopDecision(float(position.stop_price), source)
    return StopDecision(None, None)


def _pct(last: float, base: float | None) -> float | None:
    if base is None or base == 0:
        return None
    return (last - base) / base


def _base_context(
    position: PositionView,
    snap: MarketSnap,
    bars: BarContext | None,
    stop: StopDecision,
    account_status: str,
) -> dict[str, Any]:
    return {
        "position_qty": position.qty,
        "avg_cost": position.avg_cost,
        "pct_from_entry": _pct(snap.last, position.avg_cost),
        "strategy_id": position.strategy_id,
        "stop_price": stop.price,
        "stop_source": stop.source,
        "atr": None if bars is None else bars.atr,
        "account_status": account_status,
        "delay_sec": snap.delay_sec,
        "side": position.side,
    }


def _draft(
    *,
    rule_id: str,
    alert_type: str,
    severity: str,
    position: PositionView,
    direction: str,
    message_zh: str,
    context: dict[str, Any],
) -> AlertDraft:
    return AlertDraft(
        rule_id=rule_id,
        alert_type=alert_type,
        severity=severity,
        ticker=position.ticker,
        direction=direction,
        message_zh=message_zh,
        context=dict(context),
    )


def _stop_band(position: PositionView, last: float, stop: float, atr: float | None) -> str | None:
    distance = (stop - last) if position.side == "short" else (last - stop)
    if distance <= 0:
        return "critical"
    ratio = distance / last if last else 1.0
    if ratio <= STOP_CRIT_PCT:
        return "critical"
    if atr is not None and atr > 0 and distance <= atr:
        return "warning"
    if ratio <= STOP_WARN_PCT:
        return "warning"
    return None


def evaluate_position(
    position: PositionView,
    snap: MarketSnap,
    bars: BarContext | None,
    *,
    market_is_open: bool,
    broker_stop: float | None,
    account_status: str,
) -> list[AlertDraft]:
    """Return the alerts this position is in breach of on one tick."""
    if snap.last <= 0:
        return []
    bars = bars or BarContext()
    stop = resolve_stop(position, broker_stop)
    context = _base_context(position, snap, bars, stop, account_status)
    drafts: list[AlertDraft] = []
    move = context["pct_from_entry"]
    if isinstance(move, float) and abs(move) >= PCT_MOVE:
        down = move < 0
        adverse = down if position.side == "long" else not down
        direction = "down" if adverse else "up"
        drafts.append(
            _draft(
                rule_id="hm_vs_entry",
                alert_type="holdings_pct_move",
                severity="warning",
                position=position,
                direction=direction,
                message_zh=f"{position.ticker} 現價較成本變動 {move:.1%}。",
                context=context,
            )
        )
    if market_is_open:
        drafts.extend(_session_checks(position, snap, bars, context))
    if stop.price is not None:
        band = _stop_band(position, snap.last, stop.price, bars.atr)
        if band is not None:
            source = stop.source or "strategy"
            drafts.append(
                _draft(
                    rule_id="hm_stop",
                    alert_type="holdings_stop_proximity",
                    severity=band,
                    position=position,
                    direction="down" if position.side == "long" else "up",
                    message_zh=(
                        f"{position.ticker} 接近保護止損（{source}），"
                        f"止損價 {stop.price:.4g}，現價 {snap.last:.4g}。"
                    ),
                    context=context,
                )
            )
    return [item for item in drafts if market_is_open or item.rule_id not in SESSION_RULES]


def _session_checks(
    position: PositionView,
    snap: MarketSnap,
    bars: BarContext,
    context: dict[str, Any],
) -> list[AlertDraft]:
    found: list[AlertDraft] = []
    high = snap.high if snap.high is not None else bars.session_high
    low = snap.low if snap.low is not None else bars.session_low
    if high is not None and low is not None and high > low:
        place = (snap.last - low) / (high - low)
        near_low = place <= DAY_RANGE_EDGE
        near_high = place >= 1.0 - DAY_RANGE_EDGE
        adverse = near_low if position.side == "long" else near_high
        if adverse:
            found.append(
                _draft(
                    rule_id="hm_day_range_pct",
                    alert_type="holdings_pct_move",
                    severity="warning",
                    position=position,
                    direction="down" if position.side == "long" else "up",
                    message_zh=f"{position.ticker} 現價貼近本日區間邊緣。",
                    context=context,
                )
            )
    volume = snap.volume if snap.volume is not None else bars.volume
    spiked = (
        volume is not None
        and bars.vol20 is not None
        and bars.vol20 > 0
        and volume >= VOL_SPIKE * bars.vol20
    )
    if spiked:
        found.append(
            _draft(
                rule_id="hm_vol20",
                alert_type="holdings_volume_spike",
                severity="warning",
                position=position,
                direction="up",
                message_zh=f"{position.ticker} 成交量高於 20 日均量。",
                context=context,
            )
        )
    for rule_id, level in (("hm_ma20", bars.ma20), ("hm_ma50", bars.ma50)):
        if level is None:
            continue
        crossed = snap.last < level if position.side == "long" else snap.last > level
        if not crossed:
            continue
        period = rule_id.removeprefix("hm_ma")
        if position.side == "long":
            text = f"{position.ticker} 現價跌破 MA{period}。"
            direction = "down"
        else:
            text = f"{position.ticker} 現價升破 MA{period}。"
            direction = "up"
        found.append(
            _draft(
                rule_id=rule_id,
                alert_type="holdings_ma_break",
                severity="warning",
                position=position,
                direction=direction,
                message_zh=text,
                context=context,
            )
        )
    if bars.prior_high is not None and snap.last > bars.prior_high:
        found.append(
            _draft(
                rule_id="hm_prev_high",
                alert_type="holdings_day_range",
                severity="warning",
                position=position,
                direction="up",
                message_zh=f"{position.ticker} 現價高於前日高位。",
                context=context,
            )
        )
    if bars.prior_low is not None and snap.last < bars.prior_low:
        found.append(
            _draft(
                rule_id="hm_prev_low",
                alert_type="holdings_day_range",
                severity="warning",
                position=position,
                direction="down",
                message_zh=f"{position.ticker} 現價低於前日低位。",
                context=context,
            )
        )
    return found


def bars_from_rows(rows: list[dict[str, Any]]) -> BarContext | None:
    """MA, ATR, average volume, and the last completed daily range."""
    from auto_trading.market.indicators import atr_wilder, sma

    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    for row in rows:
        close = row.get("adj_close")
        if close is None:
            close = row.get("close")
        if close is None:
            continue
        try:
            price = float(close)
            high = float(row.get("high") if row.get("high") is not None else price)
            low = float(row.get("low") if row.get("low") is not None else price)
            raw_volume = row.get("volume")
            volume = 0.0 if raw_volume is None else float(raw_volume)
        except (TypeError, ValueError):
            continue
        closes.append(price)
        highs.append(high)
        lows.append(low)
        volumes.append(volume)
    if not closes:
        return None
    ma20 = sma(closes, 20)[-1] if len(closes) >= 20 else None
    ma50 = sma(closes, 50)[-1] if len(closes) >= 50 else None
    atr = None
    if len(closes) >= 14:
        series = atr_wilder(highs, lows, closes, 14)
        atr = series[-1] if series else None
    vol20 = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else None
    return BarContext(
        ma20=ma20,
        ma50=ma50,
        atr=atr,
        vol20=vol20,
        prior_high=highs[-1],
        prior_low=lows[-1],
    )


def apply_status_severity(severity: str, account_status: str) -> str:
    """KILL_SWITCH raises warning alerts. Stop alerts stay critical."""
    if account_status == "kill_switch" and severity == "warning":
        return "critical"
    if severity not in {"warning", "critical"}:
        return "warning"
    return severity
