"""Yahoo and IBKR ticker conversion.

``data/ref/symbols.csv`` wins when it contains the row. Otherwise the
suffix rules in the execution spec apply. Reverse conversion is for
reconciliation and reports.
"""

from __future__ import annotations

import csv
from pathlib import Path

_KOSDAQ = frozenset({"KQ", "KOSDAQ", "XKOS", ".KQ"})


def yahoo_to_ib(ticker: str, *, symbols_csv: Path | None = None) -> str:
    """Return the IBKR symbol for a Yahoo ticker."""
    mapped = _csv_value(symbols_csv, "ticker", ticker, "ib_symbol")
    if mapped is not None:
        return mapped
    if ticker.endswith(".HK"):
        return ticker[: -len(".HK")].lstrip("0") or "0"
    if ticker.endswith(".T"):
        return ticker[: -len(".T")]
    if ticker.endswith(".KS") or ticker.endswith(".KQ") or ticker.endswith(".TW"):
        return ticker.split(".", 1)[0]
    return ticker.replace("-", " ").replace(".", " ")


def ib_to_yahoo(
    ib_symbol: str,
    *,
    market: str | None = None,
    primary_exchange: str | None = None,
    symbols_csv: Path | None = None,
) -> str:
    """Return the Yahoo ticker for an IBKR symbol."""
    mapped = _csv_value(symbols_csv, "ib_symbol", ib_symbol, "ticker")
    if mapped is not None:
        return mapped
    if market == "HK":
        return f"{ib_symbol.zfill(4)}.HK"
    if market == "JP":
        return f"{ib_symbol}.T"
    if market == "TW":
        return f"{ib_symbol}.TW"
    if market == "KR" or (ib_symbol.isdigit() and len(ib_symbol) == 6):
        suffix = ".KQ" if _is_kosdaq(primary_exchange) else ".KS"
        return f"{ib_symbol}{suffix}"
    if " " in ib_symbol:
        return ib_symbol.replace(" ", "-")
    return ib_symbol


def _is_kosdaq(primary_exchange: str | None) -> bool:
    if not primary_exchange:
        return False
    return primary_exchange.upper() in _KOSDAQ


def _csv_value(path: Path | None, key: str, wanted: str, column: str) -> str | None:
    if path is None or not path.is_file():
        return None
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get(key) == wanted and row.get(column):
                return str(row[column])
    return None
