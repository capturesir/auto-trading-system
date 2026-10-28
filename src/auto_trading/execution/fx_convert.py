"""Paper-only foreign-currency cash check before a buy.

HKD and JPY (and the other IdealPro currencies) still preconvert from USD
when ``TRADING_FX_PRECONVERT=1``. That flag does not apply to TWD or KRW.

Taiwan and Korea buys use mode A (補7b #19). Enough target-currency cash
sends the stock and nothing else. A shortfall sends the stock only when
``TWKR_AUTO_FX_VERIFIED=1`` and USD plus the HKD cash R7 already treats as
spendable cover the estimated cost; IB converts on the fill. Otherwise the
gate holds the stock. This module never builds IdealPro, FXCONV, or Convert
Currency orders for TWD or KRW, and it never places an order to clear a
negative TWD or KRW balance.

Nothing here opens a session; the gateway does that on the paper ports only.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
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
ENV_TWKR_AUTO_FX = "TWKR_AUTO_FX_VERIFIED"
TWKR_CCY = frozenset({"TWD", "KRW"})
REASON_BASE_CASH = "R7_BASE_CASH"
REASON_UNVERIFIED = "R31_TWKR_AUTO_FX_UNVERIFIED"
REASON_DEFICIT = "fx_target_cash_deficit"

# IdealPro quotes a spot book. TWD and KRW are not on that path.
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
    """True only when the paper submit path preconverts IdealPro currencies."""
    source = os.environ if env is None else env
    return str(source.get(ENV_FLAG, "")).strip() == "1"


def twkr_auto_fx_verified(env: Mapping[str, str] | None = None) -> bool:
    """Mode A is armed only when ops set ``TWKR_AUTO_FX_VERIFIED=1``.

    Unset or any other value stays off. That is the fail-safe after the
    2026-10-28 paper auto-FX check: the result is known, but a stock order
    that depends on it still waits for this flag.
    """
    source = os.environ if env is None else env
    return str(source.get(ENV_TWKR_AUTO_FX, "")).strip() == "1"


def standalone_fx_forbidden(currency: str) -> bool:
    """TWD and KRW never get a standalone Gateway FX order."""
    return currency.strip().upper() in TWKR_CCY


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


def opening_twkr_buy(intent: OrderIntent) -> bool:
    """Paper BUY entry/add that settles in TWD or KRW."""
    if not opening_foreign_buy(intent):
        return False
    return trade_currency(intent) in TWKR_CCY


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


def usd_equivalent(amount: float, currency: str, fx_to_base: Mapping[str, float]) -> float | None:
    """``amount`` of ``currency`` restated in USD via the HKD-per-unit map.

    A non-positive amount is zero and does not need a rate. A missing or
    non-positive rate fails closed with ``None``.
    """
    if amount <= 0:
        return 0.0
    per_ccy = fx_to_base.get(currency)
    per_usd = fx_to_base.get(FUNDING_CCY)
    if per_ccy is None or per_usd is None:
        return None
    try:
        ccy_rate = float(per_ccy)
        usd_rate = float(per_usd)
    except (TypeError, ValueError):
        return None
    if ccy_rate <= 0 or usd_rate <= 0:
        return None
    return amount * ccy_rate / usd_rate


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
    hkd_qty: float | None = None


def fx_order_ref(intent: OrderIntent) -> str:
    key = intent.intent_id or intent.client_id
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"fx-{digest}"


def venue_for(currency: str) -> tuple[str, str]:
    """Return ``(venue, order_type)``. IdealPro uses a market FX order.

    TWD and KRW raise. Mode A never builds a venue for those currencies.
    """
    code = currency.strip().upper()
    if code in TWKR_CCY:
        raise ValueError("standalone FX for TWD/KRW is retired")
    if code in IDEALPRO_CCY:
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


def _broker_amount(cash: Mapping[str, float] | None, currency: str) -> float | None:
    if cash is None:
        return None
    try:
        return float(cash.get(currency, 0.0) or 0.0)
    except (TypeError, ValueError):
        return None


def _assess_twkr(
    intent: OrderIntent,
    *,
    cash_by_ccy: Mapping[str, float] | None,
    open_orders: Sequence[Any],
    fx_to_base: Mapping[str, float] | None,
    broker_cash: Mapping[str, float] | None,
    verified: bool | None,
) -> FxAttempt:
    """Mode A for a paper TWD or KRW opening buy. Never returns an FX order."""
    target = trade_currency(intent)
    if cash_by_ccy is None:
        return FxAttempt("block", "unknown_balance")
    required = required_target_cash(intent)
    if required is None:
        return FxAttempt("block", "unpriced")
    rates: Mapping[str, float] = fx_to_base or {}
    reported = broker_cash if broker_cash is not None else cash_by_ccy
    balance = _broker_amount(reported, target)
    have = available_cash(target, cash_by_ccy, open_orders, rates)
    if balance is not None and balance < 0:
        return FxAttempt(
            "block",
            "target_cash_deficit",
            required_target=required,
            have_target=have,
        )
    if have + 1e-6 >= required:
        return FxAttempt(
            "covered",
            "target_cash",
            required_target=required,
            have_target=have,
        )
    shortfall = required - max(have, 0.0)
    usd_need = usd_equivalent(shortfall, target, rates)
    if usd_need is None:
        return FxAttempt(
            "block",
            "no_fx_rate",
            required_target=required,
            have_target=have,
        )
    usd_have = available_cash(FUNDING_CCY, cash_by_ccy, open_orders, rates)
    hkd_have = available_cash("HKD", cash_by_ccy, open_orders, rates)
    hkd_as_usd = 0.0
    if hkd_have > 0:
        converted = usd_equivalent(hkd_have, "HKD", rates)
        if converted is None and usd_have + 1e-6 < usd_need:
            return FxAttempt(
                "block",
                "no_fx_rate",
                required_target=required,
                have_target=have,
                usd_qty=usd_need,
            )
        hkd_as_usd = float(converted or 0.0)
    if usd_have + hkd_as_usd + 1e-6 < usd_need:
        return FxAttempt(
            "block",
            "usd_short",
            required_target=required,
            have_target=have,
            usd_qty=usd_need,
        )
    armed = twkr_auto_fx_verified() if verified is None else bool(verified)
    usd_used = min(max(usd_have, 0.0), usd_need)
    remainder = max(usd_need - usd_used, 0.0)
    hkd_used = 0.0
    if remainder > 1e-9 and hkd_as_usd > 0 and hkd_have > 0:
        hkd_used = remainder / hkd_as_usd * hkd_have
    if not armed:
        return FxAttempt(
            "block",
            "auto_fx_unverified",
            required_target=required,
            have_target=have,
            usd_qty=usd_used,
            hkd_qty=hkd_used,
        )
    return FxAttempt(
        "covered",
        "auto_fx",
        required_target=required,
        have_target=have,
        usd_qty=usd_used,
        hkd_qty=hkd_used,
    )


def assess(
    intent: OrderIntent,
    *,
    cash_by_ccy: Mapping[str, float] | None,
    open_orders: Sequence[Any] = (),
    fx_to_base: Mapping[str, float] | None = None,
    broker_cash: Mapping[str, float] | None = None,
    verified: bool | None = None,
) -> FxAttempt:
    """Decide whether a paper buy may go out, or still needs an IdealPro conversion.

    TWD and KRW never produce a conversion order. HKD, JPY, and the other
    IdealPro currencies still do when the target cash is short and USD covers
    the FX order.
    """
    if intent.mode != "paper" or intent.live_size_multiplier is not None:
        return FxAttempt("block", "not_paper")
    if not opening_foreign_buy(intent):
        return FxAttempt("skip")
    if opening_twkr_buy(intent):
        return _assess_twkr(
            intent,
            cash_by_ccy=cash_by_ccy,
            open_orders=open_orders,
            fx_to_base=fx_to_base,
            broker_cash=broker_cash,
            verified=verified,
        )
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


def twkr_gate_code(attempt: FxAttempt) -> str | None:
    """Map a mode-A attempt onto a transient gate code. ``None`` lets the stock through."""
    if attempt.action in {"covered", "skip"}:
        return None
    if attempt.reason == "not_paper":
        return None
    if attempt.reason == "auto_fx_unverified":
        return REASON_UNVERIFIED
    if attempt.reason == "target_cash_deficit":
        return REASON_DEFICIT
    return REASON_BASE_CASH


def twkr_detail(
    attempt: FxAttempt,
    *,
    phase: str,
    account_base: str | None,
    verified: bool,
) -> dict[str, Any]:
    """Fields stored on ``risk_checks.detail_json`` for a Taiwan or Korea cash decision."""
    return {
        "mode": "A",
        "have_target": attempt.have_target,
        "required_target": attempt.required_target,
        "verified": verified,
        "phase": phase,
        "account_base": account_base,
        "detail": attempt.reason or "target_cash",
        "usd_qty": attempt.usd_qty,
    }


def reserve_funding(book: dict[str, float], attempt: FxAttempt) -> dict[str, float]:
    """Hold the USD and HKD mode A already counted, so the next order cannot reuse them."""
    if attempt.reason != "auto_fx":
        return book
    if attempt.usd_qty:
        book["USD"] = float(book.get("USD", 0.0)) - float(attempt.usd_qty)
    if attempt.hkd_qty:
        book["HKD"] = float(book.get("HKD", 0.0)) - float(attempt.hkd_qty)
    return book


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


def _negative_targets(cash: Mapping[str, float] | None) -> dict[str, float]:
    found: dict[str, float] = {}
    if not isinstance(cash, Mapping):
        return found
    for currency in sorted(TWKR_CCY):
        try:
            amount = float(cash.get(currency, 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if amount < 0:
            found[currency] = amount
    return found


def write_fx_cash_gate(
    home: Path,
    *,
    now: str,
    reason: str | None,
    verified: bool,
    cash: Mapping[str, float] | None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Persist the latest mode-A decision and any negative TWD/KRW balances."""
    path = home / "state" / "fx_cash_gate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "fx_cash_gate/1.0",
        "mode": "A",
        "twkr_auto_fx_verified": bool(verified),
        "reason": reason,
        "target_cash_deficit": _negative_targets(cash),
        "detail": dict(detail or {}),
        "at": now,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def warn_if_base_not_usd(home: Path, base: str | None, when: str) -> None:
    """Ops warning when the account base is present and is not USD.

    This does not reject the order. ``BASE_CCY_NOT_USD`` is retired.
    """
    code = (base or "").strip().upper()
    if not code or code == FUNDING_CCY:
        return
    folder = home / "state" / "fx_cash_hold"
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / "base_ccy_warn.txt"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == code:
        return
    from datetime import datetime

    from auto_trading.alerts.critical import write_ops_notice

    parsed = datetime.fromisoformat(when)
    write_ops_notice(
        home,
        message_zh=(
            f"帳戶基準貨幣為 {code}，不是 USD。"
            "台／韓買單仍按模式 A 現金閘處理，不再以 BASE_CCY_NOT_USD 永久拒絕。"
        ),
        now=parsed,
        kind="ops",
    )
    marker.write_text(code + "\n", encoding="utf-8")


