"""Paper-only cash conversion before a foreign-currency buy.

DUR243736 keeps its spendable cash in USD. A buy that settles in another
currency (KRW, TWD, HKD, JPY, and the rest) must already have that cash,
including the equity commission and a fee buffer. Otherwise the stock order
would open a debit in the target currency.

The check is off until ``TRADING_FX_PRECONVERT=1``. JPY and HKD go out as
IdealPro FX. KRW and TWD go out as an IBKR cash conversion (``FXCONV``).
Nothing here opens a session; the gateway does that on the paper ports only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from auto_trading.backtest.costs import market_cost
from auto_trading.broker import OrderIntent
from auto_trading.execution.contracts import UnsupportedInstrument, map_contract

FUNDING_CCY = "USD"
# 100 bps of price/FX slippage plus 20 bps of estimated FX commission.
FX_BUFFER_BPS = 100
FX_COMMISSION_BPS = 20
MIN_FX_FEE_USD = 2.0
FX_CONFIRM_SEC = 30.0
FX_POLL_SEC = 0.5
ENV_FLAG = "TRADING_FX_PRECONVERT"

# IdealPro quotes a spot book. KRW and TWD do not, so those use FXCONV.
IDEALPRO_CCY = frozenset(
    {
        "HKD",
        "JPY",
        "EUR",
        "GBP",
        "AUD",
        "CAD",
        "CHF",
        "CNH",
        "SGD",
        "NZD",
        "MXN",
        "SEK",
        "NOK",
        "DKK",
        "CZK",
        "HUF",
        "PLN",
        "ILS",
        "ZAR",
        "TRY",
    }
)
_CASH_TAGS = frozenset({"CashBalance"})
_CASH_FALLBACK_TAGS = frozenset({"TotalCashValue"})
_SKIP_CCY = frozenset({"", "BASE"})


def fx_preconvert_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the paper submit path is asked to convert first."""
    source = os.environ if env is None else env
    return str(source.get(ENV_FLAG, "")).strip() == "1"


def _field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "")
    return str(getattr(item, name, "") or "")


def _code(value: str) -> str | None:
    text = value.strip().upper()
    if len(text) != 3 or not text.isalpha() or text in _SKIP_CCY:
        return None
    return text


def cash_by_currency(rows: list[Any]) -> dict[str, float] | None:
    """Settled cash by ISO currency.

    ``CashBalance`` wins when any real currency is present. Otherwise
    ``TotalCashValue`` is used. ``BASE`` is not a currency. No such row
    means the balance is unknown (``None``), which is not the same as zero.
    """
    balances = _tagged_cash(rows, _CASH_TAGS)
    if balances is None:
        balances = _tagged_cash(rows, _CASH_FALLBACK_TAGS)
    return balances


def _tagged_cash(rows: list[Any], tags: frozenset[str]) -> dict[str, float] | None:
    found: dict[str, float] = {}
    seen = False
    for item in rows:
        if _field(item, "tag") not in tags:
            continue
        code = _code(_field(item, "currency"))
        if code is None or code in found:
            continue
        seen = True
        try:
            found[code] = float(_field(item, "value"))
        except ValueError:
            continue
    if not seen:
        return None
    return found


def parse_cash_map(raw: Any) -> dict[str, float] | None:
    """Normalize a stored ``cash_by_ccy`` object. A missing object stays unknown."""
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        code = _code(str(key))
        if code is None:
            continue
        try:
            parsed[code] = float(value)
        except (TypeError, ValueError):
            continue
    return parsed


def trade_currency(intent: OrderIntent) -> str:
    """Settlement currency of the equity order."""
    raw = (intent.currency or "").strip().upper()
    if raw:
        return raw
    try:
        return map_contract(intent.symbol).currency.upper()
    except UnsupportedInstrument:
        return ""


def opening_foreign_buy(intent: OrderIntent) -> bool:
    """Paper BUY entry/add whose settlement currency is not USD."""
    if intent.mode != "paper" or intent.live_size_multiplier is not None:
        return False
    if intent.protective or intent.reduce_only:
        return False
    if intent.side.value != "BUY" or intent.intent_type not in {"ENTRY", "ADD"}:
        return False
    currency = trade_currency(intent)
    return bool(currency) and currency != FUNDING_CCY


def _session_day(intent: OrderIntent) -> date:
    if intent.valid_from is not None:
        return intent.valid_from.date()
    return date(2026, 1, 2)


def _market_of(intent: OrderIntent) -> str | None:
    if intent.market:
        return intent.market
    try:
        return map_contract(intent.symbol).market
    except UnsupportedInstrument:
        return None


