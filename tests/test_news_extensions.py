"""Optional news/1.0 fields and the morning file re-check.

``importance`` and ``opportunity`` arrived on 2026-10-05. They stay on the
file, and strategy code does not turn them into signals, ``judged_at``, or
updates. The session loader re-checks ``news/<date>.json`` until 07:15 Macau.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_trading.backtest.engine import BtSignal, _event_signals
from auto_trading.news.reader import (
    classify_item,
    events_for_strategy,
    read_news,
    read_session_news,
    without_extensions,
)
from auto_trading.news.timing import event_clock
from auto_trading.strategy import ApprovedStrategy, StrategyEngine, spec_hash

MACAU = ZoneInfo("Asia/Macau")
DAY = "2026-10-06"
JUDGED_AT = "2026-10-05T19:00:00+08:00"
SCORED_AT = "2026-10-06T07:06:00+08:00"


def _importance(total: int = 72, scored_at: str = SCORED_AT) -> dict[str, object]:
    return {
        "rubric": "importance/1.0",
        "total": total,
        "grade": "高",
        "scored_at": scored_at,
        "components": {
            "market_reaction": {"score": 20, "max": 40, "reason": "measured"},
            "novelty": {"score": 20, "max": 25, "reason": "new"},
            "valuation": {"score": 12, "max": 15, "reason": "gap"},
            "event_type": {"score": 10, "max": 10, "reason": "product"},
            "credibility": {"score": 10, "max": 10, "reason": "official"},
        },
        "market_reaction": {"status": "pending"},
    }


def _opportunity() -> dict[str, object]:
    return {
        "flag": True,
        "flag_basis": "not_yet_traded",
        "beneficiaries": [
            {
                "ticker": "AAPL",
                "market": "US",
                "name": "Apple",
                "rationale": "潛在機會：先進封裝需求外溢",
                "on_watchlist": True,
            }
        ],
        "confirm_signals": ["價格確認"],
        "invalidators": ["需求落空"],
    }


def _item() -> dict[str, object]:
    return {
        "news_id": "20261005-2330-ext",
        "status": "new",
        "event_type": "product",
        "published_at": "2026-10-05T18:00:00+08:00",
        "published_at_precision": "exact",
        "first_seen_at": "2026-10-05T18:05:00+08:00",
        "judged_at": JUDGED_AT,
        "generated_at": SCORED_AT,
        "tickers": [
            {"ticker": "2330.TW", "relation": "subject", "direction": "positive", "impact": 3}
        ],
        "importance": _importance(),
        "opportunity": _opportunity(),
    }


def _write(path: Path, item: dict[str, object] | None = None) -> None:
    payload = {
        "schema_version": "news/1.0",
        "date": DAY,
        "generated_at": SCORED_AT,
        "revision": 1,
        "window_start": "2026-10-05T07:00:00+08:00",
        "window_end": SCORED_AT,
        "items": [_item() if item is None else item],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_optional_fields_load_and_do_not_become_signals() -> None:
    item = _item()
    assert item["importance"]["total"] == 72
    assert item["opportunity"]["beneficiaries"][0]["ticker"] == "AAPL"

    judged = classify_item(item, "BUY")
    assert [row.ticker for row in judged] == ["2330.TW"]
    assert judged[0].outcome == "candidate"
    assert judged[0].news_id == "20261005-2330-ext"

    shadow = dict(item)
    shadow["tickers"] = []
    empty = classify_item(shadow, "BUY")
    assert empty[0].ticker is None
    assert empty[0].reason == "empty_tickers"
    assert events_for_strategy([shadow]) == []

    revised = json.loads(json.dumps(item))
    revised["importance"]["total"] = 10
    revised["importance"]["scored_at"] = "2026-10-06T12:00:00+08:00"
    revised["opportunity"]["flag"] = False
    assert without_extensions(item) == without_extensions(revised)

    earlier = json.loads(json.dumps(item))
    earlier["judged_at"] = "2026-10-05T18:30:00+08:00"
    earlier["importance"]["scored_at"] = "2026-10-06T18:00:00+08:00"
    earlier["importance"]["total"] = 99
    events = events_for_strategy([item, revised, earlier])
    assert len(events) == 1
    assert events[0]["ticker"] == "2330.TW"
    assert events[0]["status"] == "new"
    assert events[0]["news_id"] == "20261005-2330-ext"
    assert events[0]["judged_at"] == JUDGED_AT
    assert "importance" not in events[0]
    assert "opportunity" not in events[0]
    assert "-u" not in events[0]["news_id"]

    clock = event_clock(item, "TW")
    assert clock.usable
    assert clock.available_at == datetime.fromisoformat(JUDGED_AT)
    assert event_clock(events[0], "TW").available_at == clock.available_at
    assert clock.t1 is not None

    day = clock.t1.isoformat()
    strategy = {"trigger": {"type": "event", "timing": {"max_age_sessions": 1}}}
    signals = [
        BtSignal("2330.TW", "BUY", 80, 100.0, "tw", "2026-10-01"),
        BtSignal("AAPL", "BUY", 80, 100.0, "us", "2026-10-01"),
    ]
    chosen = _event_signals(strategy, {}, signals, events, day, f"{day}T07:05:00+08:00")
    assert [row.ticker for row in chosen] == ["2330.TW"]


def test_news_file_with_both_fields_stays_schema_1(tmp_path: Path) -> None:
    path = tmp_path / f"{DAY}.json"
    _write(path)
    loaded = read_news(path)
    assert loaded.enabled
    assert loaded.data_error is None
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "news/1.0"
    assert saved["items"][0]["importance"]["total"] == 72
    assert saved["items"][0]["opportunity"]["beneficiaries"][0]["rationale"].startswith("潛在機會")


def _moving_clock(start: datetime) -> tuple[dict[str, datetime], object, object]:
    state = {"now": start}

    def clock() -> datetime:
        return state["now"]

    def sleeper(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return state, clock, sleeper


def test_session_news_waits_until_the_file_appears(tmp_path: Path) -> None:
    start = datetime(2026, 10, 6, 7, 0, tzinfo=MACAU)
    state, clock, sleeper = _moving_clock(start)
    appeared = start + timedelta(minutes=6)

    def wait(seconds: float) -> None:
        sleeper(seconds)
        if state["now"] >= appeared and not (tmp_path / f"{DAY}.json").is_file():
            _write(tmp_path / f"{DAY}.json")

    loaded = read_session_news(tmp_path, DAY, clock=clock, sleep=wait, poll_seconds=60)
    assert loaded.enabled
    assert loaded.items[0]["judged_at"] == JUDGED_AT
    assert state["now"] >= appeared
    assert state["now"] < start + timedelta(minutes=15)


def test_session_news_stops_at_the_deadline(tmp_path: Path) -> None:
    start = datetime(2026, 10, 6, 7, 0, tzinfo=MACAU)
    deadline = start + timedelta(minutes=15)
    state, clock, sleeper = _moving_clock(start)
    slept_at: list[datetime] = []

    def wait(seconds: float) -> None:
        slept_at.append(state["now"])
        sleeper(seconds)

    loaded = read_session_news(tmp_path, DAY, clock=clock, sleep=wait, poll_seconds=60)
    assert loaded.enabled is False
    assert loaded.data_error == "missing"
    assert slept_at
    assert all(stamp < deadline for stamp in slept_at)
    assert state["now"] <= deadline


def test_session_news_does_not_wait_outside_the_window(tmp_path: Path) -> None:
    def fail(_seconds: float) -> None:
        raise AssertionError("must not wait")

    before = datetime(2026, 10, 6, 6, 59, tzinfo=MACAU)
    after = datetime(2026, 10, 6, 7, 15, tzinfo=MACAU)
    later_day = datetime(2026, 10, 7, 7, 6, tzinfo=MACAU)
    for moment in (before, after, later_day):
        loaded = read_session_news(tmp_path, DAY, clock=lambda moment=moment: moment, sleep=fail)
        assert loaded.data_error == "missing"

    ready = tmp_path / f"{DAY}.json"
    _write(ready)
    loaded = read_session_news(tmp_path, DAY, clock=lambda: before, sleep=fail)
    assert loaded.enabled


def _engine_document(news: Path) -> dict[str, object]:
    return {
        "id": "news_event",
        "version": 4,
        "mode": "paper",
        "trigger": {
            "type": "event",
            "sources": [
                {"source": "a_news", "path_pattern": str(news / "{date}.json")},
            ],
        },
    }


def test_engine_rechecks_news_and_ignores_the_extensions(tmp_path: Path) -> None:
    news = tmp_path / "news"
    news.mkdir()
    document = _engine_document(news)
    approved = ApprovedStrategy(
        strategy_id="news_event",
        version=4,
        spec_hash=spec_hash(document),
    )
    start = datetime(2026, 10, 6, 7, 0, tzinfo=MACAU)
    state, clock, sleeper = _moving_clock(start)

    def wait(seconds: float) -> None:
        sleeper(seconds)
        if state["now"] >= start + timedelta(minutes=6):
            _write(news / f"{DAY}.json")

    path = StrategyEngine().run(
        document,
        approved,
        trading_home=tmp_path / "home",
        session_date=DAY,
        signals=[],
        bars={},
        news_clock=clock,
        news_sleep=wait,
    )
    saved = path.read_text(encoding="utf-8")
    assert "AAPL" not in saved
    assert "20261005-2330-ext-u" not in saved
    assert state["now"] >= start + timedelta(minutes=6)
    assert state["now"] < start + timedelta(minutes=15)

    def fail(_seconds: float) -> None:
        raise AssertionError("an old session must not wait")

    old = StrategyEngine().run(
        document,
        approved,
        trading_home=tmp_path / "home",
        session_date="2026-10-02",
        signals=[],
        bars={},
        news_clock=lambda: datetime(2026, 10, 6, 7, 6, tzinfo=MACAU),
        news_sleep=fail,
    )
    assert '"intents": []' in old.read_text(encoding="utf-8")
