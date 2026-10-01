"""Risk gate. ``allow`` is the only check that may release an order.

R0 runs first and is never waived. Caps come from the same limits object
that ``state/risk_limits.json`` publishes. Exits skip the opening-only
rules. R23 records a streak and does not change the mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from auto_trading.broker import OrderIntent
from auto_trading.execution.contracts import UnsupportedInstrument, map_contract
from auto_trading.execution.r0 import DU_ACCOUNT, PAPER_PORTS
from auto_trading.risk.limits import EXPECTED_LIMITS

OPENING = frozenset({"ENTRY", "ADD"})
REDUCING = frozenset({"REDUCE", "EXIT"})
MARKETS = frozenset({"US", "HK", "JP", "KR", "TW"})


class RiskRejected(Exception):
    """The gate refused the intent. The broker must not be called."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class OpenOrder:
    intent_id: str
    symbol: str
    side: str
    active: bool = True
    protective: bool = False
    opened_at: datetime | None = None


@dataclass
class Held:
    symbol: str
    quantity: float
    market_value_base: float
    strategy_id: str | None = None


@dataclass
class Quote:
    last: float
    as_of: datetime
    bid: float | None = None
    ask: float | None = None
    open_price: float | None = None
    delayed: bool = False


@dataclass
class GateContext:
    """In-process snapshot. The gate does not open a socket."""

    port: int = 4002
    accounts: tuple[str, ...] = ("DU1234567",)
    expected_account: str = ""
    baseline_accounts: tuple[str, ...] | None = None
    nlv: float = 1_000_000.0
    cash: float = 1_000_000.0
    hwm: float = 1_000_000.0
    day_start_nlv: float = 1_000_000.0
    limits: dict[str, Any] = field(default_factory=lambda: dict(EXPECTED_LIMITS))
    positions: list[Held] = field(default_factory=list)
    open_orders: list[OpenOrder] = field(default_factory=list)
    quotes: dict[str, Quote] = field(default_factory=dict)
    fx_to_base: dict[str, float] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 2, 9, 10, tzinfo=UTC))
    kill_switch: bool = False
    recon_ok: bool = True
    connected: bool = True
    orders_today: int = 0
    paused: bool = False
    registry_mode: str = "paper"
    approved_hash: str | None = None
    signal_side: str | None = "BUY"
    signal_confidence: int | None = 70
    signal_expired: bool = False
    fair_value: float | None = None
    macro_blackout: bool = False
    minutes_since_open: float | None = 10
    loss_streak: int = 0
    stage: str = "pre"
    approval_price: float | None = None
    reapproval_deviation: float = 0.015
    quote_stale_sec: int = 10
    r23_note: str | None = None


class RiskGate:
    """Rejects an intent by raising :class:`RiskRejected`."""

    def __init__(self, context: GateContext | None = None) -> None:
        if context is None:
            context = GateContext(connected=False, recon_ok=False)
        self.context = context

    def allow(self, intent: OrderIntent) -> None:
        """Raise when the intent must not continue to the broker."""
        code = evaluate(intent, self.context)
        if code:
            raise RiskRejected(code)


