"""Market data for US, Japan, Hong Kong, Korea, and Taiwan equities.

Quotes and bars will come from IBKR. This package does not fetch anything yet.
"""

MARKETS = ("US", "JP", "HK", "KR", "TW")


def fetch_bars(symbol: str, timeframe: str) -> None:
    """Placeholder for historical or live bars."""
    raise NotImplementedError("market data is not implemented yet")
