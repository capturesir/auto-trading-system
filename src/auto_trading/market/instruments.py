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
    if ticker.endswith(".KS") or ticker.endswith(".KQ"):
        symbol = ticker.split(".")[0]
        # KRX is the venue name IBKR publishes now. KSE failed qualify on 2026-10-06.
        return InstrumentSpec(ticker, "KR", "KRW", 1, 1, symbol, "KRX", "XKRX")
    return InstrumentSpec(ticker, "US", "USD", 1, 1, ticker.replace("-", " "), "SMART", "XNYS")
