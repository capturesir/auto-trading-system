"""Read A-group ``news/1.0`` JSON. Markdown is never parsed.

A file whose ``schema_version`` is not exactly ``news/1.0`` disables the
``a_news`` source and is reported as ``data_error``. ``news_event`` v4 keeps
``status`` new and update, impact ≥ 2 on subject and affected names, positive
direction, and ``tickers`` only. Visibility is the item's ``judged_at``.
Events are de-duplicated by the root ``news_id`` (a trailing ``-u`` / ``-uN``
suffix is stripped). Empty tickers and ``market_impacts`` are shadow records.

``importance`` and ``opportunity`` are optional item fields (2026-10-05).
They are not judgements: they do not create signals, change ``judged_at``,
or turn an item into an update. This package does not vendor the news schema.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.jsonutil import loads_strict

REQUIRED_SCHEMA = "news/1.0"
EXCLUDED_TYPES = frozenset({"rating", "other"})
ACCEPTED_STATUS = frozenset({"new", "update"})
IGNORED_ITEM_FIELDS = frozenset({"importance", "opportunity"})
MACAU = ZoneInfo("Asia/Macau")
PUBLISH_WAIT = timedelta(minutes=15)
POLL_SECONDS = 30.0
_UPDATE_SUFFIX = re.compile(r"-u\d*$")
_EVENT_FIELDS = (
    "news_id",
    "status",
    "event_type",
    "published_at",
    "published_at_precision",
    "first_seen_at",
    "judged_at",
    "generated_at",
    "related_news_ids",
)


def root_news_id(news_id: str) -> str:
    """Original id. ``-u`` and ``-uN`` update suffixes are stripped."""
    return _UPDATE_SUFFIX.sub("", news_id)


def dedupe_chain(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep one item per root id. The latest ``judged_at`` in the chain wins."""
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        root = root_news_id(str(item.get("news_id", "")))
        if root not in best:
            order.append(root)
            best[root] = dict(item)
            continue
        if str(item.get("judged_at") or "") >= str(best[root].get("judged_at") or ""):
            best[root] = dict(item)
    return [best[key] for key in order]


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


def without_extensions(item: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an item without the optional score and opportunity fields."""
    return {key: value for key, value in item.items() if key not in IGNORED_ITEM_FIELDS}


def events_for_strategy(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Ticker rows a strategy may see.

    Beneficiary names inside ``opportunity`` are not tickers. ``judged_at``
    stays the item's own judgement time, and ``status`` is not rewritten.
    """
    events: list[dict[str, Any]] = []
    for item in dedupe_chain(items):
        body = without_extensions(item)
        tickers = body.get("tickers") or []
        if not isinstance(tickers, list):
            continue
        base = {key: body[key] for key in _EVENT_FIELDS if key in body}
        for row in tickers:
            if not isinstance(row, Mapping) or not row.get("ticker"):
                continue
            event = dict(base)
            event["ticker"] = row.get("ticker")
            event["relation"] = row.get("relation")
            event["direction"] = row.get("direction")
            event["impact"] = row.get("impact")
            event["source"] = "a_news"
            events.append(event)
    return events


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("news clock needs a timezone")
    return moment.astimezone(MACAU)


def read_session_news(
    directory: Path,
    session_date: str,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> NewsRead:
    """Load ``news/<session_date>.json`` for the morning decision.

    A-group sometimes writes that file a few minutes after 07:00 Macau.
    A missing file is re-checked until 07:15 on ``session_date`` when the
    clock is already inside that window. Before 07:00, at or after 07:15,
    and on any other day, a missing file is missing immediately. A fixed
    ``now`` does not wait; pass a moving ``clock`` or omit both.
    """
    path = Path(directory) / f"{session_date}.json"
    if path.is_file():
        return read_news(path)
    start = datetime.fromisoformat(f"{session_date}T07:00:00+08:00")
    deadline = start + PUBLISH_WAIT

    def current() -> datetime:
        if clock is not None:
            return _aware(clock())
        if now is not None:
            return _aware(now)
        return datetime.now(MACAU)

    moment = current()
    if moment < start or moment >= deadline or (clock is None and now is not None):
        return NewsRead(False, "missing")
    sleeper = sleep or time.sleep
    seen = moment
    while moment < deadline:
        remaining = (deadline - moment).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(float(poll_seconds), remaining))
        if path.is_file():
            return read_news(path)
        moment = current()
        if moment <= seen:
            break
        seen = moment
    if path.is_file():
        return read_news(path)
    return NewsRead(False, "missing")


def classify_item(
    item: Mapping[str, Any],
    signal_side: str | None,
    *,
    min_impact: int = 2,
    affected_min_impact: int = 2,
    prices_confirm: bool | None = None,
) -> list[NewsJudgement]:
    """Classify one v4 news item. ``market_impacts`` never open a position."""
    item = without_extensions(item)
    news_id = root_news_id(str(item.get("news_id", "")))
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
        if status not in ACCEPTED_STATUS or event_type in EXCLUDED_TYPES:
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