def required_target_cash(intent: OrderIntent) -> float | None:
    """Local cash the buy needs: notional, equity fee, and the FX buffer.

    The buffer is ``FX_BUFFER_BPS + FX_COMMISSION_BPS`` of notional so a
    fill at the limit plus the conversion haircut does not debit the currency.
    """
    price = intent.limit_price
    if price is None or price <= 0 or intent.quantity <= 0:
        return None
    notional = float(intent.quantity) * float(price)
    market = _market_of(intent)
    if market is None:
        return None
    try:
        cost = market_cost(
            market,
            float(intent.quantity),
            notional,
            _session_day(intent),
            sell=False,
        )
    except ValueError:
        return None
    buffer = notional * (FX_BUFFER_BPS + FX_COMMISSION_BPS) / 10_000
    return notional + cost.commission + cost.tax + buffer


def _ceil_cent(amount: float) -> float:
    return math.ceil(amount * 100 - 1e-9) / 100.0


def usd_for_shortfall(
    shortfall_target: float, target: str, fx_to_base: Mapping[str, float]
) -> float | None:
    """USD to sell so the shortfall arrives after the minimum FX fee.

    ``fx_to_base`` is HKD per one unit, the same map the gate already stores.
    """
    if shortfall_target <= 0:
        return 0.0
    per_target = fx_to_base.get(target)
    per_usd = fx_to_base.get(FUNDING_CCY)
    if per_target is None or per_usd is None:
        return None
    try:
        target_rate = float(per_target)
        usd_rate = float(per_usd)
    except (TypeError, ValueError):
        return None
    if target_rate <= 0 or usd_rate <= 0:
        return None
    raw = shortfall_target * target_rate / usd_rate + MIN_FX_FEE_USD
    return _ceil_cent(raw)


