"""Yahoo chart downloader with a parquet cache and retries.

Tests pass ``getter``. Nothing in the test suite calls the network. A cache
hit returns the parquet file and does not call ``getter``.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_trading.market.bars import read_parquet, write_parquet
from auto_trading.market.instruments import describe

Getter = Callable[[str], str]


class DownloadError(Exception):
    """The chart request failed on every attempt."""


def chart_url(ticker: str) -> str:
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker}?interval=1d&events=div%2Csplit&range=max"
    )


def quote_url(ticker: str) -> str:
    """Intraday chart used as the public last price. No new data package."""
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"


def _urllib_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_chart(payload: dict[str, Any], *, ticker: str) -> list[dict[str, Any]]:
    """Turn one chart response into daily rows. Adjusted close tracks splits."""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise DownloadError("chart result is empty")
    block = result[0]
    stamps = block.get("timestamp") or []
    quote = (block.get("indicators") or {}).get("quote") or [{}]
    adj = (block.get("indicators") or {}).get("adjclose") or [{}]
    opens = quote[0].get("open") or []
    highs = quote[0].get("high") or []
    lows = quote[0].get("low") or []
    closes = quote[0].get("close") or []
    volumes = quote[0].get("volume") or []
    adjusted = adj[0].get("adjclose") if adj else None
    rows: list[dict[str, Any]] = []
    for index, stamp in enumerate(stamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        day = datetime.fromtimestamp(int(stamp), tz=UTC).date().isoformat()
        adj_close = adjusted[index] if adjusted and index < len(adjusted) else close
        factor = (adj_close / close) if close else 1.0
        rows.append(
            {
                "date": day,
                "open": opens[index],
                "high": highs[index],
                "low": lows[index],
                "close": close,
                "adj_close": adj_close,
                "adj_factor": factor,
                "volume": volumes[index] if index < len(volumes) else 0,
                "dividend": 0.0,
                "split_ratio": 1.0,
                "source": "yahoo",
                "ingested_at": datetime.now(tz=UTC).isoformat(),
                "available_at": f"{day}T07:00:00+08:00",
                "qc_flags": "",
                "ticker": ticker,
            }
        )
    if not rows:
        raise DownloadError("chart contained no closes")
    return rows


def parse_public_quote(payload: dict[str, Any], *, ticker: str) -> tuple[float, datetime]:
    """Last price and its exchange timestamp from one chart payload."""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise DownloadError(f"public quote for {ticker} is empty")
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    stamp = meta.get("regularMarketTime")
    if price is None or stamp is None:
        raise DownloadError(f"public quote for {ticker} has no last price")
    return float(price), datetime.fromtimestamp(int(stamp), tz=UTC)


def public_quote(
    ticker: str,
    *,
    getter: Getter | None = None,
) -> tuple[float, datetime]:
    """Fetch one public last price through the existing Yahoo chart client."""
    fetch = getter or _urllib_get
    body = fetch(quote_url(ticker))
    return parse_public_quote(json.loads(body), ticker=ticker)


def download_chart(
    ticker: str,
    *,
    getter: Getter | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch daily bars, retrying transient failures."""
    fetch = getter or _urllib_get
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            body = fetch(chart_url(ticker))
            return parse_chart(json.loads(body), ticker=ticker)
        except Exception as exc:
            last = exc
            if attempt == retries:
                break
            sleep(0.25 * attempt)
    raise DownloadError(str(last)) from last


def cache_path(cache_dir: Path, ticker: str) -> Path:
    market = describe(ticker).market
    return cache_dir / market / f"{ticker}.parquet"


def load_daily_bars(
    ticker: str,
    timeframe: str = "1d",
    *,
    cache_dir: Path,
    getter: Getter | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Return cached daily bars, downloading on a miss."""
    if timeframe not in {"1d", "1day", "daily"}:
        raise ValueError(f"unsupported timeframe {timeframe}")
    path = cache_path(cache_dir, ticker)
    if path.is_file():
        return read_parquet(path)
    rows = download_chart(ticker, getter=getter, retries=retries, sleep=sleep)
    write_parquet(path, rows)
    return rows
