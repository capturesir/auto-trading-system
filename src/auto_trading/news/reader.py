"""Read A-group ``news/1.0`` JSON. Markdown is never parsed.

A file whose ``schema_version`` is not exactly ``news/1.0`` disables the
``a_news`` source and is reported as ``data_error``. ``news_event`` v3 keeps
items with impact ≥ 2 on both subject and affected names, positive direction,
and ``tickers`` only. Empty tickers and ``market_impacts`` are shadow records.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_trading.jsonutil import loads_strict

REQUIRED_SCHEMA = "news/1.0"
EXCLUDED_TYPES = frozenset({"rating", "other"})


@dataclass
class NewsRead:
    enabled: bool
    data_error: str | None
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class NewsJudgement:
    news_id: str
    ticker: str | None
    outcome: str
    reason: str


def read_news(path: Path | None) -> NewsRead:
    """Load one news file. A missing file disables ``a_news``."""
    if path is None or not path.is_file():
        return NewsRead(False, "missing")
    try:
        payload = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return NewsRead(False, f"invalid: {exc}")
    if not isinstance(payload, Mapping) or payload.get("schema_version") != REQUIRED_SCHEMA:
        return NewsRead(False, "schema")
    items = payload.get("items")
    if not isinstance(items, list):
        return NewsRead(False, "schema")
    return NewsRead(True, None, [item for item in items if isinstance(item, dict)])


def _impact_ok(row: Mapping[str, Any], min_impact: int, affected_min: int) -> bool:
    impact = row.get("impact")
    if not isinstance(impact, int):
        return False
    if row.get("relation") == "affected":
        return impact >= affected_min
    return impact >= min_impact


def classify_item(
    item: Mapping[str, Any],
    signal_side: str | None,
    *,
    min_impact: int = 2,
    affected_min_impact: int = 2,
    prices_confirm: bool | None = None,
) -> list[NewsJudgement]:
    """Classify one v3 news item. ``market_impacts`` never open a position."""
    news_id = str(item.get("news_id", ""))
    judgements: list[NewsJudgement] = []
    if item.get("market_impacts"):
        judgements.append(NewsJudgement(news_id, None, "shadow", "market_impacts"))
    tickers = item.get("tickers") or []
    if not tickers:
        if not any(row.reason == "market_impacts" for row in judgements):
            judgements.append(NewsJudgement(news_id, None, "shadow", "empty_tickers"))
        return [row for row in judgements if row.ticker is None][:1] or judgements[:1]
    status = item.get("status")
    event_type = item.get("event_type")
    for row in tickers:
        ticker = str(row.get("ticker"))
        if status != "new" or event_type in EXCLUDED_TYPES:
            judgements.append(NewsJudgement(news_id, ticker, "filtered", "item_filter"))
            continue
        impact_ok = _impact_ok(row, min_impact, affected_min_impact)
        if row.get("direction") != "positive" or not impact_ok:
            judgements.append(NewsJudgement(news_id, ticker, "filtered", "direction_or_impact"))
            continue
        if signal_side == "HOLD":
            judgements.append(NewsJudgement(news_id, ticker, "shadow", "hold_signal"))
            continue
        if signal_side != "BUY":
            judgements.append(NewsJudgement(news_id, ticker, "shadow", "signal_not_buy"))
            continue
        if prices_confirm is False:
            judgements.append(NewsJudgement(news_id, ticker, "unconfirmed", "price_confirm"))
            continue
        if prices_confirm is True:
            judgements.append(NewsJudgement(news_id, ticker, "entry", "confirmed"))
            continue
        judgements.append(NewsJudgement(news_id, ticker, "candidate", "awaiting_confirmation"))
    shadows = [row for row in judgements if row.reason == "market_impacts"]
    return judgements if not shadows else judgements


def visible(visible_at: str, decision_time: str) -> bool:
    """An event is usable only when its visible time is at or before the decision."""
    return visible_at <= decision_time
