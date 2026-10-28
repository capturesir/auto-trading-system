"""HKD per one unit of foreign currency.

The account snapshot refreshes ``state/fx.json`` from IBKR ``ExchangeRate``
rows. IB quotes each row as units of the account base currency per one unit
of that currency. When the account base is HKD, or when it cannot be
identified, the figure is already what the gate stores. When the account
base is another currency, an HKD quote re-expresses the map. A missing HKD
quote is omitted rather than stored as 1, so the offline table can still
apply. Until a snapshot arrives, that table matches the backtest rates.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

# IB prints ``1.00`` for the account base. Anything farther away is another currency.
_ONE = 1e-6
_RATE_TAGS = frozenset({"ExchangeRate", "$LEDGER-ExchangeRate"})
_BASE_CCY_TAGS = frozenset({"RealCurrency", "$LEDGER-RealCurrency"})

# Same offline table the backtest uses when a test does not pass rates.
DEFAULT_RATES_HKD: dict[str, float] = {
    "HKD": 1.0,
    "USD": 7.8,
    "TWD": 0.244,
    "JPY": 0.052,
    "KRW": 0.0056,
}

# Yahoo ``CCY=X`` is units of that currency per one USD. ``HKD=X`` is the
# exception: it is already HKD per one USD, which is the USD entry below.
_PUBLIC_PER_USD: dict[str, str] = {
    "USD": "HKD=X",
    "TWD": "TWD=X",
    "JPY": "JPY=X",
    "KRW": "KRW=X",
}

log = logging.getLogger(__name__)


def _field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "")
    return str(getattr(item, name, "") or "")


def _currency_code(value: str) -> str | None:
    """A real ISO-style code. The ``BASE`` alias is not one."""
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None
    return code


def _raw_exchange_rates(rows: list[Any]) -> dict[str, float]:
    """IB's own quotes: account-base units per one unit of the row currency."""
    rates: dict[str, float] = {}
    for item in rows:
        if _field(item, "tag") not in _RATE_TAGS:
            continue
        currency = _field(item, "currency").strip().upper()
        if not currency or currency in rates:
            continue
        try:
            rate = float(_field(item, "value"))
        except ValueError:
            continue
        if rate <= 0:
            continue
        rates[currency] = rate
    return rates


def account_base_currency(rows: list[Any]) -> str | None:
    """IB account base currency from AccountValues, or None when it is ambiguous.

    ``RealCurrency`` on the BASE ledger row is used when its value is a real
    code. Otherwise the currency whose ``ExchangeRate`` is 1 is the base:
    IB defines that rate as the price of one unit in the account base.
    With no such quote, a single ``NetLiquidation`` currency is the base
    only when IB did not also send a BASE line. Two currencies and no BASE
    line stay unresolved.
    """
    explicit: list[str] = []
    for item in rows:
        if _field(item, "tag") not in _BASE_CCY_TAGS:
            continue
        if _field(item, "currency").strip().upper() not in {"", "BASE"}:
            continue
        code = _currency_code(_field(item, "value"))
        if code and code not in explicit:
            explicit.append(code)
    if len(explicit) == 1:
        return explicit[0]
    if len(explicit) > 1:
        return None

    quoted = _raw_exchange_rates(rows)
    ones = [
        code
        for currency, rate in quoted.items()
        if (code := _currency_code(currency)) is not None and abs(rate - 1.0) <= _ONE
    ]
    unique = list(dict.fromkeys(ones))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        return None
    if any(
        _currency_code(currency) is not None and abs(rate - 1.0) > _ONE
        for currency, rate in quoted.items()
    ):
        return None

    has_base_nlv = any(
        _field(item, "tag") == "NetLiquidation"
        and _field(item, "currency").strip().upper() == "BASE"
        for item in rows
    )
    if has_base_nlv:
        return None
    seen: list[str] = []
    for item in rows:
        if _field(item, "tag") != "NetLiquidation":
            continue
        code = _currency_code(_field(item, "currency"))
        if code and code not in seen:
            seen.append(code)
    if len(seen) == 1:
        return seen[0]
    return None


def total_cash_base(rows: list[Any]) -> float | None:
    """``TotalCashValue`` in BASE. A missing line is not replaced with NLV."""
    for item in rows:
        if _field(item, "tag") != "TotalCashValue":
            continue
        if _field(item, "currency") != "BASE":
            continue
        try:
            return float(_field(item, "value"))
        except ValueError:
            return None
    return None


def rates_from_account_values(rows: list[Any]) -> dict[str, float]:
    """IBKR ``ExchangeRate`` rows, as HKD per one unit of currency.

    An empty map means the account base is not HKD and IB did not quote HKD,
    so callers keep the offline table instead of treating a 1.0 base rate as
    HKD per unit.
    """
    raw = _raw_exchange_rates(rows)
    base = account_base_currency(rows)
    if base and base != "HKD":
        per_base_hkd = raw.get("HKD")
        if per_base_hkd is None or per_base_hkd <= 0:
            return {}
        rates = {"HKD": 1.0}
        for currency, rate in raw.items():
            code = _currency_code(currency)
            if code is None or code == "HKD":
                continue
            rates[code] = rate / per_base_hkd
        return rates
    rates = {"HKD": 1.0}
    for currency, rate in raw.items():
        code = _currency_code(currency)
        if code is None or code == "HKD":
            continue
        rates[code] = rate
    return rates


