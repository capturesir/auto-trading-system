"""session_open limit prices.

An entry waits until five minutes after the open, then uses the official
open times 1.001 (10 bps), aligned to the tick, DAY. If the open is missing,
the reference falls back to the mid when the spread is within 1%.
Delayed IBKR data (market data type 3) is accepted in paper mode and logged.
The plan records which quote source produced the reference.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from auto_trading.market.ticks import tick_size

log = logging.getLogger(__name__)

OFFSET_BPS = 10
OPEN_BUFFER = timedelta(minutes=5)


@dataclass(frozen=True)
class LimitPlan:
    ready: bool
    reason: str | None
    ref_price_mode: str
    session_open_price: float | None
    ref_price_at_submit: float | None
    limit_price: float | None
    slippage_bps_vs_decision: float | None
    quote_source: str | None = None
    data_type: str | None = None
    quote_age_sec: float | None = None


def apply_quote(intent: Any, plan: LimitPlan) -> None:
    """Copy the plan's quote record onto the order that will be sent."""
    intent.quote_source = plan.quote_source
    intent.quote_data_type = plan.data_type
    intent.quote_age_sec = plan.quote_age_sec


def align_tick(price: float, tick: float, *, buy: bool) -> float:
    """BUY rounds down. SELL rounds up."""
    steps = price / tick
    if buy:
        return math.floor(steps + 1e-9) * tick
    return math.ceil(steps - 1e-9) * tick


def session_open_limit(
    *,
    market: str,
    side: str,
    now: datetime,
    session_open_at: datetime,
    open_price: float | None,
    bid: float | None,
    ask: float | None,
    last: float | None,
    decision_ref: float | None,
    offset_bps: float = OFFSET_BPS,
    cap: float | None = None,
    quote_source: str | None = None,
    data_type: str | None = None,
    quote_as_of: datetime | None = None,
) -> LimitPlan:
    """Price one order. ``ready`` is false during the first five minutes."""
    age = None if quote_as_of is None else (now - quote_as_of).total_seconds()
    if data_type == "delayed":
        log.info(
            "session_open pricing accepted delayed data source=%s market=%s",
            quote_source or "ibkr",
            market,
        )
    if now < session_open_at + OPEN_BUFFER:
        return LimitPlan(
            False,
            "R10",
            "session_open",
            open_price,
            None,
            None,
            None,
            quote_source,
            data_type,
            age,
        )
    buy = side == "BUY"
    mode = "session_open"
    reference = open_price
    if reference is None:
        mode = "live_quote"
        reference = _fallback(bid, ask, last)
        if reference is None:
            return LimitPlan(
                False, "R22", mode, None, None, None, None, quote_source, data_type, age
            )
    factor = 1 + offset_bps / 10_000 if buy else 1 - offset_bps / 10_000
    raw = reference * factor
    tick = tick_size(market, raw)
    limit = align_tick(raw, tick, buy=buy)
    if buy and cap is not None:
        if reference > cap:
            return LimitPlan(
                False,
                "CHASE_CAP_EXCEEDED",
                mode,
                open_price,
                reference,
                None,
                None,
                quote_source,
                data_type,
                age,
            )
        limit = min(limit, align_tick(cap, tick_size(market, cap), buy=True))
    slippage = None
    if decision_ref:
        slippage = (limit / decision_ref - 1) * 10_000
        if not buy:
            slippage = (decision_ref / limit - 1) * 10_000 if limit else None
    return LimitPlan(
        True, None, mode, open_price, reference, limit, slippage, quote_source, data_type, age
    )


def _fallback(bid: float | None, ask: float | None, last: float | None) -> float | None:
    if bid and ask and ask > 0 and (ask - bid) / ask <= 0.01:
        return (bid + ask) / 2
    return last
