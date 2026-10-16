"""IBKR adapter for the async Broker protocol.

A real session uses ``ib_async`` against IB Gateway. That import happens
only inside :meth:`PaperGateway.connect`, so tests never load it and CI
does not open a socket. Ports are the paper pair only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_trading.broker import (
    Balance,
    BrokerPosition,
    Fill,
    Instrument,
    OrderAck,
    OrderIntent,
    OrderState,
)
from auto_trading.config import DEFAULT_TRADING_HOME, Settings
from auto_trading.execution.contracts import map_contract
from auto_trading.execution.pricing import quantize_to_tick
from auto_trading.execution.r0 import AccountGuard, latch_kill_switch
from auto_trading.execution.stops import cancel_keeps_protective
from auto_trading.risk.gate import MAX_QUOTE_DELAY_SEC, QUOTE_RECEIPT_MAX_SEC, RiskRejected

# Kept so a test can replace ``datetime.now`` without breaking ``isinstance``.
_WallClock = datetime
log = logging.getLogger(__name__)


def _field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "")
    return str(getattr(item, name, "") or "")


_NLV_TAGS = frozenset({"NetLiquidation"})
_LEDGER_NLV_TAGS = frozenset({"$LEDGER-NetLiquidationByCurrency", "NetLiquidationByCurrency"})


def _tagged_amount(
    rows: list[Any], tags: frozenset[str], currency: str
) -> tuple[bool, float | None]:
    """Whether ``tags`` had a ``currency`` line, and the parsed amount.

    A present line that is not a number fails closed: the flag is set and
    the amount is ``None``, so a later tag cannot stand in for it.
    """
    for item in rows:
        if _field(item, "tag") not in tags:
            continue
        if _field(item, "currency").strip().upper() != currency:
            continue
        try:
            return True, float(_field(item, "value"))
        except ValueError:
            return True, None
    return False, None


def _chosen_nlv(rows: list[Any]) -> tuple[float, str] | None:
    """NLV amount and the currency it is denominated in.

    Currency ``BASE`` means the alias was used and the account base currency
    was not identified, so the figure is already the system base.
    """
    from auto_trading.execution.fx import account_base_currency

    base = account_base_currency(rows)
    if base:
        found, amount = _tagged_amount(rows, _NLV_TAGS, base)
        if found:
            return None if amount is None else (amount, base)
    found, amount = _tagged_amount(rows, _NLV_TAGS, "BASE")
    if found:
        return None if amount is None else (amount, base or "BASE")
    found, amount = _tagged_amount(rows, _LEDGER_NLV_TAGS, "BASE")
    if found:
        return None if amount is None else (amount, base or "BASE")
    return None


def net_liquidation_base(rows: list[Any]) -> float | None:
    """Net liquidation in the system base currency (HKD unless configured).

    IB's account base currency, read from AccountValues, is authoritative.
    ``NetLiquidation`` in that currency is accepted. An explicit ``BASE``
    line and ``$LEDGER-NetLiquidationByCurrency`` in BASE are accepted too.
    A figure already in the system base is kept. Any other currency is
    converted with the existing FX helpers. Missing NLV, an unparseable
    line, or a currency with no rate stays ``None``.
    """
    from auto_trading.backtest.fx import to_base
    from auto_trading.config import get_settings
    from auto_trading.execution.fx import DEFAULT_RATES_HKD, rates_from_account_values

    chosen = _chosen_nlv(rows)
    if chosen is None:
        return None
    amount, currency = chosen
    system = get_settings().base_currency
    if currency in {system, "BASE"}:
        return amount
    rates = rates_from_account_values(rows)
    if currency not in rates:
        if currency not in DEFAULT_RATES_HKD:
            return None
        rates = dict(DEFAULT_RATES_HKD)
    try:
        return to_base(amount, currency, rates, system)
    except (KeyError, ValueError):
        return None


class IbkrBroker:
    """Paper Gateway adapter. ``place`` re-checks R0 immediately before submit."""

    venue = "ibkr"

    def __init__(
        self,
        client: Any | None = None,
        *,
        port: int = 4002,
        expected_account: str = "",
        trading_home: Path | None = None,
    ) -> None:
        self.client = client
        self.trading_home = _latch_home(trading_home)

        def _trip(code: str) -> None:
            latch_kill_switch(self.trading_home, code)
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                disconnect()

        self.guard = AccountGuard(port, expected_account, on_violation=_trip)

    async def _snapshot(self, account: str) -> None:
        if self.client is not None:
            self.guard.observe(await self.client.managed_accounts())
        self.guard.assert_order_account(account)

    async def balances(self) -> list[Balance]:
        self._require_client()
        return await self.client.balances()

    async def positions(self) -> list[BrokerPosition]:
        self._require_client()
        return await self.client.positions()

    async def instrument(self, symbol: str) -> Instrument:
        spec = map_contract(symbol)
        return Instrument(
            symbol=spec.symbol,
            market=spec.market,
            currency=spec.currency,
            lot_size=spec.lot_size,
        )

    async def place(self, intent: OrderIntent) -> OrderAck:
        """Third R0 check, immediately before the client submits."""
        await self._snapshot(intent.account)
        self._require_client()
        blocked = _quote_block(intent)
        if blocked:
            raise RiskRejected(blocked)
        spec = map_contract(intent.symbol)
        order = {
            "client_id": intent.client_id,
            "account": intent.account,
            "symbol": spec.ib_symbol,
            "exchange": spec.exchange,
            "currency": spec.currency,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "tif": "DAY",
            "order_type": "LMT",
            "quote_source": intent.quote_source,
            "quote_data_type": intent.quote_data_type,
            "quote_age_sec": intent.quote_age_sec,
            "quote_delay_sec": intent.quote_delay_sec,
            "market_data_type": intent.quote_market_data_type,
        }
        return await self.client.place_order(order)

    async def modify(self, client_id: str, limit_price: float, *, account: str = "") -> None:
        """Modifications use the same R0 check as a new order."""
        await self._snapshot(account)
        self._require_client()
        await self.client.modify_order(client_id, limit_price)

    async def cancel(
        self,
        client_id: str,
        *,
        protective: bool = False,
        full_stop: bool = False,
    ) -> None:
        """Cancels still pass after an R0 violation. A full stop keeps disaster stops."""
        if cancel_keeps_protective(protective=protective, full_stop=full_stop):
            return
        self._require_client()
        await self.client.cancel_order(client_id)

    async def order(self, client_id: str) -> OrderState:
        self._require_client()
        return await self.client.order(client_id)

    def fills(self) -> AsyncIterator[Fill]:
        self._require_client()

        async def _stream() -> AsyncIterator[Fill]:
            for fill in await self.client.executions():
                yield fill

        return _stream()

    def _require_client(self) -> None:
        if self.client is None:
            raise ConnectionError("paper gateway is not connected")


def _latch_home(trading_home: Path | None) -> Path:
    """Where an R0 violation writes the kill switch, even when no home was passed."""
    if trading_home is not None:
        return trading_home
    chosen = os.environ.get("TRADING_HOME", "").strip()
    if chosen:
        return Path(chosen)
    return DEFAULT_TRADING_HOME


def _quote_block(intent: OrderIntent) -> str | None:
    """Public prices and market-data type 2 never reach the broker."""
    if intent.protective:
        return None
    if intent.quote_source == "yahoo" or intent.quote_data_type == "public":
        return "R22"
    if intent.quote_market_data_type == 2:
        return "R22"
    if intent.quote_data_type == "delayed" and intent.quote_market_data_type not in (3, 4):
        return "R22"
    if intent.quote_age_sec is not None and intent.quote_age_sec > QUOTE_RECEIPT_MAX_SEC:
        return "R22"
    if intent.quote_data_type == "delayed" and (
        intent.quote_delay_sec is None or intent.quote_delay_sec > MAX_QUOTE_DELAY_SEC
    ):
        return "R22"
    if intent.quote_delay_sec is not None and intent.quote_delay_sec > MAX_QUOTE_DELAY_SEC:
        return "QUOTE_DELAY_EXCEEDED"
    return None


# How long a snapshot waits for a price and a timestamp tick. Not a fixed sleep.
_QUOTE_WAIT_SEC = 2.0


def _finite_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, _WallClock):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and value == value and value > 1_000_000_000:
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _tick_ready(ticker: Any) -> bool:
    """True once a price and a timestamp tick are both on the ticker."""
    price = _finite_price(getattr(ticker, "last", None))
    if price is None:
        price = _finite_price(getattr(ticker, "delayedLast", None))
    if price is None:
        return False
    return _tick_time(ticker, "delayedLastTimestamp", "lastTimestamp") is not None


def _tick_time(ticker: Any, *names: str) -> datetime | None:
    """Tick 45 is ``lastTimestamp``. Tick 88 is ``delayedLastTimestamp``."""
    for name in names:
        found = _as_utc(getattr(ticker, name, None))
        if found is not None:
            return found
    wanted = {"lastTimestamp": 45, "delayedLastTimestamp": 88}
    ids = {wanted[name] for name in names if name in wanted}
    for tick in getattr(ticker, "ticks", None) or []:
        if getattr(tick, "tickType", None) not in ids:
            continue
        found = _as_utc(getattr(tick, "time", None))
        if found is not None:
            return found
    return None


def _quote_row(symbol: str, ticker: Any, received: datetime) -> dict[str, Any] | None:
    """One quote row. A missing type or timestamp is omitted, never delay 0."""
    raw_type = getattr(ticker, "marketDataType", None)
    if raw_type is None:
        return None
    try:
        market_data_type = int(raw_type)
    except (TypeError, ValueError):
        return None
    last = _finite_price(getattr(ticker, "last", None))
    if last is None:
        last = _finite_price(getattr(ticker, "delayedLast", None))
    if last is None:
        return None
    if market_data_type in (3, 4):
        as_of = _tick_time(ticker, "delayedLastTimestamp", "lastTimestamp")
        kind = "delayed"
    elif market_data_type == 1:
        as_of = _tick_time(ticker, "lastTimestamp")
        kind = "realtime"
    else:
        return None
    if as_of is None:
        return None
    delay = (received - as_of).total_seconds()
    return {
        "symbol": symbol,
        "last": last,
        "as_of": as_of.isoformat(timespec="seconds"),
        "received_at": received.isoformat(timespec="seconds"),
        "delay_sec": delay,
        "source": "ibkr",
        "data_type": kind,
        "market_data_type": market_data_type,
    }


async def _wait_for_tick(ticker: Any) -> datetime | None:
    """Receipt time of the first complete tick, or None when the wait ends.

    The stamp is taken inside the tick callback, before any later sleep.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _QUOTE_WAIT_SEC
    receipt: datetime | None = None

    def _note(*_args: Any) -> None:
        nonlocal receipt
        if receipt is None and _tick_ready(ticker):
            receipt = datetime.now(UTC)

    event = getattr(ticker, "updateEvent", None)
    if event is not None and hasattr(event, "__iadd__"):
        event += _note
    try:
        _note()
        while receipt is None:
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.05)
            if receipt is None and _tick_ready(ticker):
                receipt = datetime.now(UTC)
        return receipt
    finally:
        if event is not None and hasattr(event, "__isub__"):
            with contextlib.suppress(Exception):
                event -= _note