def evaluate(intent: OrderIntent, ctx: GateContext) -> str | None:
    """Return the first failing rule code, or None when the order may proceed."""
    code = _r0(intent, ctx)
    if code:
        return code
    if ctx.kill_switch:
        return "R18"
    if not ctx.connected or not ctx.recon_ok:
        return "R17"
    if ctx.paused and intent.intent_type in OPENING:
        return "R18"
    if intent.mode != "paper" or intent.live_size_multiplier is not None:
        return "LIVE_NOT_AUTHORIZED"
    if intent.spec_hash is not None and ctx.approved_hash not in {None, intent.spec_hash}:
        return "R25"
    if ctx.registry_mode != "paper":
        return "LIVE_NOT_AUTHORIZED"
    market = _market(intent)
    if market is None:
        return "R26"
    held_qty = _position_qty(ctx, intent)
    orphan_exit = intent.intent_type in REDUCING and not intent.parent_intent_id
    if orphan_exit and not held_qty:
        return "R27"
    if intent.side.value == "SELL" and intent.quantity > held_qty + 1e-9:
        return "R20"
    key = intent.intent_id or intent.client_id
    active = [row for row in ctx.open_orders if row.active and row.intent_id == key]
    if active:
        return "R11"
    reducing = intent.intent_type in REDUCING or intent.reduce_only or intent.protective
    if not reducing:
        opening = _opening_rules(intent, ctx, market)
        if opening:
            return opening
    quote_code = _quote_rules(intent, ctx)
    if quote_code:
        return quote_code
    if ctx.stage == "final" and ctx.approval_price and _price(intent, ctx):
        price = _price(intent, ctx)
        if price and abs(price / ctx.approval_price - 1) > ctx.reapproval_deviation:
            return "REAPPROVAL_REQUIRED"
    if ctx.loss_streak >= int(ctx.limits["R23_consecutive_loss_demote"]):
        ctx.r23_note = "r23_demote_count"
    return None


def _r0(intent: OrderIntent, ctx: GateContext) -> str | None:
    if ctx.port not in PAPER_PORTS:
        return "R0_LIVE_PORT"
    if not ctx.accounts or any(DU_ACCOUNT.fullmatch(account) is None for account in ctx.accounts):
        return "R0_NOT_PAPER_ACCOUNT"
    if ctx.expected_account and ctx.expected_account not in ctx.accounts:
        return "R0_NOT_PAPER_ACCOUNT"
    baseline = ctx.baseline_accounts
    if baseline is not None and frozenset(ctx.accounts) != frozenset(baseline):
        return "R0_ACCOUNTS_CHANGED"
    if intent.account and DU_ACCOUNT.fullmatch(intent.account) is None:
        return "R0_NOT_PAPER_ACCOUNT"
    return None


def _market(intent: OrderIntent) -> str | None:
    if intent.market:
        return intent.market if intent.market in MARKETS else None
    try:
        return map_contract(intent.symbol).market
    except UnsupportedInstrument:
        return None


def _position_qty(ctx: GateContext, intent: OrderIntent) -> float:
    return sum(row.quantity for row in ctx.positions if row.symbol == intent.symbol)


def _price(intent: OrderIntent, ctx: GateContext) -> float | None:
    if intent.limit_price is not None:
        return intent.limit_price
    quote = ctx.quotes.get(intent.symbol)
    if quote is None:
        return None
    return quote.last


def _notional(intent: OrderIntent, ctx: GateContext) -> float | None:
    price = _price(intent, ctx)
    if price is None:
        return None
    currency = intent.currency or map_contract(intent.symbol).currency
    fx = ctx.fx_to_base.get(currency, 1.0)
    return intent.quantity * price * fx


