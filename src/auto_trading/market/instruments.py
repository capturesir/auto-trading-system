"""Instrument identity for the five equity markets.

Lot size, currency, and exchange follow strategy spec §1.2. Taiwan
``lot_size`` stays 1,000. ``qty_step`` is 1 only when the odd-lot toggle is
on; the default toggle is false, so the step is a whole lot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    ticker: str
    market: str
    currency: str
    lot_size: int
    qty_step: int
    ib_symbol: str
    ib_exchange: str
    calendar: str


def describe(ticker: str, *, odd_lot_enabled: bool = False) -> InstrumentSpec:
    """Map a Yahoo ticker to market, lot, and exchange fields."""
    if ticker.endswith(".TW"):
        symbol = ticker[: -len(".TW")]
        step = 1 if odd_lot_enabled else 1000
        return InstrumentSpec(ticker, "TW", "TWD", 1000, step, symbol, "TWSE", "XTAI")
    if ticker.endswith(".HK"):
        symbol = ticker[: -len(".HK")].lstrip("0") or "0"
        return InstrumentSpec(ticker, "HK", "HKD", 1000, 1000, symbol, "SEHK", "XHKG")
    if ticker.endswith(".T"):
        symbol = ticker[: -len(".T")]
        return InstrumentSpec(ticker, "JP", "JPY", 100, 100, symbol, "TSEJ", "XTKS")
    if ticker.endswith(".KS") or ticker.endswith(".KQ") or _bare_korea(ticker):
        symbol = ticker.split(".")[0]
        # Paper Gateway 2026-10-06: 005930 / KRX / KRW -> conId 17382528.
        # A bare six-digit code is the same local symbol. SMART+USD and KSE are error 200.
        return InstrumentSpec(ticker, "KR", "KRW", 1, 1, symbol, "KRX", "XKRX")
    return InstrumentSpec(ticker, "US", "USD", 1, 1, ticker.replace("-", " "), "SMART", "XNYS")


def _bare_korea(ticker: str) -> bool:
    """IB local codes such as ``005930`` have no Yahoo suffix."""
    return len(ticker) == 6 and ticker.isdigit()
