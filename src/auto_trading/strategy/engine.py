"""Strategy engine.

The engine turns an approved spec into intent files. It does not place
orders, approve its own specs, or call the risk gate. An LLM is not part
of this package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.intents.writer import write_intents
from auto_trading.market.calendar import us_open_in_macau
from auto_trading.market.instruments import describe
from auto_trading.strategy.spec import INTERPRETER_VERSION, ApprovedStrategy, require_approved

OPEN = {"JP": "08:00:00", "KR": "08:00:00", "TW": "09:00:00", "HK": "09:30:00"}


class StrategyEngine:
    """Runs a strategy document only after :func:`require_approved` succeeds."""

    def run(
        self,
        document: Mapping[str, Any],
        approved: ApprovedStrategy,
        *,
        trading_home: Path,
        session_date: str,
        signals: list[Any] | None = None,
        bars: dict[str, list[Any]] | None = None,
        nlv: float = 1_000_000.0,
        entries_allowed: bool = True,
        adr_map: list[Any] | None = None,
        events: list[dict[str, Any]] | None = None,
        news_now: datetime | None = None,
        news_sleep: Callable[[float], None] | None = None,
        news_clock: Callable[[], datetime] | None = None,
    ) -> Path:
        """Write the session intent file. A hash mismatch writes nothing."""
        digest = require_approved(document, approved)
        orders: list[Any] = []
        if bars is not None and signals is not None:
            from auto_trading.backtest.engine import plan_session

            if events is None:
                events = _session_events(
                    document,
                    session_date,
                    now=news_now,
                    sleep=news_sleep,
                    clock=news_clock,
                )
            orders = plan_session(
                dict(document),
                bars,
                signals,
                session_date,
                nlv=nlv,
                adr_map=adr_map,
                events=events,
            )
        payload = {
            "schema_version": "intent/1.0",
            "strategy_id": document["id"],
            "strategy_version": document["version"],
            "spec_hash": digest,
            "interpreter_version": INTERPRETER_VERSION,
            "mode": document.get("mode", "paper"),
            "base_currency": "HKD",
            "generated_at": f"{session_date}T07:05:00+08:00",
            "intents": [_intent(document, order, session_date) for order in orders],
        }
        return write_intents(
            trading_home,
            payload,
            session_date=session_date,
            entries_allowed=entries_allowed,
        )


def _news_dir(document: Mapping[str, Any]) -> Path | None:
    """Directory of an ``a_news`` ``path_pattern``, when the spec has one."""
    trigger = document.get("trigger")
    if not isinstance(trigger, Mapping):
        return None
    sources = trigger.get("sources")
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, Mapping) or source.get("source") != "a_news":
            continue
        pattern = source.get("path_pattern")
        if isinstance(pattern, str) and "{date}" in pattern:
            return Path(pattern).parent
    return None


def _session_events(
    document: Mapping[str, Any],
    session_date: str,
    *,
    now: datetime | None,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], datetime] | None,
) -> list[dict[str, Any]]:
    """News events for this session. Optional score fields are left out."""
    folder = _news_dir(document)
    if folder is None:
        return []
    from auto_trading.news.reader import events_for_strategy, read_session_news

    loaded = read_session_news(folder, session_date, now=now, sleep=sleep, clock=clock)
    if not loaded.enabled:
        return []
    return events_for_strategy(loaded.items)


def _intent(document: Mapping[str, Any], order: Any, session_date: str) -> dict[str, Any]:
    spec = describe(order.ticker)
    side = "BUY" if order.side == "BUY" else "SELL"
    if order.kind == "ENTRY":
        kind = "ENTRY"
    elif order.kind == "indicator_take_profit":
        kind = "REDUCE"
    else:
        kind = "EXIT"
    protective = bool(getattr(order, "protective", False))
    row = {
        "intent_id": order.intent_id,
        "intent_type": kind,
        "parent_intent_id": order.parent,
        "ticker": order.ticker,
        "con_id": None,
        "market": spec.market,
        "exchange": spec.ib_exchange,
        "currency": spec.currency,
        "side": side,
        "qty": order.qty,
        "lot_size": spec.lot_size,
        "order_type": "LMT",
        "tif": "DAY",
        "limit_offset_bps": 10,
        "outside_rth": False,
        "ref_price_at_create": order.ref_price,
        "valid_from": _valid_from(spec.market, session_date),
        "signal_id": order.signal_id,
        "confidence": order.confidence,
        "fair_value": order.fair_value,
        "exit_reason": None if kind == "ENTRY" else order.kind,
        "limit_price_cap": order.limit_cap,
        "event_ref": None
        if not order.news_id
        else {
            "event_source": order.event_source,
            "news_id": order.news_id,
            "pre_event_close": order.pre_event_close,
        },
        "strategy_id": document.get("id"),
    }
    if protective:
        row.pop("valid_from", None)
        row["protective"] = True
    return row


def _valid_from(market: str, session_date: str) -> str:
    if market == "US":
        opened = us_open_in_macau(datetime.fromisoformat(session_date).date())
        return opened.isoformat()
    clock = OPEN.get(market, "09:30:00")
    return f"{session_date}T{clock}+08:00"
