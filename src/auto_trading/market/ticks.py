"""Exchange tick tables.

Bands follow the published exchange schedules and are marked 待核對 where the
strategy spec does not include a tick table of its own (the v0.1 table was
not attached). Prices sit on the band that contains them. The upper edge of
a band uses the next band's tick.
"""

from __future__ import annotations

# (upper_inclusive, tick). The last upper is None (no cap).
_US = ((None, 0.01),)
_HK = (
    (0.25, 0.001),
    (0.50, 0.005),
    (10, 0.01),
    (20, 0.02),
    (100, 0.05),
    (200, 0.10),
    (500, 0.20),
    (1000, 0.50),
    (2000, 1.0),
    (5000, 2.0),
    (None, 5.0),
)
_TW = (
    (10, 0.01),
    (50, 0.05),
    (100, 0.1),
    (500, 0.5),
    (1000, 1.0),
    (None, 5.0),
)
# Tokyo Stock Exchange price ticks, 待核對 against the IB contract.
_JP = (
    (3000, 1.0),
    (5000, 5.0),
    (30000, 10.0),
    (50000, 50.0),
    (300000, 100.0),
    (500000, 500.0),
    (3000000, 1000.0),
    (5000000, 5000.0),
    (None, 10000.0),
)
_KR = (
    (2000, 1.0),
    (5000, 5.0),
    (20000, 10.0),
    (50000, 50.0),
    (200000, 100.0),
    (500000, 500.0),
    (None, 1000.0),
)

_TABLES = {"US": _US, "HK": _HK, "TW": _TW, "JP": _JP, "KR": _KR}


def tick_size(market: str, price: float) -> float:
    """Return the tick that applies at ``price`` for ``market``."""
    table = _TABLES[market]
    for upper, tick in table:
        if upper is None or price <= upper:
            return tick
    return table[-1][1]
