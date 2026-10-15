"""Bar quality flags for injected rows. ERROR names skip new entries for that ticker."""

from __future__ import annotations

from typing import Any


def qc_flags(rows: list[dict[str, Any]], expected_dates: list[str]) -> list[str]:
    """Return flag names. An empty list means the rows passed these checks."""
    flags: list[str] = []
    by_date = {str(row.get("date")): row for row in rows}
    for day in expected_dates:
        if day not in by_date:
            flags.append(f"missing_session:{day}")
    for row in rows:
        values = [row.get(name) for name in ("open", "high", "low", "close")]
        if any(value is None for value in values):
            flags.append(f"null_ohlc:{row.get('date')}")
            continue
        high, low = float(row["high"]), float(row["low"])
        open_, close = float(row["open"]), float(row["close"])
        if high < low or high < max(open_, close) or low > min(open_, close):
            flags.append(f"ohlc_inconsistent:{row.get('date')}")
        if float(row.get("volume") or 0) == 0:
            flags.append(f"zero_volume:{row.get('date')}")
    return flags
