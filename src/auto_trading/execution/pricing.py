"""session_open limit prices.

An entry waits until five minutes after the open, then uses the official
open times 1.001 (10 bps), aligned to the tick, DAY. If the open is missing,
the reference falls back to the mid when the spread is within 1%.
Delayed IBKR types 3 and 4 are accepted in paper mode and logged. Type 2 is
not. A missing official open is not replaced. The plan records the source,
the integer market-data type, and the delay.
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
    quote_delay_sec: float | None = None
    market_data_type: int | None = None


def apply_quote(intent: Any, plan: LimitPlan) -> None:
    """Copy the plan's quote record onto the order that will be sent."""
    intent.quote_source = plan.quote_source
    intent.quote_data_type = plan.data_type
    intent.quote_age_sec = plan.quote_age_sec
    intent.quote_delay_sec = plan.quote_delay_sec
    intent.quote_market_data_type = plan.market_data_type


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
    quote_received_at: datetime | None = None,
    market_data_type: int | None = None,
) -> LimitPlan:
    """Price one order. ``ready`` is false during the first five minutes.

    ``quote_age_sec`` is seconds since receipt. ``quote_delay_sec`` is how far
    the exchange timestamp lags that receipt.
    """
    received = quote_received_at or now
    age = (now - received).total_seconds()
    delay = None if quote_as_of is None else (received - quote_as_of).total_seconds()
    log.info(
        "quote_source=%s market_data_type=%s quote_data_type=%s delay_sec=%s",
        quote_source,
        market_data_type,
        data_type,
        delay,
    )
    delayed_ok = data_type == "delayed" and market_data_type in (3, 4)
    blocked = quote_source == "yahoo" or data_type == "public" or market_data_type == 2
    if data_type == "delayed" and not delayed_ok:
        blocked = True
    if data_type == "delayed":
        log.info(
            "session_open pricing accepted delayed data source=%s market=%s",
            quote_source or "ibkr",
            market,
        )
    if blocked:
        return LimitPlan(
            False,
            "R22",
            "session_open",
            open_price,
            None,
            None,
            None,
            quote_source,
            data_type,
            age,
            delay,
            market_data_type,
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
            delay,
            market_data_type,
        )
    buy = side == "BUY"
    mode = "session_open"
    reference = open_price
    if reference is None:
        return LimitPlan(
            False,
            "OPEN_UNAVAILABLE",
            mode,
            None,
            None,
            None,
            None,
            quote_source,
            data_type,
            age,
            delay,
            market_data_type,
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
                delay,
                market_data_type,
            )
        limit = min(limit, align_tick(cap, tick_size(market, cap), buy=True))
    slippage = None
    if decision_ref:
        slippage = (limit / decision_ref - 1) * 10_000
        if not buy:
            slippage = (decision_ref / limit - 1) * 10_000 if limit else None
    return LimitPlan(
        True,
        None,
        mode,
        open_price,
        reference,
        limit,
        slippage,
        quote_source,
        data_type,
        age,
        delay,
        market_data_type,
    )
