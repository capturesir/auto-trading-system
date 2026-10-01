"""Commission and tax defaults from strategy spec §2.2 and §2.3.

Numbers are configuration, not literals buried in the engine. Tests call
these functions directly. Rates marked 待核對 in the spec stay in
``config/costs.yaml`` as the same defaults.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Cost:
    commission: float
    tax: float
    currency: str


def us_cost(shares: float, notional: float, *, sell: bool) -> Cost:
    """IBKR Pro fixed: USD 0.005/share, minimum 1, maximum 1% of notional."""
    per_share = shares * 0.005
    commission = min(max(per_share, 1.0), notional * 0.01)
    tax = 0.0
    if sell:
        sec = notional * 0.0000206
        taf = min(shares * 0.000195, 9.79)
        tax = sec + taf
    return Cost(commission, tax, "USD")


def jp_cost(notional: float) -> Cost:
    """0.08% with a JPY 80 minimum."""
    return Cost(max(notional * 0.0008, 80.0), 0.0, "JPY")


def hk_cost(notional: float) -> Cost:
    """Commission, stamp duty rounded up to a whole HKD, and the four levies."""
    commission = max(notional * 0.0008, 18.0)
    stamp = math.ceil(notional * 0.001 - 1e-9)
    levies = notional * (0.000027 + 0.0000015 + 0.0000565 + 0.000042)
    return Cost(commission, stamp + levies, "HKD")


def kr_sell_tax_rate(session: date, *, kosdaq: bool) -> float:
    """Korea securities transaction tax on sells, by effective date."""
    if session >= date(2026, 1, 1):
        return 0.0020
    if session >= date(2025, 1, 1):
        return 0.0015
    if session >= date(2024, 1, 1):
        return 0.0018
    if session >= date(2023, 1, 1):
        return 0.0020
    if session >= date(2021, 1, 1):
        return 0.0023
    return 0.0020 if not kosdaq else 0.0020


def kr_cost(notional: float, session: date, *, sell: bool, kosdaq: bool = False) -> Cost:
    """Tier I 0.06% with a KRW 4,000 minimum, plus sell-side tax."""
    commission = max(notional * 0.0006, 4000.0)
    tax = notional * kr_sell_tax_rate(session, kosdaq=kosdaq) if sell else 0.0
    return Cost(commission, tax, "KRW")


def tw_cost(notional: float, *, sell: bool, etf: bool = False) -> Cost:
    """0.08% with a TWD 80 minimum. Stock sell tax is 0.3% (ETF 0.1%)."""
    commission = max(notional * 0.0008, 80.0)
    rate = 0.001 if etf else 0.003
    tax = notional * rate if sell else 0.0
    return Cost(commission, tax, "TWD")