def _pending_local(
    currency: str,
    open_orders: Sequence[Any],
    fx_to_base: Mapping[str, float],
) -> float:
    """HKD notional of working buys in ``currency``, restated in that currency."""
    raw = fx_to_base.get(currency)
    try:
        rate = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0:
        return 0.0
    total = 0.0
    for row in open_orders:
        if not getattr(row, "active", False) or getattr(row, "protective", False):
            continue
        if str(getattr(row, "side", "") or "") != "BUY":
            continue
        if str(getattr(row, "currency", "") or "").upper() != currency:
            continue
        try:
            notional = float(getattr(row, "notional_base", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if notional > 0:
            total += notional / rate
    return total


def available_cash(
    currency: str,
    cash_by_ccy: Mapping[str, float],
    open_orders: Sequence[Any],
    fx_to_base: Mapping[str, float],
) -> float:
    """Snapshot cash minus working buys already reserving that currency."""
    try:
        held = float(cash_by_ccy.get(currency, 0.0) or 0.0)
    except (TypeError, ValueError):
        held = 0.0
    return held - _pending_local(currency, open_orders, fx_to_base)


@dataclass(frozen=True)
class FxOrder:
    """One paper conversion. Quantity is USD sold to acquire ``target_ccy``."""

    target_ccy: str
    usd_qty: float
    required_target: float
    order_type: str
    exchange: str
    action: str
    symbol: str
    sec_type: str
    tif: str
    order_ref: str
    venue: str


@dataclass(frozen=True)
class FxAttempt:
    """What the paper path should do before the equity order.

    ``convert`` places ``order`` and waits for cash. ``block`` places nothing.
    ``covered`` and ``skip`` leave the equity decision to the rest of the gate.
    """

    action: str
    reason: str = ""
    order: FxOrder | None = None
    required_target: float | None = None
    have_target: float | None = None
    usd_qty: float | None = None


def fx_order_ref(intent: OrderIntent) -> str:
    key = intent.intent_id or intent.client_id
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"fx-{digest}"


def venue_for(currency: str) -> tuple[str, str]:
    """Return ``(venue, order_type)``. IdealPro uses a market FX order."""
    if currency in IDEALPRO_CCY:
        return "IDEALPRO", "MKT"
    return "FXCONV", "FXCONV"


def ib_order_spec(order: FxOrder, account: str) -> dict[str, Any]:
    """Fields the paper gateway copies onto an FX contract and order."""
    return {
        "secType": order.sec_type,
        "symbol": order.symbol,
        "currency": order.target_ccy,
        "exchange": order.exchange,
        "action": order.action,
        "orderType": order.order_type,
        "totalQuantity": order.usd_qty,
        "tif": order.tif,
        "account": account,
        "orderRef": order.order_ref,
        "venue": order.venue,
    }


def _build_order(intent: OrderIntent, target: str, usd_qty: float, required: float) -> FxOrder:
    venue, order_type = venue_for(target)
    return FxOrder(
        target_ccy=target,
        usd_qty=usd_qty,
        required_target=required,
        order_type=order_type,
        exchange="IDEALPRO",
        action="SELL",
        symbol=FUNDING_CCY,
        sec_type="CASH",
        tif="DAY",
        order_ref=fx_order_ref(intent),
        venue=venue,
    )


def assess(
    intent: OrderIntent,
    *,
    cash_by_ccy: Mapping[str, float] | None,
    open_orders: Sequence[Any] = (),
    fx_to_base: Mapping[str, float] | None = None,
) -> FxAttempt:
    """Decide whether a paper buy still needs USD converted into the target currency."""
    if intent.mode != "paper" or intent.live_size_multiplier is not None:
        return FxAttempt("block", "not_paper")
    if not opening_foreign_buy(intent):
        return FxAttempt("skip")
    if cash_by_ccy is None:
        return FxAttempt("block", "unknown_balance")
    required = required_target_cash(intent)
    if required is None:
        return FxAttempt("block", "unpriced")
    rates: Mapping[str, float] = fx_to_base or {}
    target = trade_currency(intent)
    have = available_cash(target, cash_by_ccy, open_orders, rates)
    if have + 1e-6 >= required:
        return FxAttempt("covered", required_target=required, have_target=have)
    shortfall = required - have
    usd = usd_for_shortfall(shortfall, target, rates)
    if usd is None:
        return FxAttempt(
            "block",
            "no_fx_rate",
            required_target=required,
            have_target=have,
        )
    usd_have = available_cash(FUNDING_CCY, cash_by_ccy, open_orders, rates)
    if usd_have + 1e-6 < usd:
        return FxAttempt(
            "block",
            "usd_short",
            required_target=required,
            have_target=have,
            usd_qty=usd,
        )
    return FxAttempt(
        "convert",
        order=_build_order(intent, target, usd, required),
        required_target=required,
        have_target=have,
        usd_qty=usd,
    )


def reserve_target(cash_by_ccy: Mapping[str, float], intent: OrderIntent) -> dict[str, float]:
    """Subtract this buy's target-currency need so the next order sees it."""
    book = {str(key).upper(): float(value) for key, value in cash_by_ccy.items()}
    if not opening_foreign_buy(intent):
        return book
    required = required_target_cash(intent)
    if required is None:
        return book
    currency = trade_currency(intent)
    book[currency] = float(book.get(currency, 0.0)) - float(required)
    return book


def record_fx_attempt(
    home: Path,
    *,
    intent_id: str,
    symbol: str,
    status: str,
    when: str,
    target_ccy: str = "",
    usd_qty: float | None = None,
    required_target: float | None = None,
    detail: str = "",
) -> None:
    """Append one conversion attempt. The equity order is a separate record."""
    path = home / "state" / "fx_conversions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": when,
        "intent_id": intent_id,
        "symbol": symbol,
        "status": status,
        "target_ccy": target_ccy,
        "usd_qty": usd_qty,
        "required_target": required_target,
        "detail": detail,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def await_target_cash(
    read_cash: Callable[[], Mapping[str, float] | None],
    *,
    target: str,
    required: float,
    timeout_sec: float = FX_CONFIRM_SEC,
    poll_sec: float = FX_POLL_SEC,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rejected: Callable[[], bool] | None = None,
) -> dict[str, float] | None:
    """Poll until ``target`` cash covers ``required``, or the FX order is rejected.

    A timeout returns ``None``. The caller must not send the equity order.
    """
    deadline = monotonic() + timeout_sec
    while True:
        if rejected is not None and rejected():
            return None
        snapshot = read_cash()
        if isinstance(snapshot, Mapping):
            try:
                have = float(snapshot.get(target, 0.0) or 0.0)
            except (TypeError, ValueError):
                have = 0.0
            if have + 1e-6 >= required:
                parsed: dict[str, float] = {}
                for key, value in snapshot.items():
                    try:
                        parsed[str(key).upper()] = float(value)
                    except (TypeError, ValueError):
                        continue
                return parsed
        if monotonic() >= deadline:
            return None
        sleep(poll_sec)


_FX_REJECTED = frozenset({"cancelled", "inactive", "apicancelled"})


def _fx_name(trade: Any) -> tuple[str, str]:
    order = getattr(trade, "order", None)
    ref = str(getattr(order, "orderRef", "") or "")
    status = getattr(trade, "orderStatus", None)
    name = str(getattr(status, "status", "") or "").lower()
    return ref, name


def fx_still_working(trades: Sequence[Any], order_ref: str) -> bool:
    """True when this conversion is already resting at the gateway."""
    done = {"filled"} | _FX_REJECTED
    for trade in trades:
        ref, name = _fx_name(trade)
        if ref == order_ref and name not in done:
            return True
    return False


def fx_rejected(trades: Sequence[Any], order_ref: str) -> bool:
    """True when every copy of this ref is rejected.

    An older cancelled order does not count once a later copy is still working.
    No matching trade is not a rejection; the new order may not be booked yet.
    """
    matched = False
    for trade in trades:
        ref, name = _fx_name(trade)
        if ref != order_ref:
            continue
        matched = True
        if name not in _FX_REJECTED:
            return False
    return matched