def store_rates(home: Path, rates: dict[str, float]) -> None:
    payload = {"schema_version": "fx/1.0", "hkd_per_unit": rates}
    path = home / "state" / "fx.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_rates(home: Path, account: dict[str, Any] | None = None) -> dict[str, float]:
    """Snapshot rates, else the cached file, else the offline table.

    An explicit snapshot map is used as-is (plus HKD). Missing currencies
    stay missing so the gate can refuse them. Strategy sizing calls
    :func:`fill_missing_rates` on a copy and does not write that copy back.
    """
    raw = account.get("fx_to_base") if isinstance(account, dict) else None
    if isinstance(raw, dict) and raw:
        rates = _positive(raw)
        rates.setdefault("HKD", 1.0)
        return rates
    path = home / "state" / "fx.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        table = payload.get("hkd_per_unit") if isinstance(payload, dict) else None
        if isinstance(table, dict) and table:
            rates = _positive(table)
            rates.setdefault("HKD", 1.0)
            return rates
    return dict(DEFAULT_RATES_HKD)


def _positive(raw: Mapping[str, Any]) -> dict[str, float]:
    rates: dict[str, float] = {}
    for key, value in raw.items():
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            rates[str(key)] = rate
    return rates


def currencies_for_tickers(tickers: Iterable[str]) -> list[str]:
    """Quote currencies for sizing. Unknown tickers are omitted."""
    from auto_trading.market.instruments import describe

    found: list[str] = []
    for ticker in tickers:
        try:
            code = describe(str(ticker)).currency
        except (TypeError, ValueError):
            continue
        if code and code not in found:
            found.append(code)
    return found


def _fx_http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "paper-fx/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("utf-8")


def _public_hkd_per_unit(
    currency: str,
    *,
    getter: Callable[[str], str] | None,
    usd_hkd: float | None,
) -> float:
    """HKD per one unit of ``currency`` from Yahoo. Raises when the quote fails."""
    from auto_trading.market.yahoo import public_quote

    pair = _PUBLIC_PER_USD.get(currency)
    if pair is None:
        raise ValueError(f"no public FX pair for {currency}")
    price, _as_of = public_quote(pair, getter=getter or _fx_http_get)
    if price <= 0:
        raise ValueError(f"non-positive public FX quote for {currency}")
    if currency == "USD":
        return float(price)
    if usd_hkd is None or usd_hkd <= 0:
        raise ValueError(f"missing USD rate for {currency}")
    return float(usd_hkd) / float(price)


def fill_missing_rates(
    rates: Mapping[str, float],
    needed: Iterable[str],
    *,
    getter: Callable[[str], str] | None = None,
) -> dict[str, float]:
    """Copy snapshot rates and fill gaps from Yahoo for strategy sizing.

    Existing positive rates are kept. HKD, when absent, is 1. A currency
    Yahoo cannot price stays absent so the caller can skip that symbol.
    This map is not written back to ``fx.json`` or the account snapshot:
    the cash gate still sees only broker rates from :func:`load_rates`.
    The offline ``DEFAULT_RATES_HKD`` table is not used here.
    """
    filled = {str(key).strip().upper(): value for key, value in _positive(rates).items()}
    if "HKD" not in filled:
        filled["HKD"] = 1.0
        log.info("fx_fallback=identity currency=HKD rate=1")
    wanted: list[str] = []
    for raw in needed:
        code = str(raw or "").strip().upper()
        if code and code not in wanted:
            wanted.append(code)
    missing = [code for code in wanted if code not in filled]
    if not missing:
        return filled
    ordered = sorted(missing, key=lambda code: (code != "USD", code))
    usd_hkd = filled.get("USD")
    for code in ordered:
        if code != "USD" and (usd_hkd is None or usd_hkd <= 0):
            try:
                usd_hkd = _public_hkd_per_unit("USD", getter=getter, usd_hkd=None)
            except Exception as exc:
                log.warning("fx_fallback=skip currency=%s reason=%s", code, type(exc).__name__)
                continue
            filled["USD"] = usd_hkd
            log.info("fx_fallback=yahoo currency=USD rate=%s", usd_hkd)
        try:
            rate = _public_hkd_per_unit(code, getter=getter, usd_hkd=usd_hkd)
        except Exception as exc:
            log.warning("fx_fallback=skip currency=%s reason=%s", code, type(exc).__name__)
            continue
        filled[code] = rate
        if code == "USD":
            usd_hkd = rate
        log.info("fx_fallback=yahoo currency=%s rate=%s", code, rate)
    return filled
