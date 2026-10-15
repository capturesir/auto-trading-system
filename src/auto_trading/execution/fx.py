"""HKD per one unit of foreign currency.

The account snapshot refreshes ``state/fx.json`` from IBKR ``ExchangeRate``
rows (units of BASE per one unit of that currency; BASE is HKD). Until a
snapshot arrives, the offline table matches the backtest rates. A currency
that is still missing is not treated as 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Same offline table the backtest uses when a test does not pass rates.
DEFAULT_RATES_HKD: dict[str, float] = {
    "HKD": 1.0,
    "USD": 7.8,
    "TWD": 0.244,
    "JPY": 0.052,
    "KRW": 0.0056,
}


def _field(item: Any, name: str) -> str:
    if isinstance(item, dict):
        return str(item.get(name, "") or "")
    return str(getattr(item, name, "") or "")


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
    """IBKR ``ExchangeRate`` rows, as HKD (BASE) per one unit of currency."""
    rates: dict[str, float] = {"HKD": 1.0}
    for item in rows:
        if _field(item, "tag") != "ExchangeRate":
            continue
        currency = _field(item, "currency")
        if currency in {"", "BASE", "HKD"}:
            continue
        try:
            rate = float(_field(item, "value"))
        except ValueError:
            continue
        if rate <= 0:
            continue
        rates[currency] = rate
    return rates


def store_rates(home: Path, rates: dict[str, float]) -> None:
    payload = {"schema_version": "fx/1.0", "hkd_per_unit": rates}
    path = home / "state" / "fx.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_rates(home: Path, account: dict[str, Any] | None = None) -> dict[str, float]:
    """Snapshot rates, else the cached file, else the offline table.

    An explicit snapshot map is used as-is (plus HKD). Missing currencies
    stay missing so the gate can refuse them.
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


def _positive(raw: dict[str, Any]) -> dict[str, float]:
    rates: dict[str, float] = {}
    for key, value in raw.items():
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            rates[str(key)] = rate
    return rates
