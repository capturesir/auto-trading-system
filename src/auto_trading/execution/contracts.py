"""IBKR contract fields for the five equity markets.

``2330.TW`` maps to symbol ``2330`` on ``TWSE``. Exchange codes that have not
been qualified against a paper Gateway are marked unverified for E1.

Korean names use the six-digit local code, currency ``KRW``, and exchange
``KRX``. On 2026-10-06 the paper Gateway's ``qualifyContracts`` for
``005930`` / ``KSE`` / ``KRW`` returned no details; ib_async logs that as
``Unknown contract``. ``KSE`` is the old venue code. SMART is not a
documented Korea route. An empty qualify can also mean the account has no
Korea stock permission (Client Portal, Trading Permissions, Stocks, Korea).
Residents of Korea and IBIN or IBSJ clients cannot enable that permission.
TODO: qualify ``KRX`` on the paper Gateway before treating the conId as known.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from auto_trading.market.instruments import describe

# TODO E1 paper-account tests: qualifyContracts against Gateway before trusting these.
# KRX is the candidate after KSE returned Unknown contract on 2026-10-06.
UNVERIFIED_EXCHANGES = frozenset({"TWSE", "TPEX", "TSEJ", "KSE", "KRX"})


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


def ib_contract_fields(ticker: str) -> dict[str, str]:
    """Fields a qualify request would send. No network."""
    spec = map_contract(ticker)
    fields = {
        "symbol": spec.ib_symbol,
        "secType": "STK",
        "exchange": spec.exchange,
        "currency": spec.currency,
    }
    if spec.primary_exchange:
        fields["primaryExchange"] = spec.primary_exchange
    return fields


def qualify_with(ticker: str, qualify: Callable[[dict[str, str]], list[Any]]) -> dict[str, Any]:
    """Run ``qualify``. An empty list is ib_async's Unknown contract result."""
    fields = ib_contract_fields(ticker)
    found = qualify(fields)
    if not found:
        return {"fields": fields, "con_id": None, "error": "Unknown contract"}
    row = found[0]
    con_id = row["conId"] if isinstance(row, dict) else row
    return {"fields": fields, "con_id": con_id, "error": None}
