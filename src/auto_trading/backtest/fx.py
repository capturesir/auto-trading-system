"""Base-currency conversion. ``BASE_CURRENCY`` is HKD or USD, default HKD.

Yahoo ``CCY=X`` quotes are units of CCY per one USD. HKD per one unit of CCY
is ``HKD=X / CCY=X``. The engine takes explicit rates so tests stay offline.
"""

from __future__ import annotations


def hkd_per_unit(ccy_per_usd: float, hkd_per_usd: float) -> float:
    """Convert a Yahoo ``CCY=X`` quote into HKD per one unit of that currency."""
    if ccy_per_usd <= 0 or hkd_per_usd <= 0:
        raise ValueError("FX quotes must be positive")
    return hkd_per_usd / ccy_per_usd


def to_base(amount: float, currency: str, rates_hkd_per_unit: dict[str, float], base: str) -> float:
    """Convert ``amount`` of ``currency`` into ``base`` (HKD or USD)."""
    if base not in {"HKD", "USD"}:
        raise ValueError(f"BASE_CURRENCY must be HKD or USD, got {base}")
    if currency == base:
        return amount
    in_hkd = amount * rates_hkd_per_unit[currency]
    if base == "HKD":
        return in_hkd
    return in_hkd / rates_hkd_per_unit["USD"]
