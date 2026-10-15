"""Market data for US, Japan, Hong Kong, Korea, and Taiwan equities.

Daily history comes from the Yahoo chart API through a local parquet cache.
``fetch_bars`` does not import a broker client.
"""

from pathlib import Path

MARKETS = ("US", "JP", "HK", "KR", "TW")


def fetch_bars(symbol: str, timeframe: str, **kwargs):
    """Load daily bars. Pass ``cache_dir`` and, in tests, ``getter``."""
    from auto_trading.market.yahoo import load_daily_bars

    cache_dir = kwargs.pop("cache_dir", None)
    if cache_dir is None:
        cache_dir = Path("data/daily")
    return load_daily_bars(symbol, timeframe, cache_dir=Path(cache_dir), **kwargs)
