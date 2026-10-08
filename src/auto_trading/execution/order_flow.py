"""Price, approve, re-check, then send.

Automatic mode writes a decided approval and never a pending file.
One extra approval is the program cap shared by price drift and the
submit window.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.broker import OrderIntent
from auto_trading.execution.approval_mode import (
    MANUAL,
    ApprovalSession,
    decide_approval_mode,
    record_auto_approval,
)
from auto_trading.execution.approvals import order_hash, queue_order, signed_body
from auto_trading.execution.breakers import note_execution_anomaly
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.risk.gate import GateContext, evaluate

MAX_AUTO_REAPPROVALS = 1
AUTO_SUBMIT_WINDOW_SEC = 120


@dataclass
class FlowOutcome:
    sent: bool
    code: str | None = None
    detail: str | None = None
    attempt: int = 0
    status: str = ""


def _price(intent: OrderIntent, ctx: GateContext) -> float:
    if intent.limit_price is not None:
        return float(intent.limit_price)
    quote = ctx.quotes.get(intent.symbol)
    if quote is None:
        return 0.0
    return float(quote.last)


def _blocked(intent: OrderIntent, ctx: GateContext, session: ApprovalSession) -> str | None:
    """R0 from the gate context and from a fresh account read."""
    previous = ctx.accounts
    fresh_accounts = tuple(session.read_accounts())
    ctx.accounts = fresh_accounts
    try:
        code = evaluate(intent, ctx)
    finally:
        ctx.accounts = previous
    if code and code.startswith("R0_"):
        return code
    if session.port not in (4002, 7497):
        return "R0_FORBIDDEN_PORT"
    return None


def run_entry_flow(
    *,
    trading_home: Path,
    intent: OrderIntent,
    ctx: GateContext,
    session: ApprovalSession,
    order_id: str,
    payload: dict[str, Any],
    priceable: bool,
    minutes_to_close: float | None,
    now: datetime,
    place: Callable[[OrderIntent], None],
    before_final: Callable[[], None] | None = None,
    after_reapproval: Callable[[], None] | None = None,
    before_place: Callable[[], None] | None = None,
    strategy_approval: str = "auto",
) -> FlowOutcome:
    """Queue, run pre, approve, run final, then place. Hooks run in that order."""
    TradeJournal(trading_home).record_intent(
        {
            "intent_id": intent.intent_id or order_id,
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
        }
    )
    if not priceable:
        if minutes_to_close is not None and minutes_to_close <= 10:
            TradeJournal(trading_home).record_risk_check(
                symbol=intent.symbol,
                code="APPROVAL_EXPIRED",
                intent_id=intent.intent_id,
                detail={"detail": "not_priced_by_close"},
                when=now,
            )
            return FlowOutcome(
                False,
                "APPROVAL_EXPIRED",
                "not_priced_by_close",
                status="APPROVAL_EXPIRED",
            )
        return FlowOutcome(False, status="QUEUED")
    ctx.stage = "pre"
    pre = evaluate(intent, ctx)
    if pre:
        return FlowOutcome(False, pre, status="RISK_REJECTED")
    mode = decide_approval_mode(intent, session)
    if strategy_approval != "auto":
        mode = MANUAL
    if mode == MANUAL:
        queue_order(trading_home, order_id, dict(payload))
        return FlowOutcome(False, status="PENDING_APPROVAL")
    document = record_auto_approval(
        trading_home,
        order_id,
        payload,
        session,
        attempt=1,
        approval_price=_price(intent, ctx),
        order=intent,
    )
    attempt = 1
    ctx.approval_price = float(document["approval_price"])
    if before_final is not None:
        before_final()
    while True:
        ctx.stage = "final"
        final = evaluate(intent, ctx)
        if final and final.startswith("R0_"):
            return FlowOutcome(False, final, attempt=attempt, status="RISK_REJECTED")
        if final == "REAPPROVAL_REQUIRED":
            if attempt > MAX_AUTO_REAPPROVALS:
                TradeJournal(trading_home).record_risk_check(
                    symbol=intent.symbol,
                    code="CHASE_REAPPROVAL_EXCEEDED",
                    intent_id=intent.intent_id,
                    detail={"stage": "final"},
                    when=now,
                )
                return FlowOutcome(
                    False,
                    "CHASE_REAPPROVAL_EXCEEDED",
                    attempt=attempt,
                    status="RISK_REJECTED",
                )
            ctx.stage = "pre"
            again = evaluate(intent, ctx)
            if again:
                return FlowOutcome(False, again, attempt=attempt, status="RISK_REJECTED")
            attempt = 2
            document = record_auto_approval(
                trading_home,
                order_id,
                payload,
                session,
                attempt=attempt,
                approval_price=_price(intent, ctx),
                order=intent,
            )
            ctx.approval_price = float(document["approval_price"])
            if after_reapproval is not None:
                after_reapproval()
            continue
        if final:
            return FlowOutcome(False, final, attempt=attempt, status="RISK_REJECTED")
        fresh = order_hash(
            signed_body(
                payload,
                order_id=order_id,
                approval_price=float(document["approval_price"]),
                attempt=attempt,
            )
        )
        if fresh != document["order_hash"]:
            note_execution_anomaly(
                trading_home,
                order_id=order_id,
                kind="ORDER_HASH_MISMATCH",
                now=now,
                account=intent.account,
            )
            return FlowOutcome(
                False,
                "ORDER_HASH_MISMATCH",
                attempt=attempt,
                status="RISK_REJECTED",
            )
        break
    if before_place is not None:
        before_place()
    blocked = _blocked(intent, ctx, session)
    if blocked:
        return FlowOutcome(False, blocked, attempt=attempt, status="RISK_REJECTED")
    place(intent)
    return FlowOutcome(True, attempt=attempt, status="SUBMITTED")


def expire_submit_window(
    trading_home: Path,
    *,
    intent: OrderIntent,
    ctx: GateContext,
    session: ApprovalSession,
    order_id: str,
    payload: dict[str, Any],
    decided_at: datetime,
    now: datetime,
    attempt: int,
) -> FlowOutcome:
    """After 120 seconds without a send, re-approve once, then expire."""
    if (now - decided_at).total_seconds() <= AUTO_SUBMIT_WINDOW_SEC:
        return FlowOutcome(False, attempt=attempt, status="APPROVED")
    if attempt > MAX_AUTO_REAPPROVALS:
        TradeJournal(trading_home).record_risk_check(
            symbol=intent.symbol,
            code="APPROVAL_EXPIRED",
            intent_id=intent.intent_id,
            detail={"detail": "submit_window"},
            when=now,
        )
        return FlowOutcome(False, "APPROVAL_EXPIRED", "submit_window", attempt, "APPROVAL_EXPIRED")
    record_auto_approval(
        trading_home,
        order_id,
        payload,
        session,
        attempt=2,
        approval_price=_price(intent, ctx),
        order=intent,
    )
    return FlowOutcome(False, attempt=2, status="REAPPROVED")