def _opening_rules(intent: OrderIntent, ctx: GateContext, market: str) -> str | None:
    if ctx.minutes_since_open is not None and ctx.minutes_since_open < 5:
        return "R10"
    if ctx.macro_blackout and market in {"US", "JP"}:
        return "R21"
    if intent.confidence is not None and intent.confidence < int(ctx.limits["min_confidence"]):
        return "R19"
    if ctx.signal_expired or (ctx.signal_side is not None and ctx.signal_side != "BUY"):
        return "SIGNAL_NOT_BUY"
    floor = int(ctx.limits["min_confidence"])
    if ctx.signal_confidence is not None and ctx.signal_confidence < floor:
        return "SIGNAL_NOT_BUY"
    same_symbol = [
        row
        for row in ctx.open_orders
        if row.active and row.symbol == intent.symbol and row.side == "BUY"
    ]
    if same_symbol:
        return "R14"
    if ctx.orders_today >= int(ctx.limits["R15_max_orders_per_day"]):
        return "R15"
    last = _last_same_side(ctx, intent)
    if last is not None and ctx.now - last < timedelta(minutes=60):
        return "R13"
    daily = ctx.nlv / ctx.day_start_nlv - 1 if ctx.day_start_nlv else 0.0
    if daily <= float(ctx.limits["R8_daily_loss_halt_pct"]):
        return "R8"
    drawdown = ctx.nlv / ctx.hwm - 1 if ctx.hwm else 0.0
    if drawdown <= float(ctx.limits["R9_drawdown_halt_pct"]):
        return "R9"
    notional = _notional(intent, ctx)
    if notional is None:
        return "R22"
    pct_cap = ctx.nlv * float(ctx.limits["R1_order_size_pct_nlv"])
    cash_cap = float(ctx.limits["R2_max_order_notional_base"])
    cap = min(pct_cap, cash_cap)
    price = _price(intent, ctx) or 0.0
    currency = intent.currency or map_contract(intent.symbol).currency
    fx = ctx.fx_to_base.get(currency, 1.0)
    lot = intent.lot_size or map_contract(intent.symbol).lot_size
    if lot * price * fx > cap + 1e-6:
        return "LOT_TOO_LARGE"
    if notional > ctx.nlv * float(ctx.limits["R1_order_size_pct_nlv"]) + 1e-6:
        return "R1"
    if notional > float(ctx.limits["R2_max_order_notional_base"]) + 1e-6:
        return "R2"
    held = sum(row.market_value_base for row in ctx.positions if row.symbol == intent.symbol)
    if held + notional > ctx.nlv * float(ctx.limits["R3_max_position_pct_nlv"]) + 1e-6:
        return "R3"
    market_cap = ctx.limits["R4_max_market_pct_nlv"][market]
    market_held = sum(
        row.market_value_base for row in ctx.positions if _symbol_market(row.symbol) == market
    )
    if market_held + notional > ctx.nlv * float(market_cap) + 1e-6:
        return "R4"
    ccy_cap = ctx.limits["R5_max_ccy_pct_nlv"].get(currency)
    if ccy_cap is not None:
        ccy_held = market_held
        if ccy_held + notional > ctx.nlv * float(ccy_cap) + 1e-6:
            return "R5"
    gross = sum(row.market_value_base for row in ctx.positions)
    if gross + notional > ctx.nlv * float(ctx.limits["R6_max_gross_exposure_pct_nlv"]) + 1e-6:
        return "R6"
    if ctx.limits["R7_require_cash_cover"] and notional > ctx.cash + 1e-6:
        return "R7"
    if ctx.fair_value is not None and price >= ctx.fair_value:
        return "ABOVE_FAIR_VALUE"
    if intent.ref_price_at_create and abs(price / intent.ref_price_at_create - 1) > 0.10:
        return "STALE_REF"
    return None


def _quote_rules(intent: OrderIntent, ctx: GateContext) -> str | None:
    quote = ctx.quotes.get(intent.symbol)
    price = _price(intent, ctx)
    if quote is None or price is None:
        return "R22"
    if quote.delayed:
        return "R22"
    age = (ctx.now - quote.as_of).total_seconds()
    if age > ctx.quote_stale_sec:
        return "R22"
    if intent.limit_price is not None and quote.last:
        drift = abs(intent.limit_price / quote.last - 1)
        if drift > 0.02 + 1e-9:
            return "R16"
    return None


def _last_same_side(ctx: GateContext, intent: OrderIntent) -> datetime | None:
    times = [
        row.opened_at
        for row in ctx.open_orders
        if (
            row.symbol == intent.symbol
            and row.side == intent.side.value
            and row.opened_at is not None
        )
    ]
    if not times:
        return None
    return max(times)


def _symbol_market(symbol: str) -> str:
    try:
        return map_contract(symbol).market
    except UnsupportedInstrument:
        return ""