class PaperGateway:
    """Real ``ib_async`` session. Not constructed by tests or CI.

    Connects to a Gateway that is already logged in. Login names are not
    read here; the IBC launcher is the only reader of those variables.
    """

    def __init__(self, settings: Settings, *, client_id: int) -> None:
        self.settings = settings
        self.client_id = client_id
        self._ib: Any = None

    async def connect(self) -> list[str]:
        from ib_async import IB

        ib = IB()
        await ib.connectAsync(
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            clientId=self.client_id,
        )
        self._ib = ib
        accounts = list(ib.managedAccounts())

        def _trip(code: str) -> None:
            latch_kill_switch(self.settings.trading_home, code)
            ib.disconnect()

        AccountGuard(
            self.settings.ibkr_port,
            self.settings.ibkr_paper_account,
            on_violation=_trip,
        ).observe(accounts)
        return accounts

    def disconnect(self) -> None:
        """Drop the Gateway socket. Safe when connect never finished."""
        ib = self._ib
        self._ib = None
        if ib is None:
            return
        with contextlib.suppress(Exception):
            ib.disconnect()

    async def snapshot_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Delayed paper quotes from the Gateway. An empty list is a failure."""
        from ib_async import Stock

        await self.connect()
        ib = self._ib
        try:
            ib.reqMarketDataType(3)
            found: list[dict[str, Any]] = []
            for symbol in symbols:
                spec = map_contract(symbol)
                contract = Stock(spec.ib_symbol, spec.exchange, spec.currency)
                ticker = ib.reqMktData(contract, "", False, False)
                try:
                    received = await _wait_for_tick(ticker)
                    row = _quote_row(symbol, ticker, received) if received is not None else None
                    if row is not None:
                        found.append(row)
                finally:
                    with contextlib.suppress(Exception):
                        ib.cancelMktData(contract)
            return found
        finally:
            self.disconnect()

    async def snapshot_account(self) -> dict[str, Any]:
        """Connect, check R0, and return NLV, positions, and open orders."""
        accounts = await self.connect()
        ib = self._ib
        try:
            values = list(ib.accountValues())
            nlv = net_liquidation_base(values)
            from auto_trading.execution.fx import rates_from_account_values, total_cash_base

            positions: list[dict[str, Any]] = []
            for pos in ib.positions():
                contract = pos.contract
                positions.append(
                    {
                        "symbol": str(contract.symbol),
                        "qty": float(pos.position),
                        "avg": float(pos.avgCost),
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "exchange": str(
                            getattr(contract, "primaryExchange", "")
                            or getattr(contract, "exchange", "")
                            or ""
                        ),
                    }
                )
            open_orders: list[dict[str, Any]] = []
            for trade in ib.openTrades():
                order = trade.order
                contract = trade.contract
                ref = str(getattr(order, "orderRef", "") or getattr(order, "orderId", ""))
                status = getattr(trade, "orderStatus", None)
                total = float(getattr(order, "totalQuantity", 0) or 0)
                filled = float(getattr(status, "filled", 0) or 0)
                remaining = max(total - filled, 0.0)
                open_orders.append(
                    {
                        "order_id": ref,
                        "symbol": str(contract.symbol),
                        "side": str(getattr(order, "action", "") or ""),
                        "qty": remaining,
                        "currency": str(getattr(contract, "currency", "") or ""),
                        "exchange": str(
                            getattr(contract, "primaryExchange", "")
                            or getattr(contract, "exchange", "")
                            or ""
                        ),
                    }
                )
            from auto_trading.execution.fx_convert import cash_by_currency

            snapshot = {
                "accounts": accounts,
                "nlv": nlv,
                "cash": total_cash_base(values),
                "fx_to_base": rates_from_account_values(values),
                "positions": positions,
                "open_orders": open_orders,
            }
            balances = cash_by_currency(values)
            if balances is not None:
                snapshot["cash_by_ccy"] = balances
            return snapshot
        finally:
            self.disconnect()

    async def convert_cash(self, plan: Any, *, account: str) -> dict[str, float] | None:
        """Sell USD for ``plan.target_ccy`` and wait until that cash covers the buy.

        An IdealPro pair is a market FX order. Anything else, including KRW
        and TWD, is an IBKR cash conversion. A rejected or unconfirmed fill
        returns None so the equity order is not sent. A working order with
        the same ref is not placed again.
        """
        from auto_trading.execution.fx_convert import (
            await_target_cash,
            cash_by_currency,
            fx_rejected,
            fx_still_working,
            ib_order_spec,
        )
        from auto_trading.paper_only import port_accepted

        if not port_accepted(self.settings.ibkr_port):
            return None
        await self.connect()
        ib = self._ib
        try:
            spec = ib_order_spec(plan, account)
            values = list(ib.accountValues())
            current = cash_by_currency(values) or {}
            try:
                have = float(current.get(plan.target_ccy, 0.0) or 0.0)
            except (TypeError, ValueError):
                have = 0.0
            if have + 1e-6 >= float(plan.required_target):
                return {str(key).upper(): float(amount) for key, amount in current.items()}
            trades = list(ib.trades()) if callable(getattr(ib, "trades", None)) else []
            if not fx_still_working(trades or list(ib.openTrades()), spec["orderRef"]):
                self._place_fx(ib, spec)

            def read() -> dict[str, float] | None:
                return cash_by_currency(list(ib.accountValues()))

            def rejected() -> bool:
                book = list(ib.trades()) if callable(getattr(ib, "trades", None)) else []
                if not book:
                    book = list(ib.openTrades())
                return fx_rejected(book, spec["orderRef"])

            return await_target_cash(
                read,
                target=plan.target_ccy,
                required=float(plan.required_target),
                sleep=ib.sleep,
                rejected=rejected,
            )
        except Exception as exc:
            log.warning("fx conversion failed: %s", type(exc).__name__)
            return None
        finally:
            self.disconnect()

    def _place_fx(self, ib: Any, spec: dict[str, Any]) -> None:
        from ib_async import Contract, Order

        contract = Contract()
        contract.secType = spec["secType"]
        contract.symbol = spec["symbol"]
        contract.currency = spec["currency"]
        contract.exchange = spec["exchange"]
        order = Order()
        order.action = spec["action"]
        order.orderType = spec["orderType"]
        order.totalQuantity = spec["totalQuantity"]
        order.account = spec["account"]
        order.orderRef = spec["orderRef"]
        order.tif = spec["tif"]
        ib.placeOrder(contract, order)

    async def submit_order(self, intent: Any) -> Any:
        """Place one order through the connected Gateway. E2 only."""
        await self.connect()
        try:
            return await self._submit_connected(intent)
        finally:
            self.disconnect()

    async def _submit_connected(self, intent: Any) -> Any:
        class _Client:
            def __init__(self, ib: Any, accounts: list[str]) -> None:
                self._ib = ib
                self._accounts = accounts

            async def managed_accounts(self) -> list[str]:
                return list(self._accounts)

            async def place_order(self, order: dict[str, Any]) -> Any:
                from ib_async import LimitOrder, Stock

                contract = Stock(order["symbol"], order["exchange"], order["currency"])
                order["market"] = map_contract(intent.symbol).market
                side = "BUY" if order["side"] == "BUY" else "SELL"
                limit = quantize_to_tick(
                    order["market"], float(order["limit_price"]), buy=side == "BUY"
                )
                ticket = LimitOrder(side, order["quantity"], limit)
                ticket.account = order["account"]
                ticket.orderRef = order["client_id"]
                ticket.tif = "DAY"
                self._ib.placeOrder(contract, ticket)
                from auto_trading.broker import OrderAck

                return OrderAck(client_id=order["client_id"], broker_order_id=order["client_id"])

        accounts = list(self._ib.managedAccounts())
        broker = IbkrBroker(
            _Client(self._ib, accounts),
            port=self.settings.ibkr_port,
            expected_account=self.settings.ibkr_paper_account,
            trading_home=self.settings.trading_home,
        )
        return await broker.place(intent)
