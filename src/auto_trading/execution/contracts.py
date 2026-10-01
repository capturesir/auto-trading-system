"""IBKR contract fields for the five equity markets.

``2330.TW`` maps to symbol ``2330`` on ``TWSE``. Exchange codes that have not
been qualified against a paper Gateway are marked unverified for E1.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_trading.market.instruments import describe

# TODO E1 paper-account tests: qualifyContracts against Gateway before trusting these.
UNVERIFIED_EXCHANGES = frozenset({"TWSE", "TPEX", "TSEJ", "KSE"})


class UnsupportedInstrument(Exception):
    """The symbol is outside the equity whitelist."""


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    ib_symbol: str
    exchange: str
    currency: str
    market: str
    primary_exchange: str | None
    lot_size: int
    unverified: bool


def map_contract(ticker: str, *, allow_tpex: bool = False) -> ContractSpec:
    """Map a Yahoo ticker to IBKR stock fields. No network."""
    if ticker.endswith("-USD") or "/" in ticker:
        raise UnsupportedInstrument("REJECT_UNSUPPORTED_INSTRUMENT")
    if ticker.endswith(".TWO"):
        if not allow_tpex:
            raise UnsupportedInstrument("REJECT_UNSUPPORTED_INSTRUMENT")
        symbol = ticker[: -len(".TWO")]
        return ContractSpec(
            symbol=ticker,
            ib_symbol=symbol,
            exchange="TPEX",
            currency="TWD",
            market="TW",
            primary_exchange=None,
            lot_size=1000,
            unverified=True,
        )
    spec = describe(ticker)
    primary = None
    exchange = spec.ib_exchange
    if ticker.endswith(".KS") or ticker.endswith(".KQ"):
        primary = "KSE"
        exchange = "SMART"
    unverified = exchange in UNVERIFIED_EXCHANGES or primary in UNVERIFIED_EXCHANGES
    return ContractSpec(
        symbol=ticker,
        ib_symbol=spec.ib_symbol,
        exchange=exchange,
        currency=spec.currency,
        market=spec.market,
        primary_exchange=primary,
        lot_size=spec.lot_size,
        unverified=unverified,
    )
