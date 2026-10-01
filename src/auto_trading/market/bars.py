"""Daily bar cache. Downloads stay behind an injected callable so tests never hit the network."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

COLUMNS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "adj_factor",
    "volume",
    "dividend",
    "split_ratio",
    "source",
    "ingested_at",
    "available_at",
    "qc_flags",
)


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write one ticker cache. Missing columns are filled with None."""
    import pandas as pd

    frame = pd.DataFrame(rows)
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.loc[:, COLUMNS].to_parquet(path, index=False)


def read_parquet(path: Path) -> list[dict[str, Any]]:
    """Load a cache file."""
    import pandas as pd

    frame = pd.read_parquet(path)
    return frame.to_dict(orient="records")


def fetch_daily(
    ticker: str,
    market: str,
    path: Path,
    downloader: Callable[[str], list[dict[str, Any]]],
) -> Path:
    """Store rows from ``downloader``. The default process does not call Yahoo."""
    rows = downloader(ticker)
    for row in rows:
        row.setdefault("source", "injected")
    write_parquet(path, rows)
    return path
