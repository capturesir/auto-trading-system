"""Risk gate. ``allow`` is the only check that may release an order.

R0 runs first and is never waived. Caps come from the same limits object
that ``state/risk_limits.json`` publishes. Exits skip the opening-only
rules. R23 records a streak and does not change the mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from auto_trading.broker import OrderIntent
from auto_trading.execution.contracts import UnsupportedInstrument, map_contract
from auto_trading.execution.oms import order_ref
from auto_trading.execution.r0 import DU_ACCOUNT, PAPER_PORTS, paper_account_pin
from auto_trading.execution.risk_state import RiskConfigMismatch, verify_risk_limits_file
from auto_trading.execution.sessions import BLOCK_ALL, session_window
from auto_trading.risk.adr_premium import check_buy
from auto_trading.risk.limits import EXPECTED_LIMITS

log = logging.getLogger(__name__)

OPENING = frozenset({"ENTRY", "ADD"})
REDUCING = frozenset({"REDUCE", "EXIT"})
MARKETS = frozenset({"US", "HK", "JP", "KR", "TW"})
MAX_QUOTE_DELAY_SEC = 1800


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
    status: str = ""
    quantity: float = 0.0
    strategy_id: str | None = None
    notional_base: float = 0.0
    market: str = ""
    currency: str = ""


@dataclass
class Held:
    symbol: str
    quantity: float
    market_value_base: float
    strategy_id: str | None = None
    currency: str = ""
    market_value_local: float | None = None


@dataclass
class Quote:
    """One price. ``source`` is ibkr or yahoo. ``data_type`` is realtime, delayed, or public.

    ``as_of`` is the market timestamp. ``received_at`` is when this process
    got the quote. Delayed type 3 prints lag by about 15 minutes, so their
    freshness for R22 is receipt time. ``delay_sec`` keeps that lag.
    """

    last: float
    as_of: datetime
    bid: float | None = None
    ask: float | None = None
    open_price: float | None = None
    delayed: bool = False
    source: str = "ibkr"
    data_type: str = "realtime"
    received_at: datetime | None = None
    delay_sec: float | None = None
    market_data_type: int | None = None

    def __post_init__(self) -> None:
        if self.market_data_type == 2:
            self.data_type = "rejected"
        if self.delayed and self.data_type == "realtime":
            self.data_type = "delayed"
        if self.data_type == "delayed":
            self.delayed = True
        if self.received_at is not None and self.delay_sec is None:
            self.delay_sec = (self.received_at - self.as_of).total_seconds()

    def age_seconds(self, now: datetime) -> float:
        """Seconds since this process received the quote.

        Freshness never uses the exchange timestamp on ``as_of``. A missing
        receipt time is stale.
        """
        if self.received_at is None:
            return float("inf")
        return (now - self.received_at).total_seconds()


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
    loss_streaks: dict[str, int] = field(default_factory=dict)
    approved_dir: Path | None = None
    stage: str = "pre"
    approval_price: float | None = None
    reapproval_deviation: float = 0.015
    quote_stale_sec: int = 10
    adr_state_missing: bool = False
    adr_paused: frozenset[str] = frozenset()
    adr_controlled: frozenset[str] = frozenset({"TSM"})
    adr_map_symbols: tuple[str, ...] | None = ("TSM",)
    adr_last_db: frozenset[str] = frozenset()
    adr_rule: dict[str, Any] | None = None
    adr_expected_rule: dict[str, Any] | None = None
    adr_map_path: Path | None = None
    adr_state_path: Path | None = None
    adr_expected_path: Path | None = None
    r23_note: str | None = None
    trading_home: Path | None = None
    unknown_symbols: frozenset[str] = frozenset()
    order_refs: frozenset[str] = frozenset()
    order_seq: int = 1
    anomaly_breaker: bool = False
    notify_failure: bool = False


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
    reducing = intent.intent_type in REDUCING or intent.reduce_only or intent.protective
    file_code = _risk_file(ctx)
    if file_code and not reducing:
        return file_code
    if _killed(ctx):
        return "R18"
    if not ctx.connected or not ctx.recon_ok:
        return "R17"
    if ctx.paused and intent.intent_type in OPENING:
        return "R18"
    if intent.mode != "paper" or intent.live_size_multiplier is not None:
        return "LIVE_NOT_AUTHORIZED"
    if intent.spec_hash is not None and ctx.approved_hash != intent.spec_hash:
        return "R25"
    file_r25 = _r25_file(intent, ctx)
    if file_r25:
        return file_r25
    if ctx.registry_mode != "paper":
        return "LIVE_NOT_AUTHORIZED"
    market = _market(intent)
    if market is None:
        return "R26"
    if intent.symbol in ctx.unknown_symbols or _unknown_order(ctx, intent.symbol):
        return "UNKNOWN_ORDER"
    if intent.intent_id and order_ref(intent.intent_id, ctx.order_seq) in ctx.order_refs:
        return "R12"
    window = session_window(market, ctx.now)
    if window in BLOCK_ALL:
        return "R10"
    held_qty = _position_qty(ctx, intent)
    orphan_exit = intent.intent_type in REDUCING and not intent.parent_intent_id
    if orphan_exit and not held_qty:
        return "R27"
    if intent.side.value == "SELL" and _r20(intent, ctx, held_qty):
        return "R20"
    key = intent.intent_id or intent.client_id
    active = [row for row in ctx.open_orders if row.active and row.intent_id == key]
    if active:
        return "R11"
    reducing = intent.intent_type in REDUCING or intent.reduce_only or intent.protective
    if intent.intent_type != "CANCEL" and not reducing:
        paused_entry = _entry_pause(ctx)
        if paused_entry:
            return paused_entry
    if not reducing and window in {"open_buffer", "close_buffer"}:
        return "R10"
    if not reducing:
        opening = _opening_rules(intent, ctx, market)
        if opening:
            return opening
        paused = _r28(intent, ctx)
        if paused:
            return paused
    quote_code = _quote_rules(intent, ctx)
    if quote_code:
        return quote_code
    if ctx.stage == "final" and ctx.approval_price and _price(intent, ctx):
        price = _price(intent, ctx)
        if price and abs(price / ctx.approval_price - 1) > ctx.reapproval_deviation:
            return "REAPPROVAL_REQUIRED"
    streak = _loss_streak(intent, ctx)
    if streak >= int(ctx.limits["R23_consecutive_loss_demote"]):
        ctx.r23_note = "r23_demote_count"
    return None


def _r0(intent: OrderIntent, ctx: GateContext) -> str | None:
    if ctx.port not in PAPER_PORTS:
        return "R0_FORBIDDEN_PORT"
    if not ctx.accounts or any(DU_ACCOUNT.fullmatch(account) is None for account in ctx.accounts):
        return "R0_NOT_PAPER_ACCOUNT"
    pin = paper_account_pin(ctx.expected_account)
    if not pin or DU_ACCOUNT.fullmatch(pin) is None:
        return "R0_NOT_PAPER_ACCOUNT"
    if pin and frozenset(ctx.accounts) != {pin}:
        return "R0_ACCOUNTS_CHANGED"
    baseline = ctx.baseline_accounts
    if baseline is not None and frozenset(ctx.accounts) != frozenset(baseline):
        return "R0_ACCOUNTS_CHANGED"
    if intent.account and DU_ACCOUNT.fullmatch(intent.account) is None:
        return "R0_NOT_PAPER_ACCOUNT"
    if pin and intent.account and intent.account != pin:
        return "R0_ACCOUNTS_CHANGED"
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


def _strategy_qty(ctx: GateContext, intent: OrderIntent) -> float:
    if not intent.strategy_id:
        return _position_qty(ctx, intent)
    return sum(
        row.quantity
        for row in ctx.positions
        if row.symbol == intent.symbol and row.strategy_id == intent.strategy_id
    )


def _r20(intent: OrderIntent, ctx: GateContext, account_qty: float) -> bool:
    """True when this sell would exceed the strategy or the account."""
    if intent.quantity > _strategy_qty(ctx, intent) + 1e-9:
        return True
    pending = sum(
        row.quantity
        for row in ctx.open_orders
        if row.active
        and row.symbol == intent.symbol
        and row.side == "SELL"
        and row.intent_id != (intent.intent_id or intent.client_id)
    )
    return pending + intent.quantity > account_qty + 1e-9


def _r25_file(intent: OrderIntent, ctx: GateContext) -> str | None:
    if intent.spec_hash is None or intent.strategy_version is None:
        return None
    from auto_trading.execution.strategy_book import r25_code

    return r25_code(
        strategy_id=intent.strategy_id,
        version=intent.strategy_version,
        spec_hash_value=intent.spec_hash,
        root=ctx.approved_dir,
    )


def _loss_streak(intent: OrderIntent, ctx: GateContext) -> int:
    if intent.strategy_id and ctx.loss_streaks:
        return ctx.loss_streaks.get(intent.strategy_id, 0)
    return ctx.loss_streak


def _pending_open(
    ctx: GateContext,
    *,
    symbol: str | None = None,
    market: str | None = None,
    currency: str | None = None,
) -> float:
    total = 0.0
    for row in ctx.open_orders:
        if not row.active or row.side != "BUY" or row.protective:
            continue
        if symbol is not None and row.symbol != symbol:
            continue
        if market is not None and row.market != market:
            continue
        if currency is not None and row.currency != currency:
            continue
        total += row.notional_base
    return total


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
        return "LOW_CONFIDENCE"
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
    fx = ctx.fx_to_base
    held = sum(_base_value(row, fx) for row in ctx.positions if row.symbol == intent.symbol)
    pending_symbol = _pending_open(ctx, symbol=intent.symbol)
    position_cap = ctx.nlv * float(ctx.limits["R3_max_position_pct_nlv"])
    if held + pending_symbol + notional > position_cap + 1e-6:
        return "R3"
    market_cap = ctx.limits["R4_max_market_pct_nlv"][market]
    market_held = _market_exposure(ctx, market) + _pending_open(ctx, market=market)
    if market_held + notional > ctx.nlv * float(market_cap) + 1e-6:
        return "R4"
    ccy_cap = ctx.limits["R5_max_ccy_pct_nlv"].get(currency)
    if ccy_cap is not None:
        ccy_held = _currency_exposure(ctx, currency) + _pending_open(ctx, currency=currency)
        if ccy_held + notional > ctx.nlv * float(ccy_cap) + 1e-6:
            return "R5"
    gross = sum(_base_value(row, fx) for row in ctx.positions) + _pending_open(ctx)
    if gross + notional > ctx.nlv * float(ctx.limits["R6_max_gross_exposure_pct_nlv"]) + 1e-6:
        return "R6"
    if ctx.limits["R7_require_cash_cover"] and notional > ctx.cash + 1e-6:
        return "R7"
    if ctx.fair_value is not None and price >= ctx.fair_value:
        return "ABOVE_FAIR_VALUE"
    if intent.ref_price_at_create and abs(price / intent.ref_price_at_create - 1) > 0.10:
        return "STALE_REF"
    return None


def _entry_pause(ctx: GateContext) -> str | None:
    """R29 and R30 block new entries on pre and on final. Exits are exempt."""
    r29 = ctx.anomaly_breaker
    r30 = ctx.notify_failure
    if ctx.trading_home is not None:
        from auto_trading.execution.breakers import flags

        state = flags(ctx.trading_home)
        r29 = r29 or bool(state.get("r29_active"))
        r30 = r30 or bool(state.get("r30_active"))
    if r29:
        return "ANOMALY_BREAKER_ACTIVE"
    if r30:
        return "NOTIFY_FAILURE_PAUSE"
    return None


def _killed(ctx: GateContext) -> bool:
    """R18 reads the kill-switch file, not only a flag the caller remembered."""
    if ctx.kill_switch:
        return True
    if ctx.trading_home is None:
        return False
    return (ctx.trading_home / "KILL_SWITCH").is_file()


def _r28(intent: OrderIntent, ctx: GateContext) -> str | None:
    """Paper BUY entries only. The reference guard decides; exceptions pause."""
    if intent.mode != "paper" or intent.protective or intent.reduce_only:
        return None
    if intent.side.value != "BUY" or intent.intent_type not in {"ENTRY", "ADD"}:
        return None
    root = Path(__file__).resolve().parents[3]
    map_path = ctx.adr_map_path or (root / "config" / "adr_map.csv")
    expected_path = ctx.adr_expected_path or (root / "config" / "adr_premium_rule_expected.json")
    if ctx.adr_state_path is not None:
        state_path = ctx.adr_state_path
    elif ctx.trading_home is not None:
        state_path = ctx.trading_home / "state" / "adr_premium_state.json"
    else:
        state_path = Path("/nonexistent-adr-premium-state.json")
    try:
        allowed, reason, detail, _session, cause = check_buy(
            intent.symbol,
            intent.side.value,
            ctx.now.isoformat(),
            str(state_path),
            str(map_path),
            str(expected_path),
        )
    except Exception:
        return "ADR_PREMIUM_PAUSED"
    if allowed:
        return None
    log.info("r28 detail=%s cause=%s", detail, cause)
    return reason or "ADR_PREMIUM_PAUSED"


def _quote_rules(intent: OrderIntent, ctx: GateContext) -> str | None:
    if intent.protective:
        return None
    quote = ctx.quotes.get(intent.symbol)
    price = _price(intent, ctx)
    if quote is None or price is None:
        return "R22"
    log.info(
        "quote_source=%s market_data_type=%s quote_data_type=%s delay_sec=%s",
        quote.source,
        quote.market_data_type,
        quote.data_type,
        quote.delay_sec,
    )
    if quote.source == "yahoo" or quote.data_type == "public":
        return "R22"
    if quote.market_data_type == 2 or quote.data_type == "rejected":
        return "R22"
    paper = ctx.registry_mode == "paper" and intent.mode == "paper"
    if quote.data_type == "delayed":
        if not paper or quote.market_data_type not in (3, 4):
            return "R22"
    elif quote.data_type != "realtime":
        return "R22"
    if quote.age_seconds(ctx.now) > ctx.quote_stale_sec:
        return "R22"
    delay = quote.delay_sec
    if delay is not None and delay > MAX_QUOTE_DELAY_SEC:
        return "QUOTE_DELAY_EXCEEDED"
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


def _risk_file(ctx: GateContext) -> str | None:
    """Re-read the published limits. Any mismatch refuses the order."""
    if ctx.trading_home is None:
        return "RISK_CONFIG_MISMATCH"
    try:
        ctx.limits = verify_risk_limits_file(ctx.trading_home)
    except RiskConfigMismatch:
        return "RISK_CONFIG_MISMATCH"
    return None


def _unknown_order(ctx: GateContext, symbol: str) -> bool:
    return any(row.symbol == symbol and row.status == "UNKNOWN" for row in ctx.open_orders)


def _held_currency(row: Held) -> str:
    if row.currency:
        return row.currency
    try:
        return map_contract(row.symbol).currency
    except UnsupportedInstrument:
        return ""


def _base_value(row: Held, fx: dict[str, float]) -> float:
    """Position value in HKD. Local currency is converted with ``fx``."""
    if row.market_value_local is not None:
        return row.market_value_local * fx.get(_held_currency(row), 1.0)
    return row.market_value_base


def _market_exposure(ctx: GateContext, market: str) -> float:
    return sum(
        _base_value(row, ctx.fx_to_base)
        for row in ctx.positions
        if _symbol_market(row.symbol) == market
    )


def _currency_exposure(ctx: GateContext, currency: str) -> float:
    return sum(
        _base_value(row, ctx.fx_to_base) for row in ctx.positions if _held_currency(row) == currency
    )