def note_target_cash_deficits(
    home: Path,
    cash: Mapping[str, float] | None,
    when: str,
) -> list[str]:
    """Alert on negative TWD/KRW cash. Does not place a clearing FX order.

    A currency that is negative for the first time writes one warning.
    Repeating the same deficit does not write another. Cash back at zero
    or above clears the hold so a later deficit can alert again.
    """
    from datetime import UTC, datetime

    from auto_trading.alerts.critical import MACAU

    deficits = _negative_targets(cash)
    folder = home / "state" / "fx_cash_hold"
    folder.mkdir(parents=True, exist_ok=True)
    fresh: list[str] = []
    for currency in sorted(TWKR_CCY):
        marker = folder / f"{currency}.json"
        if currency not in deficits:
            if marker.is_file():
                marker.unlink()
            continue
        amount = deficits[currency]
        if marker.is_file():
            marker.write_text(
                json.dumps({"currency": currency, "cash": amount, "at": when}) + "\n",
                encoding="utf-8",
            )
            continue
        marker.write_text(
            json.dumps({"currency": currency, "cash": amount, "at": when}) + "\n",
            encoding="utf-8",
        )
        parsed = datetime.fromisoformat(when)
        local = parsed.astimezone(MACAU)
        stamp = local.strftime("%Y%m%dT%H%M%S")
        document = {
            "schema_version": "notice/1.0",
            "kind": REASON_DEFICIT,
            "level": "warn",
            "severity": "warning",
            "source": "execution",
            "currency": currency,
            "cash": amount,
            "clear_fx": False,
            "created_at": local.isoformat(timespec="seconds"),
            "message_zh": (f"{currency} 現金為負（{amount}）。新開台／韓倉先停，不送 API 清負單。"),
        }
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        for relative in ("alerts/pending", "notifications/outbox"):
            dest_dir = home / relative
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / f"{stamp}_fx_target_cash_deficit_{currency}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        fresh.append(currency)
    if fresh:
        from auto_trading.portfolio.journal import TradeJournal

        moment = datetime.fromisoformat(when)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        TradeJournal(home).audit(
            REASON_DEFICIT,
            {"currencies": {name: deficits[name] for name in fresh}, "clear_fx": False},
            when=moment,
        )
    return fresh


async def await_target_cash(
    read_cash: Callable[[], Mapping[str, float] | None],
    *,
    target: str,
    required: float,
    timeout_sec: float = FX_CONFIRM_SEC,
    poll_sec: float = FX_POLL_SEC,
    sleep: Callable[[float], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    rejected: Callable[[], bool] | None = None,
) -> dict[str, float] | None:
    """Poll until ``target`` cash covers ``required``, or the FX order is rejected.

    The pause is ``asyncio.sleep`` unless a test passes its own ``sleep``.
    That yields the running loop so IB account and order updates can arrive.
    ``ib.sleep`` is not used: it calls ``util.run`` and raises
    ``RuntimeError`` when this coroutine's loop is already running.
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
        paused = asyncio.sleep(poll_sec) if sleep is None else sleep(poll_sec)
        if inspect.isawaitable(paused):
            await paused


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
